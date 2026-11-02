import hmac
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .config import BackendMode, Settings
from .service import InferenceBackend, LivePocError, RunRequest, RunService

logger = logging.getLogger("model_service.live")


class SubmitInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=8192, repr=False)
    agent_id: str = "assistant"
    model_alias: str = "chat"
    new_session: bool = False


def _backend(settings: Settings) -> InferenceBackend:
    if settings.mode == BackendMode.FOUNDRY:
        from .foundry import FoundryBackend

        return FoundryBackend(settings)
    from .fusion import FusionBackend

    return FusionBackend(settings)


def create_app(
    settings: Settings | None = None,
    backend_factory: Callable[[Settings], InferenceBackend] = _backend,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        configured = settings or Settings.from_environment()
        service = RunService(configured, backend_factory(configured))
        application.state.service = service
        try:
            yield
        finally:
            await service.aclose()

    application = FastAPI(
        lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )

    def service() -> RunService:
        candidate = getattr(application.state, "service", None)
        if not isinstance(candidate, RunService):
            raise LivePocError("runner_not_ready", 503)
        return candidate

    def tenant(request: Request) -> str:
        candidate = getattr(request.state, "tenant_id", None)
        if not isinstance(candidate, str):
            raise LivePocError("tenant_context_missing", 403)
        return candidate

    @application.middleware("http")
    async def authenticate(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path not in {"/", "/healthz"}:
            runtime = service()
            supplied = request.headers.get("x-poc-backend-key", "")
            if not supplied.isascii() or not hmac.compare_digest(
                supplied, runtime.settings.backend_key
            ):
                logger.warning("poc_request_rejected code=backend_unauthenticated")
                return JSONResponse(
                    {"error": {"code": "backend_unauthenticated"}}, status_code=401
                )
            supplied_tenant = request.headers.get("x-poc-tenant-id", "")
            if supplied_tenant not in runtime.settings.allowed_tenants:
                logger.warning("poc_request_rejected code=tenant_not_allowed")
                return JSONResponse(
                    {"error": {"code": "tenant_not_allowed"}}, status_code=403
                )
            request.state.tenant_id = supplied_tenant
        return await call_next(request)

    @application.exception_handler(LivePocError)
    async def known_error(request: Request, exc: LivePocError) -> JSONResponse:
        logger.warning("poc_request_rejected code=%s", exc.code)
        return JSONResponse({"error": {"code": exc.code}}, status_code=exc.status_code)

    @application.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError) -> JSONResponse:
        logger.warning("poc_request_rejected code=invalid_request")
        return JSONResponse({"error": {"code": "invalid_request"}}, status_code=422)

    @application.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error("poc_request_error error_type=%s", type(exc).__name__)
        return JSONResponse({"error": {"code": "runner_error"}}, status_code=500)

    @application.get("/")
    @application.get("/healthz")
    async def health() -> dict[str, object]:
        runtime = service()
        return {
            "status": "configured",
            "mode": runtime.settings.mode.value,
            "live_enabled": runtime.settings.live_enabled,
            "instance_id": runtime.instance_id,
            "inference_verified": False,
            "state_durability": "ephemeral_single_replica",
        }

    @application.post("/runs", status_code=202)
    async def submit(body: SubmitInput, request: Request) -> dict[str, object]:
        return service().submit(
            tenant(request),
            RunRequest(
                session_id=body.session_id,
                prompt=body.prompt,
                agent_id=body.agent_id,
                model_alias=body.model_alias,
                new_session=body.new_session,
            ),
        )

    @application.get("/runs/{run_id}")
    async def status(run_id: str, request: Request) -> dict[str, object]:
        return service().get(tenant(request), run_id)

    @application.delete("/runs/{run_id}", status_code=202)
    async def cancel(run_id: str, request: Request) -> dict[str, object]:
        await service().cancel(tenant(request), run_id)
        return {"run_id": run_id, "cancel_requested": True}

    return application


app = create_app()
