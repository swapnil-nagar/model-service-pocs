import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from tempfile import gettempdir
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request as HttpRequest, build_opener

from model_service.contracts import Approach, ModelServiceError, Request, Target
from model_service.mocks import LocalExperiment
from model_service.service import ModelService


class IsolationTests(unittest.TestCase):
    def request(self, tenant="customer-a", **changes):
        return replace(Request(tenant, "assistant", "shared-id", "chat", "first"), **changes)

    def call(self, experiment, request):
        return experiment.service.run(
            experiment.customers[request.tenant_id].caller_token, request
        )

    def test_all_approaches_isolate_tenants_history_credentials_and_processes(self):
        for approach in Approach:
            with self.subTest(approach=approach), LocalExperiment(approach) as experiment:
                requests = [
                    self.request(tenant, prompt=f"private-{tenant}")
                    for tenant in experiment.customers
                ]
                with patch.dict(os.environ, {"OTHER_CUSTOMER_SECRET": "must-not-inherit"}):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        results = list(pool.map(lambda r: self.call(experiment, r), requests))
                self.assertEqual(len({result.workspace_id for result in results}), 2)
                self.assertEqual(len({result.worker_pid for result in results}), 2)
                for request, result in zip(requests, results, strict=True):
                    self.assertNotEqual(result.worker_pid, os.getpid())
                    self.assertFalse((Path(gettempdir()) / result.workspace_id).exists())
                    self.assertEqual(json.loads(result.content)["user_prompts"], [request.prompt])
                    self.assertEqual(json.loads(result.content)["tenant"], request.tenant_id)
                    followup = self.call(experiment, replace(request, prompt="second"))
                    self.assertEqual(followup.turn, 2)
                    self.assertNotEqual(followup.workspace_id, result.workspace_id)
                    self.assertEqual(
                        json.loads(followup.content)["user_prompts"], [request.prompt, "second"]
                    )
                for tenant, endpoint in experiment.endpoints.items():
                    self.assertEqual([call["user_turns"] for call in endpoint.calls], [1, 2])
                    if approach == Approach.APIM:
                        self.assertEqual(len(experiment.backends[tenant].calls), 2)

    def test_untrusted_routing_and_invalid_requests_never_reach_provider(self):
        for approach in Approach:
            with self.subTest(approach=approach), LocalExperiment(approach) as experiment:
                token = experiment.customers["customer-a"].caller_token
                cases = [
                    ("invalid", self.request(), "unauthenticated"),
                    ("\u2603", self.request(), "unauthenticated"),
                    (token, self.request("customer-b"), "tenant_mismatch"),
                    (token, self.request(agent_id="administrator"), "agent_not_allowed"),
                    (token, self.request(model_alias="other-deployment"), "model_not_allowed"),
                    (token, self.request(session_id="../customer-b"), "invalid_session"),
                    (token, self.request(prompt=" "), "invalid_prompt"),
                    (token, self.request(prompt="x" * 8193), "invalid_prompt"),
                ]
                for caller, request, code in cases:
                    with self.subTest(code=code), self.assertRaises(ModelServiceError) as error:
                        experiment.service.run(caller, request)
                    self.assertEqual(error.exception.code, code)
                self.assertTrue(all(not endpoint.calls for endpoint in experiment.endpoints.values()))

    def test_agent_and_session_scopes(self):
        for approach in Approach:
            with self.subTest(approach=approach), LocalExperiment(approach) as experiment:
                self.call(experiment, self.request(prompt="assistant-private"))
                for changes in ({"agent_id": "reviewer"}, {"session_id": "other"}):
                    result = self.call(experiment, self.request(prompt="isolated", **changes))
                    self.assertEqual(result.turn, 1)
                    self.assertEqual(json.loads(result.content)["user_prompts"], ["isolated"])

    def test_same_session_concurrency_does_not_lose_turns(self):
        for approach in Approach:
            with self.subTest(approach=approach), LocalExperiment(approach) as experiment:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    results = list(
                        pool.map(
                            lambda n: self.call(experiment, self.request(prompt=f"turn-{n}")),
                            range(3),
                        )
                    )
                self.assertEqual(sorted(result.turn for result in results), [1, 2, 3])
                final = max(results, key=lambda result: result.turn)
                self.assertEqual(
                    set(json.loads(final.content)["user_prompts"]),
                    {"turn-0", "turn-1", "turn-2"},
                )

    def test_errors_are_explicit_and_failed_turns_are_not_saved(self):
        for approach in Approach:
            with self.subTest(approach=approach), LocalExperiment(approach) as experiment:
                endpoint = experiment.endpoints["customer-a"]
                for status in (401, 403, 429, 500):
                    endpoint.fail_status = status
                    with self.assertRaises(ModelServiceError) as error:
                        self.call(experiment, self.request(prompt="failed"))
                    self.assertEqual(error.exception.code, f"upstream_http_{status}")
                endpoint.fail_status = None
                endpoint.malformed = True
                with self.assertRaisesRegex(ModelServiceError, "invalid_response"):
                    self.call(experiment, self.request(prompt="failed"))
                endpoint.malformed = False
                success = self.call(experiment, self.request(prompt="successful"))
                self.assertEqual(success.turn, 1)
                self.assertEqual(json.loads(success.content)["user_prompts"], ["successful"])

    def test_provider_rejects_another_tenants_key(self):
        for approach in Approach:
            with self.subTest(approach=approach), LocalExperiment(approach) as experiment:
                a = experiment.customers["customer-a"]
                b = experiment.customers["customer-b"]
                wrong = replace(a, target=replace(a.target, credential=b.target.credential))
                service = ModelService(approach, (wrong,))
                with self.assertRaisesRegex(ModelServiceError, "upstream_http_401"):
                    service.run(a.caller_token, self.request())

    def test_apim_forwards_only_to_its_tenants_backend_and_propagates_errors(self):
        with LocalExperiment(Approach.APIM) as experiment:
            experiment.backends["customer-a"].fail_status = 429
            with self.assertRaisesRegex(ModelServiceError, "upstream_http_429"):
                self.call(experiment, self.request())
            result = self.call(experiment, self.request("customer-b"))
            self.assertEqual(json.loads(result.content)["tenant"], "customer-b")
            self.assertEqual(len(experiment.backends["customer-b"].calls), 1)

    def test_provider_validates_deployment(self):
        for approach in (Approach.AZURE_OPENAI, Approach.FOUNDRY, Approach.APIM):
            with self.subTest(approach=approach), LocalExperiment(approach) as experiment:
                customer = experiment.customers["customer-a"]
                wrong = replace(customer, target=replace(customer.target, deployment="wrong"))
                service = ModelService(approach, (wrong,))
                expected = "404" if approach == Approach.AZURE_OPENAI else "403"
                with self.assertRaisesRegex(ModelServiceError, f"upstream_http_{expected}"):
                    service.run(customer.caller_token, self.request())

    def test_registry_rejects_shared_targets_and_nonlocal_endpoints(self):
        with LocalExperiment(Approach.FOUNDRY) as experiment:
            a, b = experiment.customers.values()
            for target in (
                a.target,
                Target("https://example.invalid", "new-key"),
                Target("http://127.0.0.1:1234/path", "new-key"),
                Target("http://user:password@127.0.0.1:1234", "new-key"),
            ):
                with self.subTest(target=target), self.assertRaises(ValueError):
                    ModelService(Approach.FOUNDRY, (a, replace(b, target=target)))

    def test_worker_timeout_is_explicit(self):
        with LocalExperiment(Approach.AZURE_OPENAI) as experiment:
            customer = experiment.customers["customer-a"]
            service = ModelService(Approach.AZURE_OPENAI, (customer,), timeout=0.000001)
            with self.assertRaisesRegex(ModelServiceError, "worker_timeout"):
                service.run(customer.caller_token, self.request())

    def test_turn_limit_is_measured_not_only_documented(self):
        with LocalExperiment(Approach.AZURE_OPENAI) as experiment:
            for turn in range(1, 11):
                result = self.call(experiment, self.request(prompt=str(turn)))
                self.assertEqual(result.turn, turn)
            with self.assertRaisesRegex(ModelServiceError, "session_turn_limit"):
                self.call(experiment, self.request())
            self.assertEqual(len(experiment.endpoints["customer-a"].calls), 10)

    def test_mock_backend_requires_its_own_credential(self):
        with LocalExperiment(Approach.APIM) as experiment:
            backend = experiment.backends["customer-a"]
            gateway = experiment.endpoints["customer-a"]
            request = HttpRequest(
                backend.target.endpoint
                + f"/openai/deployments/{backend.deployment}/chat/completions"
                "?api-version=2024-10-21",
                b'{"messages":[{"role":"user","content":"test"}]}',
                {"api-key": gateway.key, "Content-Type": "application/json"},
            )
            with self.assertRaises(HTTPError) as error:
                build_opener(ProxyHandler({})).open(request, timeout=2)
            self.assertEqual(error.exception.code, 401)


if __name__ == "__main__":
    unittest.main()
