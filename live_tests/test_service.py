import asyncio
import json
import unittest
from dataclasses import replace

from live_pocs.config import BackendMode, Settings
from live_pocs.service import (
    ChatMessage,
    LivePocError,
    ModelReply,
    RunRequest,
    RunService,
    SessionKey,
)


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[SessionKey, str, tuple[ChatMessage, ...]]] = []
        self.forgotten: list[SessionKey] = []
        self.delay = 0.0
        self.failure: str | None = None
        self.cleanup_failure = False
        self.cleanup_delay = 0.0
        self.cleanup_started = asyncio.Event()
        self.started = asyncio.Event()
        self.closed = False

    async def infer(self, scope, prompt, history, invocation_id):
        self.calls.append((scope, prompt, history))
        self.started.set()
        await asyncio.sleep(self.delay)
        if self.failure:
            raise LivePocError(self.failure)
        return ModelReply(
            json.dumps(
                {"tenant": scope.tenant_id, "prompts": [
                    *(message.content for message in history if message.role == "user"),
                    prompt,
                ]}
            ),
            requested_model="chat",
        )

    async def forget(self, scope):
        self.forgotten.append(scope)
        self.cleanup_started.set()
        await asyncio.sleep(self.cleanup_delay)
        if self.cleanup_failure:
            raise OSError("synthetic cleanup failure")

    async def aclose(self):
        self.closed = True


def settings(**changes):
    return replace(
        Settings(BackendMode.FOUNDRY, "fixture-backend-key-at-least-24-characters", live_enabled=True),
        **changes,
    )


