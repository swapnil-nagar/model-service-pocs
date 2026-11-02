import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal, Protocol

from model_service.authorization import validate_request
from model_service.contracts import ModelServiceError, Request

from .config import Settings

logger = logging.getLogger("model_service.live")
type RunStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class LivePocError(RuntimeError):
    def __init__(self, code: str, status_code: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str = field(repr=False)


@dataclass(frozen=True)
class SessionKey:
    tenant_id: str
    agent_id: str
    session_id: str
    model_alias: str
    internal_id: str


@dataclass(frozen=True)
class ModelReply:
    content: str = field(repr=False)
    requested_model: str
    reported_models: tuple[str, ...] = ()
    deployment: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    sdk_session_id: str | None = None
    fusion_patterns: tuple[str, ...] = ()

    def view(self) -> dict[str, object]:
        return {
            "content": self.content,
            "requested_model": self.requested_model,
            "reported_models": list(self.reported_models),
            "deployment": self.deployment,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "usage_reported": self.input_tokens is not None or self.output_tokens is not None,
            "sdk_session_id": self.sdk_session_id,
            "fusion_patterns": list(self.fusion_patterns),
            "fusion_attribution_observed": bool(self.fusion_patterns),
        }


class InferenceBackend(Protocol):
    async def infer(
        self,
        scope: SessionKey,
        prompt: str,
        history: tuple[ChatMessage, ...],
        invocation_id: str,
    ) -> ModelReply: ...

    async def forget(self, scope: SessionKey) -> None: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class RunRequest:
    session_id: str
    prompt: str = field(repr=False)
    agent_id: str = "assistant"
    model_alias: str = "chat"
    new_session: bool = False


@dataclass
class _Session:
    scope: SessionKey
    history: tuple[ChatMessage, ...] = field(default=(), repr=False)
    turns: int = 0
    pending: int = 0
    failed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


@dataclass
class _Run:
    run_id: str
    session: _Session
    request: RunRequest = field(repr=False)
    instance_id: str
    status: RunStatus = "queued"
    reply: ModelReply | None = field(default=None, repr=False)
    error_code: str | None = None
    cleanup_failed: bool = False
    completed_turn: int | None = None
    elapsed_ms: float | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    created_at: float = field(default_factory=time.perf_counter, repr=False)
    finished: bool = False

    def view(self, provider: str) -> dict[str, object]:
        scope = self.session.scope
        return {
            "run_id": self.run_id,
            "instance_id": self.instance_id,
            "tenant_id": scope.tenant_id,
            "agent_id": scope.agent_id,
            "session_id": scope.session_id,
            "model_alias": scope.model_alias,
            "provider": provider,
            "status": self.status,
            "turn": self.completed_turn,
            "elapsed_ms": self.elapsed_ms,
            "result": self.reply.view() if self.reply else None,
            "error": {"code": self.error_code} if self.error_code else None,
            "cleanup_failed": self.cleanup_failed,
            "state_durability": "ephemeral_single_replica",
        }


class RunService:
    def __init__(self, settings: Settings, backend: InferenceBackend) -> None:
        self.settings = settings
        self.backend = backend
        self.instance_id = uuid.uuid4().hex
        self._sessions: dict[tuple[str, str, str, str], _Session] = {}
        self._runs: dict[str, _Run] = {}
        self._pending = 0
        self._closed = False

    def submit(self, tenant_id: str, request: RunRequest) -> dict[str, object]:
        if self._closed:
            raise LivePocError("runner_closed", 503)
        if not self.settings.live_enabled:
            raise LivePocError("live_inference_disabled", 503)
        if tenant_id not in self.settings.allowed_tenants:
            raise LivePocError("tenant_not_allowed", 403)
        try:
            validate_request(
                Request(
                    tenant_id, request.agent_id, request.session_id,
                    request.model_alias, request.prompt,
                )
            )
        except ModelServiceError as exc:
            status = 403 if exc.code in {"agent_not_allowed", "model_not_allowed"} else 422
            raise LivePocError(exc.code, status) from None
        identity = (tenant_id, request.agent_id, request.session_id, request.model_alias)
        session = self._sessions.get(identity)
        if request.new_session:
            if session is not None:
                raise LivePocError("session_already_exists", 409)
            count = sum(key[0] == tenant_id for key in self._sessions)
            if count >= self.settings.max_sessions:
                raise LivePocError("session_limit", 429)
        elif session is None:
            raise LivePocError("session_not_found", 404)
        if self._pending >= self.settings.max_pending_runs:
            raise LivePocError("pending_run_limit", 429)
        if session is None:
            session = _Session(
                SessionKey(
                    tenant_id, request.agent_id, request.session_id,
                    request.model_alias, uuid.uuid4().hex,
                )
            )
            self._sessions[identity] = session
        if session.failed:
            raise LivePocError("session_quarantined", 409)
        if session.turns + session.pending >= self.settings.max_turns:
            raise LivePocError("session_turn_limit", 429)
        run = _Run(uuid.uuid4().hex, session, request, self.instance_id)
        self._runs[run.run_id] = run
        self._pending += 1
        session.pending += 1
        run.task = asyncio.create_task(self._execute(run), name=f"poc-run-{run.run_id}")
        return run.view(self.settings.mode.value)

    def get(self, tenant_id: str, run_id: str) -> dict[str, object]:
        return self._owned_run(tenant_id, run_id).view(self.settings.mode.value)

    async def cancel(self, tenant_id: str, run_id: str) -> None:
        run = self._owned_run(tenant_id, run_id)
        if run.status not in {"queued", "running"} or run.task is None:
            raise LivePocError("run_not_running", 409)
        await self._cancel_run(run)

    async def _cancel_run(self, run: _Run) -> None:
        if run.task is None or run.finished:
            return
        run.task.cancel()
        try:
            await run.task
        except asyncio.CancelledError:
            pass
        if not run.finished:
            run.error_code = "run_cancelled"
            try:
                await self._quarantine(run)
            finally:
                run.status = "cancelled"
                self._finish(run)

    def _owned_run(self, tenant_id: str, run_id: str) -> _Run:
        run = self._runs.get(run_id)
        if run is None or run.session.scope.tenant_id != tenant_id:
            raise LivePocError("run_not_found", 404)
        return run

    async def _quarantine(self, run: _Run) -> None:
        run.session.failed = True
        try:
            await asyncio.wait_for(self.backend.forget(run.session.scope), timeout=10)
        except asyncio.CancelledError:
            run.cleanup_failed = True
            logger.error("poc_cleanup_interrupted run_id=%s", run.run_id)
            raise
        except Exception as exc:
            run.cleanup_failed = True
            logger.error(
                "poc_cleanup_failed run_id=%s error_type=%s",
                run.run_id, type(exc).__name__,
            )

    async def _execute(self, run: _Run) -> None:
        try:
            async with asyncio.timeout(self.settings.run_timeout_seconds):
                async with run.session.lock:
                    if run.session.failed:
                        raise LivePocError("session_quarantined", 409)
                    run.status = "running"
                    reply = await self.backend.infer(
                        run.session.scope,
                        run.request.prompt,
                        run.session.history,
                        run.run_id,
                    )
                    if run.session.failed:
                        raise LivePocError("session_quarantined", 409)
                    if not reply.content.strip():
                        raise LivePocError("empty_provider_response")
                    if len(reply.content) > 32768:
                        raise LivePocError("provider_response_too_large")
                    run.session.history = (
                        *run.session.history,
                        ChatMessage("user", run.request.prompt),
                        ChatMessage("assistant", reply.content),
                    )
                    run.session.turns += 1
                    run.completed_turn = run.session.turns
                    run.reply = reply
                    run.status = "succeeded"
        except asyncio.CancelledError:
            run.error_code = "run_cancelled"
            await self._quarantine(run)
            run.status = "cancelled"
        except TimeoutError:
            run.error_code = "run_timeout"
            await self._quarantine(run)
            run.status = "failed"
        except LivePocError as exc:
            run.error_code = exc.code
            await self._quarantine(run)
            run.status = "failed"
        except Exception as exc:
            run.error_code = "provider_error"
            logger.error(
                "poc_provider_error run_id=%s error_type=%s",
                run.run_id, type(exc).__name__,
            )
            await self._quarantine(run)
            run.status = "failed"
        finally:
            if run.error_code and run.status in {"queued", "running"}:
                run.status = "cancelled" if run.error_code == "run_cancelled" else "failed"
            self._finish(run)

    def _finish(self, run: _Run) -> None:
        if run.finished:
            return
        run.finished = True
        self._pending -= 1
        run.session.pending -= 1
        run.elapsed_ms = round((time.perf_counter() - run.created_at) * 1000, 2)
        logger.info(
            "poc_run_finished run_id=%s tenant_id=%s provider=%s status=%s error_code=%s",
            run.run_id, run.session.scope.tenant_id,
            self.settings.mode.value, run.status, run.error_code,
        )

    async def aclose(self) -> None:
        self._closed = True
        pending = [run for run in self._runs.values() if not run.finished]
        if pending:
            await asyncio.gather(*(self._cancel_run(run) for run in pending))
        await self.backend.aclose()
