import asyncio
import logging
import sqlite3
from contextlib import closing
from pathlib import Path

from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError, ResourceExistsError, ResourceNotFoundError
from azure.data.tables.aio import TableClient
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from pydantic import ValidationError

from .routing import BindingRegistry, RegistrySettings, SessionBinding, SessionScope
from .service import LivePocError

logger = logging.getLogger("model_service.live")


def _decode(raw: object, scope: SessionScope) -> SessionBinding:
    try:
        if not isinstance(raw, str):
            raise ValueError("Missing binding payload")
        binding = SessionBinding.model_validate_json(raw)
        if binding.scope != scope:
            raise ValueError("Binding owner does not match the storage key")
        return binding
    except (ValueError, ValidationError) as exc:
        logger.error("routing_registry_corrupt error_type=%s", type(exc).__name__)
        raise LivePocError("routing_registry_corrupt", 503) from None


class SQLiteRegistry:
    def __init__(self, path: Path) -> None:
        if str(path) == ":memory:" or str(path).startswith("file:"):
            raise ValueError("A persistent SQLite file is required")
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS session_bindings ("
                    "partition_key TEXT NOT NULL, row_key TEXT NOT NULL, payload TEXT NOT NULL, "
                    "PRIMARY KEY (partition_key, row_key))"
                )
        except sqlite3.Error as exc:
            logger.error("routing_registry_failed error_type=%s", type(exc).__name__)
            raise LivePocError("routing_registry_unavailable", 503) from None

    def _get(self, scope: SessionScope) -> SessionBinding | None:
        try:
            with closing(sqlite3.connect(self.path, timeout=10)) as connection:
                row = connection.execute(
                    "SELECT payload FROM session_bindings WHERE partition_key=? AND row_key=?",
                    scope.storage_keys(),
                ).fetchone()
            return None if row is None else _decode(row[0], scope)
        except sqlite3.Error as exc:
            logger.error("routing_registry_failed error_type=%s", type(exc).__name__)
            raise LivePocError("routing_registry_unavailable", 503) from None

    async def get(self, scope: SessionScope) -> SessionBinding | None:
        return await asyncio.to_thread(self._get, scope)

    def _create_or_get(self, binding: SessionBinding) -> tuple[SessionBinding, bool]:
        try:
            with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO session_bindings (partition_key, row_key, payload) VALUES (?, ?, ?) "
                    "ON CONFLICT(partition_key, row_key) DO NOTHING",
                    (*binding.scope.storage_keys(), binding.model_dump_json()),
                )
                row = connection.execute(
                    "SELECT payload FROM session_bindings WHERE partition_key=? AND row_key=?",
                    binding.scope.storage_keys(),
                ).fetchone()
                if row is None:
                    raise sqlite3.DatabaseError("The binding insert could not be reconciled")
                return _decode(row[0], binding.scope), cursor.rowcount == 1
        except sqlite3.Error as exc:
            logger.error("routing_registry_failed error_type=%s", type(exc).__name__)
            raise LivePocError("routing_registry_unavailable", 503) from None

    async def create_or_get(self, binding: SessionBinding) -> tuple[SessionBinding, bool]:
        return await asyncio.to_thread(self._create_or_get, binding)

    async def aclose(self) -> None:
        pass


class AzureTableRegistry:
    def __init__(self, client: TableClient, credential: AsyncTokenCredential | None = None) -> None:
        self.client = client
        self.credential = credential

    async def get(self, scope: SessionScope) -> SessionBinding | None:
        partition, row = scope.storage_keys()
        try:
            entity = await self.client.get_entity(partition_key=partition, row_key=row)
        except ResourceNotFoundError as exc:
            if getattr(exc, "error_code", None) in {"ResourceNotFound", "EntityNotFound"}:
                return None
            logger.error("routing_registry_failed error_type=%s", type(exc).__name__)
            raise LivePocError("routing_registry_unavailable", 503) from None
        except AzureError as exc:
            logger.error("routing_registry_failed error_type=%s", type(exc).__name__)
            raise LivePocError("routing_registry_unavailable", 503) from None
        return _decode(entity.get("Binding"), scope)

    async def create_or_get(self, binding: SessionBinding) -> tuple[SessionBinding, bool]:
        partition, row = binding.scope.storage_keys()
        try:
            await self.client.create_entity({
                "PartitionKey": partition, "RowKey": row, "Binding": binding.model_dump_json(),
            })
            return binding, True
        except ResourceExistsError:
            winner = await self.get(binding.scope)
            if winner is None:
                logger.error("routing_registry_create_conflict_unresolved")
                raise LivePocError("routing_registry_unavailable", 503)
            return winner, False
        except AzureError as exc:
            logger.error("routing_registry_failed error_type=%s", type(exc).__name__)
            raise LivePocError("routing_registry_unavailable", 503) from None

    async def aclose(self) -> None:
        try:
            await self.client.close()
        finally:
            if self.credential is not None:
                await self.credential.close()


def create_registry(settings: RegistrySettings) -> BindingRegistry:
    if settings.backend == "sqlite":
        if settings.sqlite_path is None:
            raise ValueError("SQLite path is required")
        return SQLiteRegistry(settings.sqlite_path)
    if settings.azure_endpoint is None:
        raise ValueError("Azure Table endpoint is required")
    credential = (
        DefaultAzureCredential()
        if settings.development_credential
        else ManagedIdentityCredential(client_id=settings.managed_identity_client_id)
    )
    client = TableClient(
        endpoint=settings.azure_endpoint, table_name=settings.table_name,
        credential=credential, retry_total=0, logging_enable=False,
    )
    return AzureTableRegistry(client, credential)
