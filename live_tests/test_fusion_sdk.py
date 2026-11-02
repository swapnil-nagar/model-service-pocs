import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from copilot.session_events import AssistantMessageData

from live_pocs.config import BackendMode, Settings
from live_pocs.fusion import FusionBackend, deny_permission
from live_pocs.service import ChatMessage, LivePocError, SessionKey


class FakeSession:
    def __init__(self, session_id):
        self.session_id = session_id
        self.handlers = []
        self.prompts = []
        self.aborted = False
        self.disconnected = False

    def on(self, handler):
        self.handlers.append(handler)
        return lambda: self.handlers.remove(handler)

    async def send_and_wait(self, prompt, *, timeout):
        self.prompts.append(prompt)
        return SimpleNamespace(
            data=AssistantMessageData(content="synthetic SDK reply", message_id="fixture-message")
        )

    async def abort(self):
        self.aborted = True

    async def disconnect(self):
        self.disconnected = True


class FakeClient:
    created = []
    creation_error = None

    def __init__(self, **options):
        self.options = options
        self.sessions = []
        self.stopped = False
        self.created.append(self)

    async def start(self):
        pass

    async def create_session(self, **options):
        if self.creation_error:
            raise RuntimeError(self.creation_error)
        session = FakeSession(options["session_id"])
        self.sessions.append((options, session))
        return session

    async def stop(self):
        self.stopped = True

    async def force_stop(self):
        self.stopped = True


def settings():
    return Settings(
        BackendMode.FUSION,
        "FIXTURE_BACKEND_SECRET_MORE_THAN_24",
        live_enabled=True,
        github_token="FIXTURE_GITHUB_TOKEN",
        github_login="fixture-user",
    )


class FusionSdkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeClient.created = []
        FakeClient.creation_error = None
        self.patch = patch("live_pocs.fusion.CopilotClient", FakeClient)
        self.patch.start()
        self.backend = FusionBackend(settings())
        self.identity_patch = patch.object(self.backend, "_verify_identity", new=AsyncMock())
        self.identity_patch.start()

    async def asyncTearDown(self):
        await self.backend.aclose()
        self.identity_patch.stop()
        self.patch.stop()

    async def test_separate_sdk_clients_and_sessions_under_explicit_single_identity(self):
        for tenant in ("customer-a", "customer-b"):
            scope = SessionKey(tenant, "assistant", "same", "chat", tenant + "-internal")
            reply = await self.backend.infer(scope, "synthetic", (), "run")
            self.assertEqual(reply.requested_model, "fusion")
            self.assertEqual(reply.sdk_session_id, scope.internal_id)
        self.assertEqual(len(FakeClient.created), 2)
        for client in FakeClient.created:
            self.assertEqual(client.options["mode"], "empty")
            self.assertFalse(client.options["use_logged_in_user"])
            options, _ = client.sessions[0]
            self.assertEqual(options["model"], "fusion")
            self.assertEqual(options["github_token"], "FIXTURE_GITHUB_TOKEN")
            self.assertEqual(options["available_tools"], [])
            self.assertIs(options["on_permission_request"], deny_permission)
            self.assertFalse(options["enable_config_discovery"])
            self.assertFalse(options["enable_skills"])
            self.assertNotIn("provider", options)

    async def test_actual_observed_feature_rejection_is_explicit_not_a_model_fallback(self):
        FakeClient.creation_error = "The Copilot Fusion model alias requires dynamic Auto tiers."
        with self.assertLogs("model_service.live", level="ERROR"):
            with self.assertRaisesRegex(LivePocError, "fusion_dynamic_tiers_unavailable"):
                await self.backend.infer(
                    SessionKey("customer-a", "assistant", "s", "chat", "internal"),
                    "synthetic", (), "run",
                )
        self.assertFalse(self.backend._sessions)

    async def test_missing_sdk_state_never_replays_as_a_fresh_conversation(self):
        with self.assertRaisesRegex(LivePocError, "sdk_session_unavailable"):
            await self.backend.infer(
                SessionKey("customer-a", "assistant", "s", "chat", "missing"),
                "second", (ChatMessage("user", "first"),), "run",
            )
        self.assertFalse(FakeClient.created)
