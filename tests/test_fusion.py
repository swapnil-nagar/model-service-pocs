import asyncio
import json
import unittest
from dataclasses import replace

from model_service.contracts import ModelServiceError, Request
from model_service.fusion import (
    FusionExperiment,
    FusionService,
    MockFusionClient,
    run_fusion,
)


class FusionTests(unittest.IsolatedAsyncioTestCase):
    def request(self, tenant="customer-a", **changes):
        return replace(Request(tenant, "assistant", "same-session", "chat", "first"), **changes)

    async def call(self, experiment, request):
        return await experiment.service.run(
            experiment.customers[request.tenant_id].caller_token, request
        )

    async def test_customers_have_distinct_clients_sessions_and_history(self):
        async with FusionExperiment() as experiment:
            requests = [
                self.request(tenant, prompt=f"private-{tenant}")
                for tenant in experiment.customers
            ]
            results = await asyncio.gather(*(self.call(experiment, r) for r in requests))
            self.assertEqual(len({result.sdk_session_id for result in results}), 2)
            clients = list(experiment.service.clients.values())
            self.assertIsNot(clients[0], clients[1])
            for request, result in zip(requests, results, strict=True):
                self.assertEqual(json.loads(result.content)["tenant"], request.tenant_id)
                self.assertEqual(json.loads(result.content)["user_prompts"], [request.prompt])
                again = await self.call(experiment, replace(request, prompt="second"))
                self.assertEqual(again.sdk_session_id, result.sdk_session_id)
                self.assertEqual(again.turn, 2)
                self.assertEqual(
                    json.loads(again.content)["user_prompts"], [request.prompt, "second"]
                )
                self.assertEqual(
                    json.loads(again.content)["internal_execution_pattern"], "not_simulated"
                )

    async def test_invalid_requests_are_rejected_before_sdk_use(self):
        async with FusionExperiment() as experiment:
            token = experiment.customers["customer-a"].caller_token
            cases = [
                ("invalid", self.request(), "unauthenticated"),
                ("\u2603", self.request(), "unauthenticated"),
                (token, self.request("customer-b"), "tenant_mismatch"),
                (token, self.request(agent_id="administrator"), "agent_not_allowed"),
                (token, self.request(model_alias="other-model"), "model_not_allowed"),
                (token, self.request(session_id="../customer-b"), "invalid_session"),
                (token, self.request(prompt=" "), "invalid_prompt"),
                (token, self.request(prompt="x" * 8193), "invalid_prompt"),
            ]
            for caller, request, code in cases:
                with self.subTest(code=code), self.assertRaises(ModelServiceError) as error:
                    await experiment.service.run(caller, request)
                self.assertEqual(error.exception.code, code)
            self.assertFalse(experiment.service.clients)
            self.assertTrue(all(not account.audit for account in experiment.accounts.values()))

    async def test_another_customers_github_token_cannot_authenticate(self):
        async with FusionExperiment() as experiment:
            a, b = experiment.customers.values()
            wrong = replace(a, github_token=b.github_token)
            with self.assertRaises(ValueError):
                FusionService((wrong, b), experiment.accounts)
            service = FusionService((wrong,), {"customer-a": experiment.accounts["customer-a"]})
            try:
                with self.assertRaisesRegex(ModelServiceError, "copilot_unauthenticated"):
                    await service.run(a.caller_token, self.request())
            finally:
                await service.aclose()

    async def test_wrong_github_token_fails_without_ambient_fallback(self):
        async with FusionExperiment() as experiment:
            a, b = experiment.customers.values()
            wrong = replace(a, github_token="invalid-fixture-token")
            service = FusionService((wrong, b), experiment.accounts)
            try:
                with self.assertRaisesRegex(ModelServiceError, "copilot_unauthenticated"):
                    await service.run(a.caller_token, self.request())
                self.assertFalse(service.sessions)
                self.assertFalse(
                    any(
                        entry["event"] == "send"
                        for account in experiment.accounts.values()
                        for entry in account.audit
                    )
                )
                result = await service.run(b.caller_token, self.request("customer-b"))
                self.assertEqual(json.loads(result.content)["tenant"], "customer-b")
            finally:
                await service.aclose()

    async def test_model_discovery_is_customer_scoped_and_never_falls_back(self):
        async with FusionExperiment() as experiment:
            experiment.accounts["customer-b"].available_models = ("other-model",)
            await self.call(experiment, self.request())
            with self.assertRaisesRegex(ModelServiceError, "fusion_unavailable"):
                await self.call(experiment, self.request("customer-b"))
            self.assertEqual(len(experiment.service.clients), 2)
            self.assertEqual(len(experiment.service.sessions), 1)
            self.assertFalse(
                any(
                    entry["event"] in {"session_create", "send"}
                    for entry in experiment.accounts["customer-b"].audit
                )
            )
            self.assertEqual(
                sum(
                    entry["event"] == "models_list"
                    for account in experiment.accounts.values()
                    for entry in account.audit
                ),
                2,
            )

    async def test_agent_and_session_namespaces_are_distinct(self):
        async with FusionExperiment() as experiment:
            results = [await self.call(experiment, self.request(prompt="assistant-private"))]
            for changes in ({"agent_id": "reviewer"}, {"session_id": "other"}):
                result = await self.call(experiment, self.request(prompt="isolated", **changes))
                self.assertEqual(result.turn, 1)
                self.assertEqual(json.loads(result.content)["user_prompts"], ["isolated"])
                results.append(result)
            self.assertEqual(len({result.sdk_session_id for result in results}), 3)
            self.assertEqual(
                sum(e["event"] == "models_list" for e in experiment.accounts["customer-a"].audit),
                1,
            )

    async def test_same_session_concurrency_serializes_turns(self):
        async with FusionExperiment() as experiment:
            results = await asyncio.gather(
                *(self.call(experiment, self.request(prompt=f"turn-{n}")) for n in range(3))
            )
            self.assertEqual(sorted(result.turn for result in results), [1, 2, 3])
            final = max(results, key=lambda result: result.turn)
            self.assertEqual(
                set(json.loads(final.content)["user_prompts"]), {"turn-0", "turn-1", "turn-2"}
            )
            self.assertEqual(len({result.sdk_session_id for result in results}), 1)

    async def test_errors_quarantine_sessions_instead_of_resetting_history(self):
        for code in ("copilot_unauthenticated", "copilot_forbidden", "copilot_rate_limited"):
            with self.subTest(code=code):
                async with FusionExperiment() as experiment:
                    await self.call(experiment, self.request(prompt="successful"))
                    experiment.accounts["customer-a"].send_error = code
                    with self.assertRaisesRegex(ModelServiceError, code):
                        await self.call(experiment, self.request(prompt="failed"))
                    experiment.accounts["customer-a"].send_error = None
                    with self.assertRaisesRegex(ModelServiceError, "fusion_session_failed"):
                        await self.call(experiment, self.request(prompt="do-not-reset"))
                    other = await self.call(experiment, self.request("customer-b"))
                    self.assertEqual(other.turn, 1)
                    self.assertTrue(next(iter(experiment.service.sessions.values())).closed)

    async def test_empty_and_absent_responses_are_explicit_failures(self):
        for response_kind in ("none", "empty"):
            with self.subTest(response_kind=response_kind):
                async with FusionExperiment() as experiment:
                    experiment.accounts["customer-a"].response_kind = response_kind
                    with self.assertRaisesRegex(ModelServiceError, "invalid_fusion_response"):
                        await self.call(experiment, self.request())
                    with self.assertRaisesRegex(ModelServiceError, "fusion_session_failed"):
                        await self.call(experiment, self.request())

    async def test_timeout_is_explicit_and_session_is_quarantined(self):
        async with FusionExperiment() as experiment:
            experiment.service.timeout = 0.01
            experiment.accounts["customer-a"].response_delay = 1
            with self.assertRaisesRegex(ModelServiceError, "fusion_timeout"):
                await self.call(experiment, self.request())
            with self.assertRaisesRegex(ModelServiceError, "fusion_session_failed"):
                await self.call(experiment, self.request())

    async def test_cancellation_quarantines_an_in_progress_session(self):
        async with FusionExperiment() as experiment:
            experiment.accounts["customer-a"].response_delay = 1
            pending = asyncio.create_task(self.call(experiment, self.request()))
            await asyncio.sleep(0)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            with self.assertRaisesRegex(ModelServiceError, "fusion_session_failed"):
                await self.call(experiment, self.request())

    async def test_tools_and_ambient_login_are_disabled_in_mock_boundary(self):
        async with FusionExperiment() as experiment:
            await self.call(experiment, self.request())
            session = next(iter(experiment.service.sessions.values()))
            with self.assertRaisesRegex(ModelServiceError, "tool_not_available"):
                session.attempt_tool_for_test("shell")
            account = experiment.accounts["customer-a"]
            client_events = [e for e in account.audit if e["event"] == "client_create"]
            session_events = [e for e in account.audit if e["event"] == "session_create"]
            self.assertEqual(client_events[0]["mode"], "empty")
            self.assertFalse(client_events[0]["use_logged_in_user"])
            self.assertTrue(session_events[0]["explicit_session_token"])
            self.assertFalse(session_events[0]["byok_provider"])
            self.assertFalse(session_events[0]["tools_enabled"])
            with self.assertRaisesRegex(ModelServiceError, "tools_not_allowed"):
                await experiment.service.clients["customer-a"].create_session(
                    session_id="untrusted-tools",
                    model="fusion",
                    github_token=experiment.customers["customer-a"].github_token,
                    available_tools=["shell"],
                )
            for mode, use_login in (("copilot-cli", False), ("empty", True)):
                with self.assertRaisesRegex(ModelServiceError, "unsafe_sdk_configuration"):
                    MockFusionClient(
                        account,
                        github_token=experiment.customers["customer-a"].github_token,
                        mode=mode,
                        use_logged_in_user=use_login,
                    )

    async def test_ten_successful_turn_limit_is_enforced(self):
        async with FusionExperiment() as experiment:
            for turn in range(1, 11):
                result = await self.call(experiment, self.request(prompt=str(turn)))
                self.assertEqual(result.turn, turn)
            with self.assertRaisesRegex(ModelServiceError, "session_turn_limit"):
                await self.call(experiment, self.request())
            self.assertEqual(
                sum(e["event"] == "send" for e in experiment.accounts["customer-a"].audit), 10
            )

    async def test_cleanup_disconnects_sessions_and_stops_clients(self):
        experiment = FusionExperiment()
        async with experiment:
            await self.call(experiment, self.request())
            sessions = list(experiment.service.sessions.values())
            clients = list(experiment.service.clients.values())
        self.assertTrue(all(session.closed for session in sessions))
        self.assertTrue(all(client.closed for client in clients))
        self.assertFalse(experiment.service.sessions)
        with self.assertRaisesRegex(ModelServiceError, "fusion_service_closed"):
            await self.call(experiment, self.request())

    async def test_smoke_result_discloses_unverified_properties_and_contains_no_prompts(self):
        result = await run_fusion()
        self.assertEqual(result["mode"], "local_sdk_mock_only")
        self.assertEqual(result["status"], "conditional")
        self.assertEqual(result["github_model_calls"], 0)
        self.assertEqual(result["distinct_sdk_sessions"], 2)
        self.assertEqual(result["mock_sdk_clients"], 2)
        self.assertEqual(result["process_container_isolation"], "not_exercised")
        self.assertEqual(result["internal_execution_patterns"], "not_observed_or_simulated")
        self.assertNotIn("Hello customer", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
