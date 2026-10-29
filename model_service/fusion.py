"""Mock-only Copilot SDK boundary; no SDK import, network, or Fusion algorithm."""

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from types import MappingProxyType, TracebackType
from typing import Literal

from .authorization import authorize_request, valid_identifier
from .contracts import ModelServiceError, Request

type Scope = tuple[str, str, str, str]
type AuditEntry = dict[str, str | bool]


@dataclass(frozen=True)
class FusionCustomer:
    tenant_id: str
    github_identity: str
    caller_token: str = field(repr=False)
    github_token: str = field(repr=False)


@dataclass(frozen=True)
class FusionResult:
    content: str = field(repr=False)
    sdk_session_id: str
    turn: int


@dataclass(frozen=True)
class MockModelInfo:
    id: str
    name: str


@dataclass(frozen=True)
class MockMessageData:
    content: str = field(repr=False)


@dataclass(frozen=True)
class MockMessage:
    data: MockMessageData


@dataclass
class MockFusionAccount:
    tenant_id: str
    github_identity: str
    expected_github_token: str = field(repr=False)
    available_models: tuple[str, ...] = ("fusion",)
    send_error: str | None = None
    response_kind: Literal["message", "none", "empty"] = "message"
    response_delay: float = 0
    audit: list[AuditEntry] = field(default_factory=list)

    def authenticate(self, token: str) -> None:
        if not hmac.compare_digest(token, self.expected_github_token):
            raise ModelServiceError("copilot_unauthenticated")


class MockFusionSession:
    def __init__(
        self, account: MockFusionAccount, session_id: str, github_token: str
    ) -> None:
        self.account = account
        self.session_id = session_id
        self._github_token = github_token
        self._prompts: list[str] = []
        self.closed = False

    async def send_and_wait(self, prompt: str) -> MockMessage | None:
        if self.closed:
            raise ModelServiceError("sdk_session_closed")
        self.account.authenticate(self._github_token)
        if "fusion" not in self.account.available_models:
            raise ModelServiceError("fusion_unavailable")
        self.account.audit.append({"event": "send", "session_id": self.session_id})
        await asyncio.sleep(self.account.response_delay)
        if self.account.send_error is not None:
            raise ModelServiceError(self.account.send_error)
        if self.account.response_kind == "none":
            return None
        if self.account.response_kind == "empty":
            return MockMessage(MockMessageData(""))
        prompts = [*self._prompts, prompt]
        content = json.dumps(
            {
                "mock": True,
                "tenant": self.account.tenant_id,
                "user_prompts": prompts,
                "model": "fusion",
                "internal_execution_pattern": "not_simulated",
            }
        )
        self._prompts = prompts
        return MockMessage(MockMessageData(content))

    def attempt_tool_for_test(self, name: str) -> None:
        # A fixture probe, not a Copilot SDK method or a real permission handler.
        self.account.audit.append({"event": "denied_tool", "tool": name})
        raise ModelServiceError("tool_not_available")

    async def disconnect(self) -> None:
        self.closed = True
        self._prompts.clear()


class MockFusionClient:
    def __init__(
        self,
        account: MockFusionAccount,
        *,
        github_token: str,
        mode: str,
        use_logged_in_user: bool,
    ) -> None:
        if mode != "empty" or use_logged_in_user:
            raise ModelServiceError("unsafe_sdk_configuration")
        self.account = account
        self._github_token = github_token
        self._models_cache: list[MockModelInfo] | None = None
        self.sessions: dict[str, MockFusionSession] = {}
        self.closed = False
        account.audit.append(
            {"event": "client_create", "mode": mode, "use_logged_in_user": False}
        )

    def _require_active(self) -> None:
        if self.closed:
            raise ModelServiceError("sdk_client_closed")
        self.account.authenticate(self._github_token)

    async def list_models(self) -> list[MockModelInfo]:
        self._require_active()
        if self._models_cache is None:
            self.account.audit.append({"event": "models_list"})
            self._models_cache = [
                MockModelInfo(model, model) for model in self.account.available_models
            ]
        return list(self._models_cache)

    async def create_session(
        self,
        *,
        session_id: str,
        model: str,
        github_token: str,
        available_tools: list[str],
    ) -> MockFusionSession:
        self._require_active()
        self.account.authenticate(github_token)
        if available_tools:
            raise ModelServiceError("tools_not_allowed")
        if model != "fusion" or model not in self.account.available_models:
            raise ModelServiceError("fusion_unavailable")
        if session_id in self.sessions:
            raise ModelServiceError("duplicate_sdk_session")
        session = MockFusionSession(self.account, session_id, github_token)
        self.sessions[session_id] = session
        self.account.audit.append(
            {
                "event": "session_create",
                "session_id": session_id,
                "model": model,
                "explicit_session_token": True,
                "tools_enabled": False,
                "byok_provider": False,
            }
        )
        return session

    async def stop(self) -> None:
        for session in self.sessions.values():
            await session.disconnect()
        self._models_cache = None
        self.closed = True


