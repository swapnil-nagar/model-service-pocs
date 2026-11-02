import hashlib
import hmac
import json
import logging
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from model_service.authorization import valid_identifier

from .service import LivePocError

logger = logging.getLogger("model_service.live")
BINDING_LIFETIME = timedelta(hours=24)
_ENVIRONMENT_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,127}\Z")


def _identifier(value: str) -> str:
    if not valid_identifier(value):
        raise ValueError("An ASCII identifier of 1-64 letters, digits, underscores or hyphens is required")
    return value


def _base_url(value: str, *, local: bool = False) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("A valid HTTPS base URL is required") from None
    loopback = local and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if (
        (parsed.scheme != "https" and not loopback)
        or not parsed.hostname or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or port == 0
        or any(ord(character) < 33 or character == "\\" for character in value)
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
        or "%" in parsed.path
    ):
        raise ValueError("A credential-free HTTPS base URL without query/fragment is required")
    return value.rstrip("/")


class Endpoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    base_url: str
    deployment: str = Field(min_length=1, max_length=128)
    model_name: str = Field(min_length=1, max_length=128)
    model_version: str = Field(min_length=1, max_length=128)
    reported_model: str = Field(min_length=1, max_length=256)
    api_key_env: str
    auth_header: Literal["Authorization", "api-key", "Ocp-Apim-Subscription-Key"] = "Authorization"
    max_output_tokens: int = Field(default=4096, ge=1, le=131072)
    max_prompt_tokens: int = Field(default=32768, ge=1024, le=2_000_000)

    @field_validator("base_url")
    @classmethod
    def endpoint_url(cls, value: str) -> str:
        return _base_url(value)

    @field_validator("api_key_env")
    @classmethod
    def credential_reference(cls, value: str) -> str:
        if not _ENVIRONMENT_NAME.fullmatch(value):
            raise ValueError("An environment-variable credential reference is required")
        return value

    @field_validator("deployment", "model_name", "model_version", "reported_model")
    @classmethod
    def concrete_model(cls, value: str) -> str:
        if (
            not value.isascii() or any(character.isspace() for character in value)
            or value.lower() in {"auto", "fusion", "latest", "default", "model-router"}
            or value.lower().startswith("model-router-")
        ):
            raise ValueError("An explicit non-router model/deployment/version is required")
        return value

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def headers(self) -> dict[str, str]:
        value = os.environ.get(self.api_key_env, "")
        if not value or not value.isascii() or any(character.isspace() for character in value):
            raise LivePocError("endpoint_credential_unavailable", 503)
        return {
            self.auth_header: "Bearer " + value if self.auth_header == "Authorization" else value,
            "Accept-Encoding": "identity",
        }


class CustomerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    api_key_env: str
    allowed_models: tuple[str, ...] = Field(min_length=1)
    allowed_agents: tuple[str, ...] = ("assistant",)

    @field_validator("api_key_env")
    @classmethod
    def credential_reference(cls, value: str) -> str:
        return Endpoint.credential_reference(value)

    @field_validator("allowed_models", "allowed_agents")
    @classmethod
    def identifiers(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(set(values)) != len(values):
            raise ValueError("Nonempty, unique identifiers are required")
        return tuple(_identifier(value) for value in values)


class RegistrySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    backend: Literal["azure_table", "sqlite"]
    sqlite_path: Path | None = None
    azure_endpoint: str | None = None
    table_name: str = "ModelSessionBindings"
    managed_identity_client_id: str | None = None
    development_credential: bool = False

    @model_validator(mode="after")
    def valid_backend(self) -> Self:
        if self.backend == "sqlite":
            if (
                self.sqlite_path is None
                or str(self.sqlite_path) == ":memory:"
                or str(self.sqlite_path).startswith("file:")
                or self.azure_endpoint is not None
            ):
                raise ValueError("SQLite requires an explicit persistent file, not an in-memory/URI database")
        else:
            if self.azure_endpoint is None or self.sqlite_path is not None:
                raise ValueError("Azure Table Storage requires its account endpoint")
            endpoint = _base_url(self.azure_endpoint)
            parsed = urlsplit(endpoint)
            if (
                not (parsed.hostname or "").endswith(".table.core.windows.net")
                or parsed.path not in {"", "/"} or parsed.port not in {None, 443}
            ):
                raise ValueError("An Azure public-cloud Table Storage account endpoint is required")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{2,62}", self.table_name):
                raise ValueError("Invalid Azure table name")
        return self


class RoutingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    application_id: str
    public_base_url: str
    registry: RegistrySettings
    endpoints: dict[str, Endpoint] = Field(min_length=1)
    models: dict[str, str] = Field(min_length=1)
    customers: dict[str, CustomerPolicy] = Field(min_length=1)
    live_enabled: bool = False
    max_request_bytes: int = Field(default=4_194_304, ge=1024, le=16_777_216)
    max_response_bytes: int = Field(default=8_388_608, ge=1024, le=33_554_432)
    upstream_timeout_seconds: float = Field(default=25, gt=0, le=120)

    @field_validator("application_id")
    @classmethod
    def app_identifier(cls, value: str) -> str:
        return _identifier(value)

    @field_validator("public_base_url")
    @classmethod
    def public_url(cls, value: str) -> str:
        return _base_url(value, local=True)

    @model_validator(mode="after")
    def valid_catalog(self) -> Self:
        for value in (*self.endpoints, *self.models, *self.customers):
            _identifier(value)
        if any(endpoint_id not in self.endpoints for endpoint_id in self.models.values()):
            raise ValueError("Every model alias must resolve to a configured endpoint")
        for customer in self.customers.values():
            if not set(customer.allowed_models) <= self.models.keys():
                raise ValueError("Customer model grants must reference configured aliases")
        return self

    @classmethod
    def from_environment(cls) -> Self:
        path = os.environ.get("MODEL_ROUTING_CONFIG", "")
        enabled = os.environ.get("MODEL_ROUTING_LIVE_ENABLED", "false").lower()
        if not path or enabled not in {"true", "false"}:
            raise RuntimeError("MODEL_ROUTING_CONFIG and an explicit true/false live setting are required")
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or "live_enabled" in payload:
                raise ValueError("Live enablement must be supplied separately")
            payload["live_enabled"] = enabled == "true"
            return cls.model_validate(payload)
        except (OSError, ValueError, ValidationError) as exc:
            logger.error("routing_configuration_failed error_type=%s", type(exc).__name__)
            raise RuntimeError("Invalid or unreadable model-routing configuration") from None


@dataclass(frozen=True)
class SessionScope:
    application_id: str
    tenant_id: str
    agent_id: str
    session_id: str

    def storage_keys(self) -> tuple[str, str]:
        def digest(parts: tuple[str, str]) -> str:
            return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode("utf-8")).hexdigest()
        return digest((self.application_id, self.tenant_id)), digest((self.agent_id, self.session_id))


class SessionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    application_id: str
    tenant_id: str
    agent_id: str
    session_id: str
    sdk_session_id: str
    model_alias: str
    endpoint_id: str
    endpoint_fingerprint: str
    model_name: str
    model_version: str
    created_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def valid_record(self) -> Self:
        for value in (
            self.application_id, self.tenant_id, self.agent_id, self.session_id,
            self.model_alias, self.endpoint_id, self.sdk_session_id,
        ):
            _identifier(value)
        if (
            self.created_at.tzinfo is None or self.expires_at.tzinfo is None
            or self.expires_at - self.created_at != BINDING_LIFETIME
            or not re.fullmatch(r"[a-f0-9]{64}", self.endpoint_fingerprint)
        ):
            raise ValueError("Invalid fixed-lifetime session binding")
        return self

    @property
    def scope(self) -> SessionScope:
        return SessionScope(self.application_id, self.tenant_id, self.agent_id, self.session_id)


class BindingRegistry(Protocol):
    async def get(self, scope: SessionScope) -> SessionBinding | None: ...
    async def create_or_get(self, binding: SessionBinding) -> tuple[SessionBinding, bool]: ...
    async def aclose(self) -> None: ...


