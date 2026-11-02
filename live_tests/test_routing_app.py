import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from live_pocs.routing_app import InferenceProxy, create_app
from live_pocs.routing_storage import SQLiteRegistry
from live_tests.test_routing import CREDENTIALS, routing_settings


def completion(model="small-model-2026-01-01", **changes):
    return {
        "id": "fixture-completion", "object": "chat.completion", "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "synthetic reply"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
        **changes,
    }


def sse(*items, done=True, newline="\n"):
    text = "".join("data: " + json.dumps(item) + newline * 2 for item in items)
    if done:
        text += "data: [DONE]" + newline * 2
    return text.encode("utf-8")


class Pieces(httpx.AsyncByteStream):
    def __init__(self, data, size=7, failure=None):
        self.data = data
        self.size = size
        self.failure = failure
        self.closed = False

    async def __aiter__(self):
        for offset in range(0, len(self.data), self.size):
            yield self.data[offset:offset + self.size]
            await asyncio.sleep(0)
        if self.failure:
            raise self.failure

    async def aclose(self):
        self.closed = True


class RoutingAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="routing-app-tests-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bindings.sqlite3"
        self.settings = routing_settings(self.path)
        self.environment = patch.dict(os.environ, CREDENTIALS)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.requests = []
        self.reply = None

        def handle(request):
            self.requests.append(request)
            if isinstance(self.reply, Exception):
                raise self.reply
            if self.reply is not None:
                return self.reply
            model = "large-model-2026-02-01" if "large-provider" in request.url.host else "small-model-2026-01-01"
            return httpx.Response(200, json=completion(model))

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        self.addAsyncCleanup(self.upstream.aclose)
        self.app = create_app(self.settings, registry=SQLiteRegistry(self.path), client=self.upstream)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        self.app.state.router.clock = lambda: self.now
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="https://gateway.invalid",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"Authorization": "Bearer " + CREDENTIALS["ROUTING_CUSTOMER_A"]}
        self.other_headers = {"Authorization": "Bearer " + CREDENTIALS["ROUTING_CUSTOMER_B"]}
        self.path_for_call = "/sessions/assistant/same/v1/chat/completions"

    async def bind(self, alias="economy", **changes):
        response = await self.client.post(
            "/sessions", json={"session_id": "same", "model_alias": alias, **changes}, headers=self.headers,
        )
        self.assertIn(response.status_code, {200, 201}, response.text)
        return response

    async def infer(self, **changes):
        return await self.client.post(
            self.path_for_call,
            json={"model": "economy", "messages": [{"role": "user", "content": "synthetic"}], **changes},
            headers=self.headers,
        )

    async def test_health_is_configuration_only_and_protected_paths_require_customer_credentials(self):
        health = await self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        self.assertFalse(health.json()["inference_verified"])
        self.assertEqual(health.json()["binding_lifetime_hours"], 24)
        for headers in ({}, {"Authorization": "Bearer invalid"}, {"x-poc-tenant-id": "customer-a"}):
            result = await self.client.post(
                "/sessions", json={"session_id": "same", "model_alias": "economy"}, headers=headers,
            )
            self.assertEqual(result.status_code, 401)
        self.assertFalse(self.requests)

    async def test_idempotent_creation_explicit_model_conflict_and_fixed_expiry(self):
        first = await self.bind()
        self.assertEqual(first.status_code, 201)
        self.now += timedelta(hours=12)
        repeat = await self.bind()
        self.assertEqual(repeat.status_code, 200)
        self.assertEqual(repeat.json(), first.json())
        conflict = await self.client.post(
            "/sessions", json={"session_id": "same", "model_alias": "quality"}, headers=self.headers,
        )
        self.assertEqual(conflict.status_code, 409)
        self.now += timedelta(hours=12)
        for response in (
            await self.client.get("/sessions/assistant/same", headers=self.headers),
            await self.infer(),
            await self.client.get("/sessions/assistant/same/v1/models", headers=self.headers),
            await self.client.post(
                "/sessions", json={"session_id": "same", "model_alias": "economy"}, headers=self.headers,
            ),
        ):
            self.assertEqual(response.status_code, 410)
        self.assertFalse(self.requests)

    async def test_unknown_and_foreign_session_cannot_trigger_inference(self):
        self.assertEqual((await self.infer()).status_code, 404)
        await self.bind()
        for path in ("/sessions/assistant/same", "/sessions/assistant/same/v1/models"):
            self.assertEqual((await self.client.get(path, headers=self.other_headers)).status_code, 404)
        denied = await self.client.post(
            self.path_for_call, json={"model": "economy", "messages": [{"role": "user", "content": "x"}]},
            headers=self.other_headers,
        )
        self.assertEqual(denied.status_code, 404)
        self.assertFalse(self.requests)

    async def test_customer_model_grants_and_binding_input_cannot_be_spoofed(self):
        denied = await self.client.post(
            "/sessions", json={"session_id": "same", "model_alias": "quality"}, headers=self.other_headers,
        )
        self.assertEqual(denied.status_code, 403)
        for extra in (
            {"tenant_id": "customer-b"}, {"endpoint": "https://evil.invalid"},
            {"api_key": "SECRET_SHOULD_NOT_BE_ECHOED"},
        ):
            response = await self.client.post(
                "/sessions", json={"session_id": "same", "model_alias": "economy", **extra}, headers=self.headers,
            )
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("SECRET_SHOULD_NOT_BE_ECHOED", response.text)
        self.assertFalse(self.requests)

    async def test_sdk_context_tool_calls_and_compaction_summary_are_forwarded_without_second_history(self):
        await self.bind()
        messages = [
            {"role": "system", "content": "SDK-owned instructions"},
            {"role": "user", "content": "SDK_COMPACTED_PRIVATE_SUMMARY"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": '{"key":"x"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call-1", "content": "SDK_OWNED_TOOL_RESULT"},
            {"role": "user", "content": "next turn"},
        ]
        tools = [{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object", "properties": {"key": {"type": "string"}}},
        }}]
        self.reply = httpx.Response(200, json=completion(choices=[{
            "index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call-2", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
            ]}, "finish_reason": "tool_calls",
        }]))
        response = await self.infer(messages=messages, tools=tools, max_tokens=128)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), self.reply.json())
        request = self.requests[0]
        outgoing = json.loads(request.content)
        self.assertEqual(outgoing, {
            "model": "small-deployment", "messages": messages, "tools": tools,
            "max_tokens": 128, "store": False,
        })
        self.assertEqual(request.headers["Authorization"], "Bearer " + CREDENTIALS["ROUTING_PROVIDER_SMALL"])
        self.assertNotIn(CREDENTIALS["ROUTING_CUSTOMER_A"], str(request.headers))
        with closing(sqlite3.connect(self.path)) as connection:
            persisted = str(connection.execute("SELECT payload FROM session_bindings").fetchall())
        self.assertNotIn("SDK_COMPACTED_PRIVATE_SUMMARY", persisted)
        self.assertNotIn("SDK_OWNED_TOOL_RESULT", persisted)
        self.assertFalse(any(value in persisted for value in CREDENTIALS.values()))

    async def test_each_alias_reaches_its_own_endpoint_and_body_cannot_switch_the_pin(self):
        await self.bind("quality")
        rejected = await self.infer(model="economy")
        self.assertEqual(rejected.status_code, 409)
        self.assertFalse(self.requests)
        response = await self.infer(model="quality")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(self.requests[0].url), "https://large-provider.invalid/openai/v1/chat/completions")
        self.assertEqual(self.requests[0].headers["api-key"], CREDENTIALS["ROUTING_PROVIDER_LARGE"])
        self.assertNotIn("authorization", self.requests[0].headers)
        self.assertEqual(json.loads(self.requests[0].content)["model"], "large-deployment")

    async def test_provider_credential_rotation_does_not_change_the_binding(self):
        original = (await self.bind()).json()
        with patch.dict(os.environ, {"ROUTING_PROVIDER_SMALL": "ROTATED_SYNTHETIC_PROVIDER_KEY"}):
            self.assertEqual((await self.infer()).status_code, 200)
        self.assertEqual(self.requests[0].headers["Authorization"], "Bearer ROTATED_SYNTHETIC_PROVIDER_KEY")
        self.assertEqual((await self.bind()).json(), original)

    async def test_missing_provider_credential_and_live_disabled_do_not_send_requests(self):
        await self.bind()
        with patch.dict(os.environ, {"ROUTING_PROVIDER_SMALL": ""}):
            missing = await self.infer()
            self.assertEqual(missing.status_code, 503)
            self.assertEqual(missing.json()["error"]["code"], "endpoint_credential_unavailable")
        self.app.state.router.settings = self.settings.model_copy(update={"live_enabled": False})
        self.assertEqual((await self.infer()).status_code, 503)
        self.assertFalse(self.requests)

    async def test_provider_errors_timeouts_and_redirects_do_not_retry_or_rebind(self):
        first = (await self.bind()).json()
        cases: list[tuple[httpx.Response | httpx.RequestError, int, str]] = [
            (httpx.Response(code, json={"error": "SECRET_PROVIDER_ERROR_CONTENT"}), code, f"endpoint_http_{code}")
            for code in (400, 401, 429, 500)
        ]
        cases.extend([
            (httpx.Response(302, headers={"Location": "https://other.invalid"}), 502, "endpoint_http_302"),
            (httpx.ReadTimeout("PRIVATE_TRANSPORT_DETAIL"), 504, "endpoint_timeout"),
        ])
        for reply, status, code in cases:
            self.reply = reply
            previous = len(self.requests)
            response = await self.infer()
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["error"]["code"], code)
            self.assertNotIn("PRIVATE_TRANSPORT_DETAIL", response.text)
            self.assertNotIn("SECRET_PROVIDER_ERROR_CONTENT", response.text)
            self.assertEqual(len(self.requests), previous + 1)
        self.assertEqual((await self.bind()).json(), first)

    async def test_response_model_mismatch_is_rejected_without_exposing_provider_content(self):
        await self.bind()
        self.reply = httpx.Response(200, json=completion(
            "wrong-model", choices=[{"message": {"content": "MUST_NOT_BE_RETURNED"}}],
        ))
        response = await self.infer()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "provider_model_mismatch")
        self.assertNotIn("MUST_NOT_BE_RETURNED", response.text)
        self.assertTrue(self.reply.is_closed)

    async def test_request_limits_provider_state_and_hosted_tools_fail_before_forwarding(self):
        await self.bind()
        for extra in (
            {"store": True}, {"previous_response_id": "other-object"},
            {"tools": [{"type": "web_search"}]}, {"max_tokens": 4097},
            {"stream": "true"}, {"n": 2}, {"tenant_id": "customer-b"},
            {"messages": [{"role": "user", "content": [{"type": "file", "file": {"file_id": "other-file"}}]}]},
        ):
            with self.subTest(extra=extra):
                self.assertEqual((await self.infer(**extra)).status_code, 422)
        self.app.state.router.settings = self.settings.model_copy(update={"max_request_bytes": 1024})
        self.assertEqual((await self.infer(messages=[{"role": "user", "content": "x" * 2048}])).status_code, 413)
        self.assertFalse(self.requests)

    async def test_fragmented_sse_tool_events_and_usage_are_preserved(self):
        await self.bind()
        events = sse(
            {"id": "chat-1", "model": "small-model-2026-01-01", "choices": [{"delta": {"role": "assistant"}}]},
            {"id": "chat-1", "model": "small-model-2026-01-01", "choices": [{"delta": {
                "tool_calls": [{"index": 0, "id": "tool-1", "function": {"name": "lookup", "arguments": "{}"}}],
            }}]},
            {"id": "chat-1", "model": "small-model-2026-01-01", "choices": [], "usage": {"prompt_tokens": 10}},
            newline="\r\n",
        )
        stream = Pieces(events, size=3)
        self.reply = httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
        response = await self.infer(stream=True, stream_options={"include_usage": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, events)
        self.assertTrue(stream.closed)
        self.assertEqual(json.loads(self.requests[0].content)["stream_options"], {"include_usage": True})
        self.assertIn("X-Model-Session-Expires-At", response.headers)

    async def test_bad_first_stream_model_is_rejected_before_stream_headers(self):
        await self.bind()
        self.reply = httpx.Response(
            200, headers={"Content-Type": "text/event-stream"},
            stream=Pieces(sse({"model": "wrong-model", "choices": [{"delta": {"content": "PRIVATE"}}]})),
        )
        response = await self.infer(stream=True)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "provider_model_mismatch")
        self.assertNotIn("PRIVATE", response.text)
        self.assertTrue(self.reply.is_closed)

    async def test_late_stream_failure_emits_explicit_error_without_success_done_or_fallback(self):
        await self.bind()
        data = sse({"model": "small-model-2026-01-01", "choices": [{"delta": {"content": "first"}}]}, done=False)
        for raw, failure, code in (
            (data, None, "provider_stream_incomplete"),
            (data, httpx.ReadError("PRIVATE_ERROR"), "provider_stream_interrupted"),
            (data + sse({"model": "wrong-model", "choices": [{"delta": {"content": "DO_NOT_LEAK"}}]}), None, "provider_model_mismatch"),
        ):
            with self.subTest(code=code):
                stream = Pieces(raw, failure=failure)
                self.reply = httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stream)
                with self.assertLogs("model_service.live", level="ERROR"):
                    response = await self.infer(stream=True)
                self.assertEqual(response.status_code, 200)
                self.assertIn(code, response.text)
                self.assertNotIn("[DONE]", response.text)
                self.assertNotIn("DO_NOT_LEAK", response.text)
                self.assertNotIn("PRIVATE_ERROR", response.text)
                self.assertTrue(stream.closed)

    async def test_stream_disconnect_closes_upstream(self):
        stream = Pieces(b"")
        response = httpx.Response(200, stream=stream)

        async def events():
            yield b"next event"

        proxy = InferenceProxy(self.app.state.router, self.upstream)
        body = proxy._stream(b"first event", events(), response)
        self.assertEqual(await anext(body), b"first event")
        await body.aclose()
        self.assertTrue(stream.closed)

    async def test_stream_has_an_absolute_deadline_not_only_per_chunk_read_timeouts(self):
        stream = Pieces(b"")
        response = httpx.Response(200, stream=stream)

        async def stalled():
            await asyncio.sleep(10)
            yield b"unexpected"

        proxy = InferenceProxy(self.app.state.router, self.upstream)
        body = proxy._stream(
            b"first event", stalled(), response,
            deadline=asyncio.get_running_loop().time() + 0.02,
        )
        self.assertEqual(await anext(body), b"first event")
        with self.assertLogs("model_service.live", level="ERROR"):
            error = await anext(body)
        self.assertIn(b"endpoint_timeout", error)
        with self.assertRaises(StopAsyncIteration):
            await anext(body)
        self.assertTrue(stream.closed)

    async def test_first_stream_event_timeout_and_oversized_reply_close_the_provider(self):
        await self.bind()
        self.app.state.router.settings = self.settings.model_copy(update={"upstream_timeout_seconds": 0.02})

        class NoEvents(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                await asyncio.sleep(10)
                yield b"not reached"

            async def aclose(self):
                self.closed = True

        stalled = NoEvents()
        self.reply = httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=stalled)
        response = await self.infer(stream=True)
        self.assertEqual(response.status_code, 504)
        self.assertTrue(stalled.closed)
        self.app.state.router.settings = self.settings.model_copy(update={"max_response_bytes": 1024})
        self.reply = httpx.Response(200, json=completion(choices=[{
            "message": {"content": "x" * 2048},
        }]))
        response = await self.infer()
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "provider_response_too_large")
        self.assertTrue(self.reply.is_closed)