class FusionService:
    """Local SDK-boundary experiment, not a production or live SDK adapter."""

    def __init__(
        self,
        customers: tuple[FusionCustomer, ...],
        accounts: dict[str, MockFusionAccount],
        *,
        timeout: float = 5,
    ) -> None:
        if not customers or timeout <= 0:
            raise ValueError("Customers and a positive timeout are required")
        for values in (
            [c.tenant_id for c in customers],
            [c.github_identity for c in customers],
            [c.caller_token for c in customers],
            [c.github_token for c in customers],
        ):
            if len(set(values)) != len(values) or any(not value for value in values):
                raise ValueError("Each customer must have a distinct identity and credentials")
        if any(
            not token.isascii()
            for customer in customers
            for token in (customer.caller_token, customer.github_token)
        ):
            raise ValueError("Mock credentials must be ASCII")
        if set(accounts) != {c.tenant_id for c in customers}:
            raise ValueError("Exactly one fixture account is required for each customer")
        for customer in customers:
            account = accounts[customer.tenant_id]
            if (
                not valid_identifier(customer.tenant_id)
                or account.tenant_id != customer.tenant_id
                or account.github_identity != customer.github_identity
            ):
                raise ValueError("Customer and GitHub account ownership must match")
        self._customers = MappingProxyType({c.tenant_id: c for c in customers})
        self._caller_tokens = MappingProxyType({c.tenant_id: c.caller_token for c in customers})
        self._accounts = MappingProxyType(dict(accounts))
        self.clients: dict[str, MockFusionClient] = {}
        self.sessions: dict[Scope, MockFusionSession] = {}
        self._locks: dict[Scope, asyncio.Lock] = {}
        self._turns: dict[Scope, int] = {}
        self._failed: set[Scope] = set()
        self._closed = False
        self.timeout = timeout

    async def run(self, caller_token: str, request: Request) -> FusionResult:
        tenant = authorize_request(self._caller_tokens, caller_token, request)
        if self._closed:
            raise ModelServiceError("fusion_service_closed")
        customer = self._customers[tenant]
        scope = (tenant, request.agent_id, request.session_id, request.model_alias)
        lock = self._locks.setdefault(scope, asyncio.Lock())
        async with lock:
            if self._closed:
                raise ModelServiceError("fusion_service_closed")
            if scope in self._failed:
                raise ModelServiceError("fusion_session_failed")
            turn = self._turns.get(scope, 0)
            if turn >= 10:
                raise ModelServiceError("session_turn_limit")
            if tenant not in self.clients:
                self.clients[tenant] = MockFusionClient(
                    self._accounts[tenant],
                    github_token=customer.github_token,
                    mode="empty",
                    use_logged_in_user=False,
                )
            client = self.clients[tenant]
            if scope not in self.sessions:
                models = await client.list_models()
                if not any(model.id == "fusion" for model in models):
                    raise ModelServiceError("fusion_unavailable")
                session_id = "fusion-" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()
                self.sessions[scope] = await client.create_session(
                    session_id=session_id,
                    model="fusion",
                    github_token=customer.github_token,
                    available_tools=[],
                )
            session = self.sessions[scope]
            try:
                message = await asyncio.wait_for(
                    session.send_and_wait(request.prompt), timeout=self.timeout
                )
                if message is None or not message.data.content:
                    raise ModelServiceError("invalid_fusion_response")
            except TimeoutError:
                self._failed.add(scope)
                await session.disconnect()
                raise ModelServiceError("fusion_timeout") from None
            except ModelServiceError:
                self._failed.add(scope)
                await session.disconnect()
                raise
            except asyncio.CancelledError:
                self._failed.add(scope)
                await session.disconnect()
                raise
            self._turns[scope] = turn + 1
            return FusionResult(message.data.content, session.session_id, turn + 1)

    async def aclose(self) -> None:
        self._closed = True
        for lock in tuple(self._locks.values()):
            async with lock:
                pass
        for client in self.clients.values():
            await client.stop()
        self.sessions.clear()
        self._turns.clear()


class FusionExperiment:
    def __init__(self) -> None:
        self.accounts: dict[str, MockFusionAccount] = {}
        self.customers: dict[str, FusionCustomer] = {}
        for tenant in ("customer-a", "customer-b"):
            token = secrets.token_urlsafe(24)
            identity = f"fixture-github-{tenant}"
            self.accounts[tenant] = MockFusionAccount(tenant, identity, token)
            self.customers[tenant] = FusionCustomer(
                tenant, identity, secrets.token_urlsafe(24), token
            )
        self.service = FusionService(tuple(self.customers.values()), self.accounts)

    async def __aenter__(self) -> "FusionExperiment":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.service.aclose()


async def run_fusion() -> dict:
    async with FusionExperiment() as experiment:
        async def invoke(customer: FusionCustomer) -> dict:
            start = time.perf_counter()
            request = Request(
                customer.tenant_id, "assistant", "same-session", "chat",
                f"Hello {customer.tenant_id}",
            )
            result = await experiment.service.run(customer.caller_token, request)
            content = json.loads(result.content)
            if (
                content["tenant"] != customer.tenant_id
                or content["user_prompts"] != [request.prompt]
            ):
                raise RuntimeError("Fusion SDK-boundary tenant isolation assertion failed")
            return {
                "tenant": customer.tenant_id,
                "sdk_session_id": result.sdk_session_id,
                "turn": result.turn,
                "elapsed_ms": round((time.perf_counter() - start) * 1000, 2),
            }

        results = await asyncio.gather(*(invoke(c) for c in experiment.customers.values()))
        session_ids = {result["sdk_session_id"] for result in results}
        if len(session_ids) != len(results):
            raise RuntimeError("Fusion SDK session separation assertion failed")
        return {
            "approach": "github-fusion",
            "mode": "local_sdk_mock_only",
            "status": "conditional",
            "model_id": "fusion",
            "tenant_calls": len(results),
            "mock_sdk_clients": len(experiment.service.clients),
            "distinct_sdk_sessions": len(session_ids),
            "requests": results,
            "github_model_calls": 0,
            "fusion_effort_tier": "not_validated",
            "internal_execution_patterns": "not_observed_or_simulated",
            "process_container_isolation": "not_exercised",
            "github_network_compute_isolation": "not_tested",
        }
