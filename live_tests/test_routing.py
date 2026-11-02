import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from live_pocs.routing import (
    BINDING_LIFETIME, Endpoint, RegistrySettings, RoutingSettings, SessionRouter,
)
from live_pocs.routing_storage import SQLiteRegistry
from live_pocs.service import LivePocError

CREDENTIALS = {
    "ROUTING_CUSTOMER_A": "SYNTHETIC_CUSTOMER_A_KEY_AT_LEAST_24",
    "ROUTING_CUSTOMER_B": "SYNTHETIC_CUSTOMER_B_KEY_AT_LEAST_24",
    "ROUTING_PROVIDER_SMALL": "SYNTHETIC_PROVIDER_SMALL_KEY",
    "ROUTING_PROVIDER_LARGE": "SYNTHETIC_PROVIDER_LARGE_KEY",
}


def routing_settings(path: Path, *, live_enabled: bool = True) -> RoutingSettings:
    return RoutingSettings.model_validate({
        "application_id": "fixture-app",
        "public_base_url": "https://gateway.invalid/model-service",
        "registry": {"backend": "sqlite", "sqlite_path": str(path)},
        "live_enabled": live_enabled,
        "endpoints": {
            "small-v1": {
                "base_url": "https://small-provider.invalid/v1", "deployment": "small-deployment",
                "model_name": "small-model", "model_version": "2026-01-01",
                "reported_model": "small-model-2026-01-01", "api_key_env": "ROUTING_PROVIDER_SMALL",
            },
            "large-v1": {
                "base_url": "https://large-provider.invalid/openai/v1", "deployment": "large-deployment",
                "model_name": "large-model", "model_version": "2026-02-01",
                "reported_model": "large-model-2026-02-01", "api_key_env": "ROUTING_PROVIDER_LARGE",
                "auth_header": "api-key",
            },
        },
        "models": {"economy": "small-v1", "quality": "large-v1"},
        "customers": {
            "customer-a": {
                "api_key_env": "ROUTING_CUSTOMER_A", "allowed_models": ["economy", "quality"],
                "allowed_agents": ["assistant", "reviewer"],
            },
            "customer-b": {"api_key_env": "ROUTING_CUSTOMER_B", "allowed_models": ["economy"]},
        },
    })


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="routing-tests-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "bindings.sqlite3"
        self.environment = patch.dict(os.environ, CREDENTIALS)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.settings = routing_settings(self.path)
        self.registry = SQLiteRegistry(self.path)
        self.now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
        self.router = SessionRouter(self.settings, self.registry, lambda: self.now)

    async def test_fixed_lifetime_and_idempotent_resume_survive_a_cold_registry(self):
        first, created = await self.router.bind("customer-a", "assistant", "same", "economy")
        self.assertTrue(created)
        self.assertEqual(first.expires_at - first.created_at, BINDING_LIFETIME)
        self.now += timedelta(hours=23)
        other = SessionRouter(self.settings, SQLiteRegistry(self.path), lambda: self.now)
        resumed, created = await other.bind("customer-a", "assistant", "same", "economy")
        self.assertFalse(created)
        self.assertEqual(resumed, first)
        self.assertEqual(resumed.expires_at, first.created_at + timedelta(hours=24))

    async def test_expiry_is_exact_and_never_renews_or_rebinds_the_same_id(self):
        original, _ = await self.router.bind("customer-a", "assistant", "same", "economy")
        self.now = original.expires_at - timedelta(microseconds=1)
        self.assertEqual(await self.router.get("customer-a", "assistant", "same"), original)
        self.now = original.expires_at
        for operation in (
            self.router.get("customer-a", "assistant", "same"),
            self.router.bind("customer-a", "assistant", "same", "economy"),
            self.router.bind("customer-a", "assistant", "same", "quality"),
        ):
            with self.assertRaisesRegex(LivePocError, "session_expired") as raised:
                await operation
            self.assertEqual(raised.exception.status_code, 410)
        fresh, created = await self.router.bind("customer-a", "assistant", "new", "quality")
        self.assertTrue(created)
        self.assertEqual(fresh.expires_at, self.now + timedelta(hours=24))
        self.assertEqual(await self.registry.get(original.scope), original)

    async def test_distinct_tenants_and_agents_cannot_collide_on_public_session_id(self):
        a, _ = await self.router.bind("customer-a", "assistant", "same", "economy")
        with self.assertRaisesRegex(LivePocError, "session_not_found"):
            await self.router.get("customer-b", "assistant", "same")
        b, _ = await self.router.bind("customer-b", "assistant", "same", "economy")
        c, _ = await self.router.bind("customer-a", "reviewer", "same", "economy")
        self.assertEqual(len({a.sdk_session_id, b.sdk_session_id, c.sdk_session_id}), 3)
        self.assertNotEqual(a.scope.storage_keys(), b.scope.storage_keys())

    async def test_concurrent_first_requests_create_one_binding_without_last_writer_wins(self):
        routers = [
            SessionRouter(self.settings, SQLiteRegistry(self.path), lambda: self.now)
            for _ in range(12)
        ]
        results = await asyncio.gather(*(
            router.bind("customer-a", "assistant", "same", "economy") for router in routers
        ))
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({binding.sdk_session_id for binding, _ in results}), 1)
        self.assertEqual(len({binding.endpoint_fingerprint for binding, _ in results}), 1)

    async def test_racing_model_requests_have_one_winner_and_one_explicit_conflict(self):
        other = SessionRouter(self.settings, SQLiteRegistry(self.path), lambda: self.now)
        result = await asyncio.gather(
            self.router.bind("customer-a", "assistant", "race", "economy"),
            other.bind("customer-a", "assistant", "race", "quality"),
            return_exceptions=True,
        )
        successes = [entry for entry in result if isinstance(entry, tuple)]
        failures = [entry for entry in result if isinstance(entry, LivePocError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].code, "session_model_conflict")
        winner = await self.router.get("customer-a", "assistant", "race")
        self.assertEqual(winner, successes[0][0])

    async def test_alias_policy_changes_affect_new_sessions_only(self):
        first, _ = await self.router.bind("customer-a", "assistant", "same", "economy")
        settings = self.settings.model_copy(update={"models": {"economy": "large-v1", "quality": "large-v1"}})
        router = SessionRouter(settings, SQLiteRegistry(self.path), lambda: self.now)
        self.assertEqual(await router.get("customer-a", "assistant", "same"), first)
        fresh, _ = await router.bind("customer-a", "assistant", "fresh", "economy")
        self.assertEqual(fresh.endpoint_id, "large-v1")
        self.assertEqual(first.endpoint_id, "small-v1")
        with self.assertRaisesRegex(LivePocError, "session_model_conflict"):
            await router.bind("customer-a", "assistant", "same", "quality")

    async def test_removed_or_mutated_pinned_endpoint_is_not_a_fallback(self):
        first, _ = await self.router.bind("customer-a", "assistant", "same", "economy")
        for changes in (
            {"base_url": "https://different-provider.invalid/v1"},
            {"deployment": "another-deployment"},
            {"model_version": "2026-02-02"},
            {"reported_model": "another-model"},
        ):
            with self.subTest(changes=changes):
                endpoint = self.settings.endpoints["small-v1"].model_copy(update=changes)
                config = self.settings.model_copy(update={
                    "endpoints": {**self.settings.endpoints, "small-v1": endpoint},
                })
                with self.assertRaisesRegex(LivePocError, "pinned_endpoint_changed"):
                    await SessionRouter(config, self.registry, lambda: self.now).get("customer-a", "assistant", "same")
        config = self.settings.model_copy(update={"endpoints": {"large-v1": self.settings.endpoints["large-v1"]}})
        with self.assertRaisesRegex(LivePocError, "pinned_endpoint_unavailable"):
            await SessionRouter(config, self.registry, lambda: self.now).get("customer-a", "assistant", "same")
        self.assertEqual(await self.registry.get(first.scope), first)

    async def test_binding_can_be_read_from_a_different_python_process(self):
        binding, _ = await self.router.bind("customer-a", "assistant", "same", "economy")
        script = (
            "import asyncio,sys; from pathlib import Path; "
            "from live_pocs.routing import SessionScope; "
            "from live_pocs.routing_storage import SQLiteRegistry; "
            "record=asyncio.run(SQLiteRegistry(Path(sys.argv[1])).get("
            "SessionScope('fixture-app','customer-a','assistant','same'))); "
            "assert record is not None; print(record.sdk_session_id)"
        )
        output = await asyncio.to_thread(
            subprocess.check_output, [sys.executable, "-B", "-c", script, str(self.path)],
            text=True, timeout=30,
        )
        self.assertEqual(output.strip(), binding.sdk_session_id)

    async def test_registry_failures_are_explicit_and_never_create_local_default_state(self):
        registry = AsyncMock()
        registry.get.side_effect = LivePocError("routing_registry_unavailable", 503)
        with self.assertRaisesRegex(LivePocError, "routing_registry_unavailable"):
            await SessionRouter(self.settings, registry).bind("customer-a", "assistant", "same", "economy")
        registry.create_or_get.assert_not_called()

    async def test_corrupt_or_cross_owned_database_record_fails_closed(self):
        binding, _ = await self.router.bind("customer-a", "assistant", "same", "economy")
        damaged = json.loads(binding.model_dump_json())
        damaged["tenant_id"] = "customer-b"
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE session_bindings SET payload=?", (json.dumps(damaged),))
        with self.assertLogs("model_service.live", level="ERROR"):
            with self.assertRaisesRegex(LivePocError, "routing_registry_corrupt"):
                await self.router.get("customer-a", "assistant", "same")

    async def test_permissions_credentials_and_public_metadata_are_scoped(self):
        for authorization in ("", "Basic secret", "Bearer invalid", "Bearer \N{SNOWMAN}"):
            with self.subTest(authorization=authorization), self.assertRaisesRegex(LivePocError, "unauthenticated"):
                self.router.authenticate(authorization)
        self.assertEqual(
            self.router.authenticate("Bearer " + CREDENTIALS["ROUTING_CUSTOMER_A"]), "customer-a",
        )
        with self.assertRaisesRegex(LivePocError, "model_not_allowed"):
            await self.router.bind("customer-b", "assistant", "same", "quality")
        with self.assertRaisesRegex(LivePocError, "agent_not_allowed"):
            await self.router.bind("customer-b", "reviewer", "same", "economy")
        binding, _ = await self.router.bind("customer-a", "assistant", "same", "economy")
        metadata = self.router.view(binding)
        self.assertEqual(metadata["wire_api"], "completions")
        self.assertEqual(metadata["provider_base_url"], "https://gateway.invalid/model-service/sessions/assistant/same/v1")
        text = json.dumps(metadata) + binding.model_dump_json() + repr(self.settings)
        self.assertFalse(any(value in text for value in CREDENTIALS.values()))
        self.assertNotIn("small-provider.invalid", json.dumps(metadata))


