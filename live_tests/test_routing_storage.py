import asyncio
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables.aio import TableClient

from live_pocs.routing import BINDING_LIFETIME, RegistrySettings, SessionBinding
from live_pocs.routing_storage import AzureTableRegistry, create_registry
from live_pocs.service import LivePocError


def binding():
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    return SessionBinding(
        application_id="fixture-app", tenant_id="customer-a", agent_id="assistant",
        session_id="same", sdk_session_id="server-generated-id", model_alias="economy",
        endpoint_id="small-v1", endpoint_fingerprint="a" * 64, model_name="small",
        model_version="2026-01-01", created_at=now, expires_at=now + BINDING_LIFETIME,
    )


class TableLookupNotFound(ResourceNotFoundError):
    def __init__(self, code: str | None) -> None:
        super().__init__("Synthetic not-found")
        self.error_code = code


def not_found(code: str | None = "ResourceNotFound") -> TableLookupNotFound:
    return TableLookupNotFound(code)


class AzureRegistryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncMock(spec=TableClient)
        self.registry = AzureTableRegistry(self.client)
        self.binding = binding()

    async def test_create_uses_insert_not_upsert_and_preserves_no_prompt_or_credential(self):
        result, created = await self.registry.create_or_get(self.binding)
        self.assertTrue(created)
        self.assertEqual(result, self.binding)
        entity = self.client.create_entity.call_args.args[0]
        self.assertEqual(set(entity), {"PartitionKey", "RowKey", "Binding"})
        self.assertEqual(
            (entity["PartitionKey"], entity["RowKey"]), self.binding.scope.storage_keys(),
        )
        self.assertEqual(await self.registry.create_or_get(self.binding), (self.binding, True))
        self.client.upsert_entity.assert_not_called()
        self.client.update_entity.assert_not_called()

    async def test_conflicting_creation_returns_existing_winner_without_mutating_expiry(self):
        winner = self.binding.model_copy(update={"sdk_session_id": "other-winner"})
        self.client.create_entity.side_effect = ResourceExistsError("Synthetic conflict")
        self.client.get_entity.return_value = {"Binding": winner.model_dump_json()}
        result, created = await self.registry.create_or_get(self.binding)
        self.assertFalse(created)
        self.assertEqual(result, winner)
        self.assertEqual(result.expires_at, self.binding.expires_at)
        self.client.update_entity.assert_not_called()

    async def test_missing_entity_is_not_a_missing_table_or_credential_failure(self):
        self.client.get_entity.side_effect = not_found()
        self.assertIsNone(await self.registry.get(self.binding.scope))
        for error in (not_found("TableNotFound"), HttpResponseError("Synthetic denied"), not_found(None)):
            self.client.get_entity.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertLogs("model_service.live", level="ERROR"):
                with self.assertRaisesRegex(LivePocError, "routing_registry_unavailable"):
                    await self.registry.get(self.binding.scope)

    async def test_unresolved_conflict_corrupt_json_and_wrong_owner_fail_closed(self):
        self.client.create_entity.side_effect = ResourceExistsError("Synthetic conflict")
        self.client.get_entity.side_effect = not_found()
        with self.assertLogs("model_service.live", level="ERROR"):
            with self.assertRaisesRegex(LivePocError, "routing_registry_unavailable"):
                await self.registry.create_or_get(self.binding)
        self.client.get_entity.side_effect = None
        for raw in ("not-json", self.binding.model_copy(update={"tenant_id": "customer-b"}).model_dump_json()):
            self.client.get_entity.return_value = {"Binding": raw}
            with self.subTest(raw=raw), self.assertLogs("model_service.live", level="ERROR"):
                with self.assertRaisesRegex(LivePocError, "routing_registry_corrupt"):
                    await self.registry.get(self.binding.scope)

    async def test_uncertain_insert_does_not_fall_back_to_another_backend_or_local_store(self):
        self.client.create_entity.side_effect = HttpResponseError("Synthetic transport failure")
        with self.assertLogs("model_service.live", level="ERROR"):
            with self.assertRaisesRegex(LivePocError, "routing_registry_unavailable"):
                await self.registry.create_or_get(self.binding)
        self.client.upsert_entity.assert_not_called()

    async def test_credential_is_closed_even_if_table_client_cleanup_fails(self):
        credential = AsyncMock()
        registry = AzureTableRegistry(self.client, credential)
        self.client.close.side_effect = RuntimeError("Synthetic close failure")
        with self.assertRaisesRegex(RuntimeError, "close failure"):
            await registry.aclose()
        credential.close.assert_awaited_once()

    async def test_two_instances_observe_one_atomic_azure_insert(self):
        entities = {}

        async def create(entity):
            key = (entity["PartitionKey"], entity["RowKey"])
            if key in entities:
                raise ResourceExistsError("Synthetic atomic conflict")
            entities[key] = entity
            await asyncio.sleep(0)
            return {}

        async def get(*, partition_key, row_key):
            return entities[(partition_key, row_key)]

        self.client.create_entity.side_effect = create
        self.client.get_entity.side_effect = get
        other = AzureTableRegistry(self.client)
        candidate = self.binding.model_copy(update={"sdk_session_id": "other-candidate"})
        results = await asyncio.gather(
            self.registry.create_or_get(self.binding), other.create_or_get(candidate),
        )
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len({result.sdk_session_id for result, _ in results}), 1)


class AzureRegistryFactoryTests(unittest.TestCase):
    def test_production_uses_managed_identity_no_implicit_table_creation_or_sdk_retries(self):
        with patch("live_pocs.routing_storage.ManagedIdentityCredential") as managed, patch(
            "live_pocs.routing_storage.DefaultAzureCredential",
        ) as development, patch("live_pocs.routing_storage.TableClient") as table:
            settings = RegistrySettings(
                backend="azure_table", azure_endpoint="https://fixture.table.core.windows.net",
                managed_identity_client_id="11111111-1111-1111-1111-111111111111",
            )
            result = create_registry(settings)
            self.assertIsInstance(result, AzureTableRegistry)
            managed.assert_called_once_with(client_id=settings.managed_identity_client_id)
            development.assert_not_called()
            self.assertEqual(table.call_args.kwargs["retry_total"], 0)
            table.return_value.create_table.assert_not_called()