class SessionRouter:
    def __init__(
        self, settings: RoutingSettings, registry: BindingRegistry,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.clock = clock
        self._customer_keys: dict[str, str] = {}
        for tenant_id, customer in settings.customers.items():
            value = os.environ.get(customer.api_key_env, "")
            if len(value) < 24 or not value.isascii() or any(character.isspace() for character in value):
                raise RuntimeError("Every routing customer requires a distinct ASCII API credential of at least 24 characters")
            digest = hashlib.sha256(value.encode("ascii")).hexdigest()
            if digest in self._customer_keys:
                raise RuntimeError("Customer routing credentials must not be shared")
            self._customer_keys[digest] = tenant_id

    def authenticate(self, authorization: str) -> str:
        scheme, separator, value = authorization.partition(" ")
        if scheme.lower() != "bearer" or not separator or not value.isascii() or len(value) > 1024:
            raise LivePocError("unauthenticated", 401)
        digest = hashlib.sha256(value.encode("ascii")).hexdigest()
        tenant = next(
            (tenant_id for expected, tenant_id in self._customer_keys.items()
             if hmac.compare_digest(expected, digest)),
            None,
        )
        if tenant is None:
            raise LivePocError("unauthenticated", 401)
        return tenant

    def scope(self, tenant_id: str, agent_id: str, session_id: str) -> SessionScope:
        if not valid_identifier(agent_id) or not valid_identifier(session_id):
            raise LivePocError("invalid_session", 422)
        customer = self.settings.customers.get(tenant_id)
        if customer is None or agent_id not in customer.allowed_agents:
            raise LivePocError("agent_not_allowed", 403)
        return SessionScope(self.settings.application_id, tenant_id, agent_id, session_id)

    def endpoint(self, binding: SessionBinding) -> Endpoint:
        if self.clock() >= binding.expires_at:
            raise LivePocError("session_expired", 410)
        customer = self.settings.customers.get(binding.tenant_id)
        if customer is None or binding.model_alias not in customer.allowed_models:
            raise LivePocError("model_not_allowed", 403)
        endpoint = self.settings.endpoints.get(binding.endpoint_id)
        if endpoint is None:
            raise LivePocError("pinned_endpoint_unavailable", 503)
        if endpoint.fingerprint() != binding.endpoint_fingerprint:
            raise LivePocError("pinned_endpoint_changed", 503)
        if endpoint.model_name != binding.model_name or endpoint.model_version != binding.model_version:
            logger.error("routing_registry_corrupt code=model_metadata_mismatch")
            raise LivePocError("routing_registry_corrupt", 503)
        return endpoint

    async def get(self, tenant_id: str, agent_id: str, session_id: str) -> SessionBinding:
        binding = await self.registry.get(self.scope(tenant_id, agent_id, session_id))
        if binding is None:
            raise LivePocError("session_not_found", 404)
        self.endpoint(binding)
        return binding

    async def bind(
        self, tenant_id: str, agent_id: str, session_id: str, model_alias: str,
    ) -> tuple[SessionBinding, bool]:
        scope = self.scope(tenant_id, agent_id, session_id)
        if model_alias not in self.settings.customers[tenant_id].allowed_models:
            raise LivePocError("model_not_allowed", 403)
        existing = await self.registry.get(scope)
        if existing is not None:
            self.endpoint(existing)
            if existing.model_alias != model_alias:
                raise LivePocError("session_model_conflict", 409)
            return existing, False
        endpoint_id = self.settings.models[model_alias]
        endpoint = self.settings.endpoints[endpoint_id]
        now = self.clock()
        candidate = SessionBinding(
            application_id=scope.application_id, tenant_id=tenant_id,
            agent_id=agent_id, session_id=session_id, sdk_session_id=uuid.uuid4().hex,
            model_alias=model_alias, endpoint_id=endpoint_id,
            endpoint_fingerprint=endpoint.fingerprint(), model_name=endpoint.model_name,
            model_version=endpoint.model_version, created_at=now, expires_at=now + BINDING_LIFETIME,
        )
        binding, created = await self.registry.create_or_get(candidate)
        self.endpoint(binding)
        if binding.model_alias != model_alias:
            raise LivePocError("session_model_conflict", 409)
        return binding, created

    def view(self, binding: SessionBinding) -> dict[str, object]:
        endpoint = self.endpoint(binding)
        return {
            "tenant_id": binding.tenant_id, "agent_id": binding.agent_id,
            "session_id": binding.session_id, "sdk_session_id": binding.sdk_session_id,
            "model_alias": binding.model_alias, "model_name": binding.model_name,
            "model_version": binding.model_version,
            "created_at": binding.created_at.isoformat(), "expires_at": binding.expires_at.isoformat(),
            "provider_base_url": (
                f"{self.settings.public_base_url}/sessions/{binding.agent_id}/{binding.session_id}/v1"
            ),
            "wire_api": "completions",
            "provider_limits": {
                "max_prompt_tokens": endpoint.max_prompt_tokens,
                "max_output_tokens": endpoint.max_output_tokens,
            },
            "context_owner": "caller_sdk",
        }