async def finished(service, tenant, run_id):
    async def wait():
        while True:
            result = service.get(tenant, run_id)
            if result["status"] in {"succeeded", "failed", "cancelled"}:
                return result
            await asyncio.sleep(0.001)
    return await asyncio.wait_for(wait(), timeout=2)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend = RecordingBackend()
        self.service = RunService(settings(), self.backend)

    async def asyncTearDown(self):
        await self.service.aclose()

    async def invoke(self, tenant="customer-a", **changes):
        request = replace(RunRequest("same-session", "first", new_session=True), **changes)
        run = self.service.submit(tenant, request)
        return await finished(self.service, tenant, run["run_id"])

    async def test_same_public_session_is_isolated_across_tenants(self):
        a, b = await asyncio.gather(
            self.invoke("customer-a", prompt="private-a"),
            self.invoke("customer-b", prompt="private-b"),
        )
        self.assertNotEqual(a["run_id"], b["run_id"])
        self.assertEqual(json.loads(a["result"]["content"])["prompts"], ["private-a"])
        self.assertEqual(json.loads(b["result"]["content"])["prompts"], ["private-b"])
        self.assertNotEqual(self.backend.calls[0][0].internal_id, self.backend.calls[1][0].internal_id)
        for tenant, marker in (("customer-a", "private-a"), ("customer-b", "private-b")):
            again = await self.invoke(tenant, prompt="second", new_session=False)
            self.assertEqual(again["turn"], 2)
            self.assertEqual(json.loads(again["result"]["content"])["prompts"], [marker, "second"])

    async def test_run_reads_and_cancellation_are_owned_by_tenant(self):
        run = await self.invoke()
        with self.assertRaisesRegex(LivePocError, "run_not_found"):
            self.service.get("customer-b", run["run_id"])
        with self.assertRaisesRegex(LivePocError, "run_not_found"):
            await self.service.cancel("customer-b", run["run_id"])
        self.assertEqual(self.service.get("customer-a", run["run_id"])["status"], "succeeded")

    async def test_unknown_session_never_silently_starts(self):
        with self.assertRaisesRegex(LivePocError, "session_not_found"):
            self.service.submit("customer-a", RunRequest("unknown", "resume"))
        self.assertFalse(self.backend.calls)

    async def test_explicit_new_session_does_not_overwrite_existing_history(self):
        await self.invoke()
        with self.assertRaisesRegex(LivePocError, "session_already_exists"):
            await self.invoke(prompt="overwrite")
        self.assertEqual(len(self.backend.calls), 1)

    async def test_agent_and_session_scopes_are_separate(self):
        await self.invoke(prompt="assistant-private")
        for changes in ({"agent_id": "reviewer"}, {"session_id": "other"}):
            result = await self.invoke(prompt="isolated", **changes)
            self.assertEqual(result["turn"], 1)
            self.assertEqual(json.loads(result["result"]["content"])["prompts"], ["isolated"])

    async def test_concurrent_turns_serialize_without_lost_history(self):
        first = self.service.submit("customer-a", RunRequest("same-session", "one", new_session=True))
        second = self.service.submit("customer-a", RunRequest("same-session", "two"))
        results = await asyncio.gather(
            finished(self.service, "customer-a", first["run_id"]),
            finished(self.service, "customer-a", second["run_id"]),
        )
        self.assertEqual([result["turn"] for result in results], [1, 2])
        self.assertEqual(json.loads(results[1]["result"]["content"])["prompts"], ["one", "two"])

    async def test_failed_turn_quarantines_only_its_session(self):
        await self.invoke()
        self.backend.failure = "gateway_http_429"
        failed = await self.invoke(prompt="failed", new_session=False)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "gateway_http_429")
        self.assertIsNone(failed["result"])
        with self.assertRaisesRegex(LivePocError, "session_quarantined"):
            await self.invoke(prompt="do-not-reset", new_session=False)
        self.backend.failure = None
        other = await self.invoke("customer-b")
        self.assertEqual(other["status"], "succeeded")
        self.assertTrue(self.backend.forgotten)

    async def test_timeout_is_explicit_and_bounded(self):
        self.service.settings = settings(run_timeout_seconds=0.01)
        self.backend.delay = 1
        failed = await self.invoke()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "run_timeout")
        with self.assertRaisesRegex(LivePocError, "session_quarantined"):
            await self.invoke(new_session=False)

    async def test_cancellation_before_task_start_releases_pending_capacity(self):
        self.service.settings = settings(max_pending_runs=1)
        run = self.service.submit("customer-a", RunRequest("cancel-first", "first", new_session=True))
        await self.service.cancel("customer-a", run["run_id"])
        self.assertEqual(self.service.get("customer-a", run["run_id"])["status"], "cancelled")
        result = await self.invoke(session_id="after-cancel")
        self.assertEqual(result["status"], "succeeded")

    async def test_cancellation_during_inference_quarantines_the_session(self):
        self.backend.delay = 1
        run = self.service.submit("customer-a", RunRequest("same-session", "first", new_session=True))
        await self.backend.started.wait()
        await self.service.cancel("customer-a", run["run_id"])
        result = self.service.get("customer-a", run["run_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["error"]["code"], "run_cancelled")
        with self.assertRaisesRegex(LivePocError, "session_quarantined"):
            self.service.submit("customer-a", RunRequest("same-session", "resume"))

    async def test_cleanup_failure_is_recorded_not_hidden(self):
        self.backend.failure = "synthetic_provider_error"
        self.backend.cleanup_failure = True
        with self.assertLogs("model_service.live", level="ERROR"):
            result = await self.invoke()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup_failed"])

    async def test_run_is_not_terminal_before_cleanup_attempt_finishes(self):
        self.backend.failure = "synthetic_provider_error"
        self.backend.cleanup_delay = 0.02
        run = self.service.submit("customer-a", RunRequest("cleanup", "first", new_session=True))
        await self.backend.cleanup_started.wait()
        self.assertIn(self.service.get("customer-a", run["run_id"])["status"], {"queued", "running"})
        result = await finished(self.service, "customer-a", run["run_id"])
        self.assertEqual(result["status"], "failed")

    async def test_exact_ten_turn_limit(self):
        for turn in range(1, 11):
            result = await self.invoke(prompt=str(turn), new_session=turn == 1)
            self.assertEqual(result["turn"], turn)
        with self.assertRaisesRegex(LivePocError, "session_turn_limit"):
            await self.invoke(new_session=False)
        self.assertEqual(len(self.backend.calls), 10)

    async def test_pending_limit_and_session_limit_fail_before_provider_calls(self):
        self.service.settings = settings(max_pending_runs=1, max_sessions=1)
        self.backend.delay = 0.02
        run = self.service.submit("customer-a", RunRequest("one", "first", new_session=True))
        with self.assertRaisesRegex(LivePocError, "pending_run_limit"):
            self.service.submit("customer-b", RunRequest("one", "blocked", new_session=True))
        await finished(self.service, "customer-a", run["run_id"])
        with self.assertRaisesRegex(LivePocError, "session_limit"):
            self.service.submit("customer-a", RunRequest("two", "blocked", new_session=True))
        other = await self.invoke("customer-b")
        self.assertEqual(other["status"], "succeeded")

    async def test_restart_requires_explicit_session_creation(self):
        old = await self.invoke()
        restarted = RunService(settings(), RecordingBackend())
        try:
            self.assertNotEqual(restarted.instance_id, self.service.instance_id)
            with self.assertRaisesRegex(LivePocError, "session_not_found"):
                restarted.submit("customer-a", RunRequest("same-session", "resume"))
            with self.assertRaisesRegex(LivePocError, "run_not_found"):
                restarted.get("customer-a", old["run_id"])
        finally:
            await restarted.aclose()

    async def test_live_calls_are_disabled_by_default(self):
        self.service.settings = settings(live_enabled=False)
        with self.assertRaisesRegex(LivePocError, "live_inference_disabled"):
            await self.invoke()
        self.assertFalse(self.backend.calls)

    async def test_invalid_inputs_and_aliases_never_reach_provider(self):
        for changes, code in (
            ({"agent_id": "other"}, "agent_not_allowed"),
            ({"model_alias": "other"}, "model_not_allowed"),
            ({"session_id": "../other"}, "invalid_session"),
            ({"prompt": " "}, "invalid_prompt"),
            ({"prompt": "x" * 8193}, "invalid_prompt"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(LivePocError, code):
                await self.invoke(**changes)
        self.assertFalse(self.backend.calls)