class RoutingConfigurationTests(unittest.TestCase):
    def test_dynamic_model_and_untrusted_url_configuration_is_rejected(self):
        template = {
            "base_url": "https://provider.invalid/v1", "deployment": "fixed-deployment",
            "model_name": "fixed-model", "model_version": "2026-01-01",
            "reported_model": "fixed-model-2026-01-01", "api_key_env": "FIXTURE_PROVIDER_KEY",
        }
        for changes in (
            {"model_name": "fusion"}, {"deployment": "auto"}, {"model_version": "latest"},
            {"model_name": "model-router-2025-11-18"},
            {"base_url": "http://provider.invalid/v1"},
            {"base_url": "https://user:secret@provider.invalid/v1"},
            {"base_url": "https://provider.invalid/v1?key=secret"},
            {"base_url": "https://provider.invalid/v1/../other"},
            {"api_key_env": "RAW SECRET"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                Endpoint.model_validate({**template, **changes})

    def test_registry_configuration_requires_durable_storage_and_correct_cloud_scope(self):
        for payload in (
            {"backend": "sqlite", "sqlite_path": ":memory:"},
            {"backend": "sqlite"},
            {"backend": "azure_table", "azure_endpoint": "http://account.table.core.windows.net"},
            {"backend": "azure_table", "azure_endpoint": "https://other.invalid"},
            {"backend": "azure_table", "azure_endpoint": "https://a.table.core.windows.net", "table_name": "a-b"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                RegistrySettings.model_validate(payload)
