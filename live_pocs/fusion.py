import asyncio
import logging
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from copilot import CopilotClient, RuntimeConnection
from copilot.generated.rpc import PermissionDecisionReject
from copilot.session import (
    CopilotSession,
    PermissionInvocation,
    PermissionRequest,
    PermissionRequestResult,
    SystemMessageReplaceConfig,
)
from copilot.session_events import (
    AssistantMessageData,
    AssistantUsageData,
    SessionErrorData,
    SessionEvent,
    ToolExecutionStartData,
)

from .config import Settings
from .foundry import SYSTEM_INSTRUCTIONS
from .service import ChatMessage, LivePocError, ModelReply, SessionKey

logger = logging.getLogger("model_service.live")


def configure_sdk_logging() -> None:
    sdk_logger = logging.getLogger("copilot")
    sdk_logger.handlers = [logging.NullHandler()]
    sdk_logger.propagate = False


def deny_permission(
    request: PermissionRequest, invocation: PermissionInvocation
) -> PermissionRequestResult:
    return PermissionDecisionReject(feedback="Tools and host operations are disabled in this POC.")


def _total(values: list[int | None]) -> int | None:
    if not values:
        return None
    result = 0
    for value in values:
        if value is None:
            return None
        result += value
    return result


async def stop_client(client: CopilotClient) -> None:
    try:
        await asyncio.wait_for(client.stop(), timeout=10)
    except Exception as exc:
        logger.error("poc_sdk_stop_failed error_type=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(client.force_stop(), timeout=5)
        except Exception as forced:
            logger.error("poc_sdk_force_stop_failed error_type=%s", type(forced).__name__)
            raise LivePocError("sdk_shutdown_failed", 503) from None


class FusionBackend:
    def __init__(self, settings: Settings) -> None:
        configure_sdk_logging()
        self.settings = settings
        self._root = TemporaryDirectory(prefix="live-copilot-")
        self._clients: dict[str, CopilotClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sessions: dict[str, CopilotSession] = {}
        self._identity_lock = asyncio.Lock()
        self._identity_verified = False

    async def _verify_identity(self) -> None:
        async with self._identity_lock:
            if self._identity_verified:
                return
            try:
                async with httpx.AsyncClient(
                    timeout=15, follow_redirects=False, trust_env=False
                ) as client:
                    response = await client.get(
                        "https://api.github.com/user",
                        headers={
                            "Authorization": f"Bearer {self.settings.github_token}",
                            "Accept": "application/vnd.github+json",
                            "X-GitHub-Api-Version": "2022-11-28",
                        },
                    )
            except httpx.RequestError:
                raise LivePocError("github_identity_unavailable", 503) from None
            if response.status_code != 200:
                raise LivePocError("github_authentication_failed", 503)
            try:
                data = response.json()
            except ValueError:
                raise LivePocError("github_identity_invalid_response", 503) from None
            if not isinstance(data, dict) or data.get("login") != self.settings.github_login:
                raise LivePocError("github_identity_mismatch", 503)
            self._identity_verified = True

    async def _client(self, tenant_id: str) -> CopilotClient:
        lock = self._locks.setdefault(tenant_id, asyncio.Lock())
        async with lock:
            existing = self._clients.get(tenant_id)
            if existing is not None:
                return existing
            await self._verify_identity()
            directory = Path(self._root.name) / tenant_id
            directory.mkdir(parents=True, exist_ok=True)
            client = CopilotClient(
                connection=RuntimeConnection.for_stdio(),
                mode="empty",
                github_token=self.settings.github_token,
                use_logged_in_user=False,
                base_directory=str(directory),
                working_directory=str(directory),
                log_level="none",
                telemetry=None,
            )
            try:
                await asyncio.wait_for(client.start(), timeout=30)
            except LivePocError:
                await stop_client(client)
                raise
            except Exception as exc:
                logger.error("poc_sdk_start_failed error_type=%s", type(exc).__name__)
                await stop_client(client)
                raise LivePocError("fusion_runtime_start_failed", 503) from None
            self._clients[tenant_id] = client
            return client

    async def infer(
        self,
        scope: SessionKey,
        prompt: str,
        history: tuple[ChatMessage, ...],
        invocation_id: str,
    ) -> ModelReply:
        session = self._sessions.get(scope.internal_id)
        if session is None:
            if history:
                raise LivePocError("sdk_session_unavailable", 409)
            client = await self._client(scope.tenant_id)
            try:
                session = await client.create_session(
                    model=self.settings.github_model,
                    session_id=scope.internal_id,
                    github_token=self.settings.github_token,
                    on_permission_request=deny_permission,
                    available_tools=[],
                    skip_custom_instructions=True,
                    enable_config_discovery=False,
                    enable_skills=False,
                    enable_file_hooks=False,
                    enable_host_git_operations=False,
                    enable_session_store=False,
                    working_directory=str(Path(self._root.name) / scope.tenant_id),
                    system_message=SystemMessageReplaceConfig(
                        mode="replace", content=SYSTEM_INSTRUCTIONS
                    ),
                )
            except Exception as exc:
                code = (
                    "fusion_dynamic_tiers_unavailable"
                    if "requires dynamic Auto tiers" in str(exc)
                    else "fusion_initialization_failed"
                )
                logger.error("poc_sdk_session_rejected code=%s error_type=%s", code, type(exc).__name__)
                raise LivePocError(code, 503) from None
            self._sessions[scope.internal_id] = session
        usages: dict[str, AssistantUsageData] = {}
        patterns: set[str] = set()
        models: set[str] = set()
        error_code: str | None = None
        tool_started = False

        def observe(event: SessionEvent) -> None:
            nonlocal error_code, tool_started
            data = event.data
            if isinstance(data, AssistantUsageData):
                key = data.provider_call_id or data.api_call_id or str(event.id)
                usages[key] = data
                models.add(data.model)
                if data.fusion is not None:
                    patterns.add(data.fusion.pattern)
                    if data.fusion.source_model:
                        models.add(data.fusion.source_model)
            elif isinstance(data, SessionErrorData):
                error_code = (
                    f"copilot_http_{data.status_code}"
                    if data.status_code is not None
                    else "copilot_session_error"
                )
            elif isinstance(data, ToolExecutionStartData):
                tool_started = True

        unsubscribe = session.on(observe)
        try:
            try:
                message = await session.send_and_wait(
                    prompt, timeout=self.settings.run_timeout_seconds
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise LivePocError("fusion_timeout", 504) from None
            except Exception as exc:
                logger.error(
                    "poc_sdk_inference_failed run_id=%s error_type=%s",
                    invocation_id, type(exc).__name__,
                )
                raise LivePocError(error_code or "fusion_inference_failed") from None
            if error_code:
                raise LivePocError(error_code)
            if tool_started:
                raise LivePocError("unexpected_tool_execution")
            if message is None or not isinstance(message.data, AssistantMessageData):
                raise LivePocError("invalid_fusion_response")
            data = message.data
            if data.model:
                models.add(data.model)
            if data.fusion is not None:
                patterns.add(data.fusion.pattern)
            values = list(usages.values())
            return ModelReply(
                content=data.content,
                requested_model=self.settings.github_model,
                reported_models=tuple(sorted(models)),
                input_tokens=_total([value.input_tokens for value in values]),
                output_tokens=_total([value.output_tokens for value in values]),
                cached_input_tokens=_total([value.cache_read_tokens for value in values]),
                sdk_session_id=session.session_id,
                fusion_patterns=tuple(sorted(patterns)),
            )
        finally:
            unsubscribe()

    async def forget(self, scope: SessionKey) -> None:
        session = self._sessions.pop(scope.internal_id, None)
        if session is not None:
            try:
                await asyncio.wait_for(session.abort(), timeout=4)
            finally:
                await asyncio.wait_for(session.disconnect(), timeout=4)

    async def aclose(self) -> None:
        failed = False
        for client in self._clients.values():
            try:
                await stop_client(client)
            except LivePocError:
                failed = True
        self._sessions.clear()
        self._clients.clear()
        self._root.cleanup()
        if failed:
            raise LivePocError("sdk_shutdown_failed", 503)
