import json
import unittest
from dataclasses import replace

import httpx

from live_pocs.config import BackendMode, Settings
from live_pocs.foundry import FoundryBackend
from live_pocs.service import ChatMessage, LivePocError, SessionKey


def settings():
    return Settings(
        BackendMode.FOUNDRY,
        "fixture-backend-key-at-least-24-characters",
        live_enabled=True,
        foundry_gateway_base_url="https://fixture.azure-api.net/model",
        model_deployment="shared-deployment",
        tenant_a_key="credential-for-a",
        tenant_b_key="credential-for-b",
    )


def completion(tenant="customer-a", **changes):
    body = {
        "model": "gpt-4.1-mini-2025-04-14",
        "choices": [{"message": {"content": "synthetic reply"}}],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 8,
            "prompt_tokens_details": {"cached_tokens": 12},
        },
        **changes,
    }
    return httpx.Response(200, json=body, headers={"x-poc-tenant-id": tenant})


class FoundryTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_fixed_gateway_with_distinct_credentials_and_scoped_history(self):
        requests = []

        def handle(request):
            requests.append(request)
            return completion(request.headers["x-poc-tenant-id"])

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            backend = FoundryBackend(settings(), client)
            for tenant, key, marker in (
                ("customer-a", "credential-for-a", "private-a"),
                ("customer-b", "credential-for-b", "private-b"),
            ):
                reply = await backend.infer(
                    SessionKey(tenant, "assistant", "same", "chat", tenant),
                    "followup", (ChatMessage("user", marker),), "run-id",
                )
                self.assertEqual(reply.deployment, "shared-deployment")
                self.assertEqual(reply.input_tokens, 20)
                self.assertEqual(reply.output_tokens, 8)
                self.assertEqual(reply.cached_input_tokens, 12)
                request = requests[-1]
                self.assertEqual(str(request.url), "https://fixture.azure-api.net/model/chat/completions")
                self.assertEqual(request.headers["ocp-apim-subscription-key"], key)
                body = json.loads(request.content)
                self.assertEqual(body["model"], "chat")
                self.assertFalse(body["store"])
                self.assertEqual(body["max_tokens"], 128)
                self.assertEqual(
                    [m["content"] for m in body["messages"] if m["role"] == "user"],
                    [marker, "followup"],
                )
                self.assertNotIn("credential-for", request.content.decode())

    async def test_mismatched_tenant_response_is_not_a_success(self):
        for response in (
            completion("customer-b"),
            httpx.Response(200, json=completion().json()),
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: response)
            ) as client:
                with self.assertRaisesRegex(LivePocError, "gateway_tenant_mismatch"):
                    await FoundryBackend(settings(), client).infer(
                        SessionKey("customer-a", "assistant", "same", "chat", "id"), "test", (), "run"
                    )

    async def test_http_and_schema_errors_are_explicit(self):
        for response, code in (
            (httpx.Response(401), "gateway_http_401"),
            (httpx.Response(429), "gateway_http_429"),
            (httpx.Response(302, headers={"Location": "https://example.invalid"}), "gateway_http_302"),
            (completion(choices=[]), "invalid_provider_response"),
            (httpx.Response(200, content=b"not-json", headers={"x-poc-tenant-id": "customer-a"}), "invalid_provider_response"),
        ):
            with self.subTest(code=code):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(lambda request: response)
                ) as client:
                    with self.assertRaisesRegex(LivePocError, code):
                        await FoundryBackend(settings(), client).infer(
                            SessionKey("customer-a", "assistant", "same", "chat", "id"), "test", (), "run"
                        )

    async def test_unreported_usage_remains_unknown_not_zero(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: completion(usage=None))
        ) as client:
            result = await FoundryBackend(settings(), client).infer(
                SessionKey("customer-a", "assistant", "same", "chat", "id"), "test", (), "run"
            )
            self.assertIsNone(result.input_tokens)
            self.assertIsNone(result.output_tokens)
            self.assertFalse(result.view()["usage_reported"])

    async def test_missing_credential_does_not_issue_a_request(self):
        requests = []
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: requests.append(request))
        ) as client:
            with self.assertRaisesRegex(LivePocError, "tenant_credential_unavailable"):
                await FoundryBackend(replace(settings(), tenant_a_key=""), client).infer(
                    SessionKey("customer-a", "assistant", "same", "chat", "id"), "test", (), "run"
                )
        self.assertFalse(requests)
