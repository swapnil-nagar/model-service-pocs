import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError
from starlette.background import BackgroundTask

from .routing import BindingRegistry, Endpoint, RoutingSettings, SessionBinding, SessionRouter
from .routing_storage import create_registry
from .service import LivePocError

logger = logging.getLogger("model_service.live")
_JSON_OBJECT = TypeAdapter(dict[str, JsonValue], config=ConfigDict(allow_inf_nan=False))
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: Identifier
    agent_id: Identifier = "assistant"
    model_alias: Identifier


def _error(code: str) -> dict[str, object]:
    return {"error": {"code": code, "type": "model_service_error", "message": code}}


async def _body(request: Request, maximum: int) -> dict[str, JsonValue]:
    chunks: list[bytes] = []
    length = 0
    async for chunk in request.stream():
        length += len(chunk)
        if length > maximum:
            raise LivePocError("request_too_large", 413)
        chunks.append(chunk)
    try:
        return _JSON_OBJECT.validate_json(b"".join(chunks), strict=True)
    except ValidationError:
        raise LivePocError("invalid_request", 422) from None


def _chat_body(
    payload: dict[str, JsonValue], binding: SessionBinding, endpoint: Endpoint,
) -> tuple[dict[str, JsonValue], bool]:
    if payload.get("model") != binding.model_alias:
        raise LivePocError("session_model_conflict", 409)
    if {
        "tenant_id", "provider", "endpoint", "base_url", "api_key", "credential",
        "previous_response_id", "conversation", "thread_id", "assistant_id",
    }.intersection(payload):
        raise LivePocError("unsupported_request_field", 422)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages or any(
        not isinstance(message, dict)
        or message.get("role") not in {"system", "developer", "user", "assistant", "tool", "function"}
        for message in messages
    ):
        raise LivePocError("invalid_messages", 422)
    for message in messages:
        if not isinstance(message, dict):
            raise LivePocError("invalid_messages", 422)
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "file":
                    file = block.get("file")
                    if isinstance(file, dict) and "file_id" in file:
                        raise LivePocError("provider_state_not_allowed", 422)
    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        raise LivePocError("invalid_stream_option", 422)
    if "store" in payload and payload["store"] is not False:
        raise LivePocError("provider_state_not_allowed", 422)
    if "n" in payload and (type(payload["n"]) is not int or payload["n"] != 1):
        raise LivePocError("multiple_completions_not_allowed", 422)
    tools = payload.get("tools", [])
    if not isinstance(tools, list) or any(
        not isinstance(tool, dict) or tool.get("type") != "function"
        or not isinstance(tool.get("function"), dict)
        for tool in tools
    ):
        raise LivePocError("unsupported_tools", 422)
    for name in ("max_tokens", "max_completion_tokens"):
        if name in payload:
            value = payload[name]
            if type(value) is not int or value < 1 or value > endpoint.max_output_tokens:
                raise LivePocError("output_limit_exceeded", 422)
    outgoing = dict(payload)
    outgoing["model"] = endpoint.deployment
    outgoing["store"] = False
    if "max_tokens" not in outgoing and "max_completion_tokens" not in outgoing:
        outgoing["max_tokens"] = endpoint.max_output_tokens
    return outgoing, stream


async def _limited_bytes(response: httpx.Response, maximum: int) -> bytes:
    parts: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        if size > maximum:
            raise LivePocError("provider_response_too_large", 502)
        parts.append(chunk)
    return b"".join(parts)


async def _events(
    response: httpx.Response, expected_model: str, maximum: int,
) -> AsyncGenerator[bytes]:
    buffer = b""
    total = 0
    model_seen = False
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > maximum:
            raise LivePocError("provider_response_too_large")
        buffer += chunk
        while delimiter := re.search(rb"\r?\n\r?\n", buffer):
            event, buffer = buffer[:delimiter.end()], buffer[delimiter.end():]
            data = b"\n".join(
                line[5:].lstrip(b" ") for line in event.splitlines() if line.startswith(b"data:")
            )
            if not data:
                continue
            if data == b"[DONE]":
                if not model_seen:
                    raise LivePocError("provider_model_unverified")
                yield event
                return
            try:
                item = _JSON_OBJECT.validate_json(data, strict=True)
            except ValidationError:
                raise LivePocError("invalid_provider_stream") from None
            if "error" in item:
                raise LivePocError("provider_stream_error")
            model = item.get("model")
            if model is not None:
                if model != expected_model:
                    raise LivePocError("provider_model_mismatch")
                model_seen = True
            if not model_seen:
                raise LivePocError("provider_model_unverified")
            yield event
    raise LivePocError("provider_stream_incomplete")


class InferenceProxy:
    def __init__(self, router: SessionRouter, client: httpx.AsyncClient) -> None:
        self.router = router
        self.client = client

    async def forward(self, request: Request, binding: SessionBinding) -> Response:
        settings = self.router.settings
        if not settings.live_enabled:
            raise LivePocError("live_inference_disabled", 503)
        payload = await _body(request, settings.max_request_bytes)
        endpoint = self.router.endpoint(binding)
        outgoing, stream = _chat_body(payload, binding, endpoint)
        headers = endpoint.headers()
        headers["Accept"] = "text/event-stream" if stream else "application/json"
        upstream_request = self.client.build_request(
            "POST", endpoint.base_url + "/chat/completions", headers=headers, json=outgoing,
        )
        deadline = asyncio.get_running_loop().time() + settings.upstream_timeout_seconds
        try:
            upstream = await asyncio.wait_for(
                self.client.send(upstream_request, stream=True), settings.upstream_timeout_seconds,
            )
        except (httpx.TimeoutException, TimeoutError):
            raise LivePocError("endpoint_timeout", 504) from None
        except httpx.RequestError:
            raise LivePocError("endpoint_unavailable", 503) from None
        response_headers = {
            "Cache-Control": "no-store",
            "X-Model-Session-Expires-At": binding.expires_at.isoformat(),
        }
        streaming = False
        try:
            if upstream.status_code != 200:
                status = upstream.status_code if 400 <= upstream.status_code < 600 else 502
                raise LivePocError(f"endpoint_http_{upstream.status_code}", status)
            content_type = upstream.headers.get("content-type", "").split(";", 1)[0].lower()
            if stream:
                if content_type != "text/event-stream":
                    raise LivePocError("invalid_provider_content_type")
                events = _events(upstream, endpoint.reported_model, settings.max_response_bytes)
                try:
                    first = await asyncio.wait_for(
                        anext(events), max(0, deadline - asyncio.get_running_loop().time()),
                    )
                except StopAsyncIteration:
                    raise LivePocError("provider_stream_incomplete") from None
                streaming = True
                return StreamingResponse(
                    self._stream(first, events, upstream, deadline=deadline),
                    media_type="text/event-stream", headers=response_headers,
                    background=BackgroundTask(upstream.aclose),
                )
            if content_type != "application/json":
                raise LivePocError("invalid_provider_content_type")
            raw = await asyncio.wait_for(
                _limited_bytes(upstream, settings.max_response_bytes),
                max(0, deadline - asyncio.get_running_loop().time()),
            )
            try:
                reply = _JSON_OBJECT.validate_json(raw, strict=True)
            except ValidationError:
                raise LivePocError("invalid_provider_response") from None
            if reply.get("model") != endpoint.reported_model:
                raise LivePocError("provider_model_mismatch")
            choices = reply.get("choices")
            if (
                "error" in reply or not isinstance(choices, list) or not choices
                or any(not isinstance(choice, dict) or not isinstance(choice.get("message"), dict) for choice in choices)
            ):
                raise LivePocError("invalid_provider_response")
            return Response(raw, media_type="application/json", headers=response_headers)
        except (LivePocError, httpx.RequestError, TimeoutError, asyncio.CancelledError) as exc:
            if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
                raise LivePocError("endpoint_timeout", 504) from None
            if isinstance(exc, httpx.RequestError):
                raise LivePocError("endpoint_unavailable", 503) from None
            raise
        finally:
            if not streaming:
                await upstream.aclose()

    async def _stream(
        self, first: bytes, events: AsyncGenerator[bytes], response: httpx.Response,
        *, deadline: float | None = None,
    ) -> AsyncGenerator[bytes]:
        try:
            limit = deadline if deadline is not None else (
                asyncio.get_running_loop().time() + self.router.settings.upstream_timeout_seconds
            )
            async with asyncio.timeout_at(limit):
                yield first
                async for event in events:
                    yield event
        except (LivePocError, httpx.RequestError, TimeoutError) as exc:
            code = (
                exc.code if isinstance(exc, LivePocError)
                else "endpoint_timeout" if isinstance(exc, TimeoutError)
                else "provider_stream_interrupted"
            )
            logger.error("routing_stream_failed code=%s", code)
            yield b"data: " + json.dumps(_error(code)).encode("utf-8") + b"\n\n"
        finally:
            await events.aclose()
            await response.aclose()


def create_app(
    settings: RoutingSettings | None = None, *,
    registry: BindingRegistry | None = None,
    client: httpx.AsyncClient | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncGenerator[None]:
        configured = settings if settings is not None else RoutingSettings.from_environment()
        store = registry if registry is not None else create_registry(configured.registry)
        upstream = client if client is not None else httpx.AsyncClient(
            timeout=httpx.Timeout(configured.upstream_timeout_seconds),
            follow_redirects=False, trust_env=False,
        )
        try:
            application.state.router = SessionRouter(configured, store)
            application.state.proxy = InferenceProxy(application.state.router, upstream)
            yield
        finally:
            try:
                if client is None:
                    await upstream.aclose()
            finally:
                await store.aclose()

    application = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def router() -> SessionRouter:
        candidate = getattr(application.state, "router", None)
        if not isinstance(candidate, SessionRouter):
            raise LivePocError("router_not_ready", 503)
        return candidate

    def tenant(request: Request) -> str:
        return router().authenticate(request.headers.get("authorization", ""))

    @application.exception_handler(LivePocError)
    async def known_error(_request: Request, exc: LivePocError) -> JSONResponse:
        logger.warning("routing_request_rejected code=%s", exc.code)
        return JSONResponse(_error(exc.code), status_code=exc.status_code)

    @application.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        logger.warning("routing_request_rejected code=invalid_request")
        return JSONResponse(_error("invalid_request"), status_code=422)

    @application.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        logger.error("routing_request_failed error_type=%s", type(exc).__name__)
        return JSONResponse(_error("routing_service_error"), status_code=500)

    @application.get("/healthz")
    async def health() -> dict[str, object]:
        configured = router().settings
        return {
            "status": "configured", "mode": "model_routing",
            "registry": configured.registry.backend, "binding_lifetime_hours": 24,
            "live_enabled": configured.live_enabled, "inference_verified": False,
            "context_owner": "caller_sdk",
        }

    @application.post("/sessions")
    async def bind(request: Request, response: Response) -> dict[str, object]:
        owner = tenant(request)
        payload = await _body(request, 16384)
        try:
            body = SessionInput.model_validate(payload)
        except ValidationError:
            raise LivePocError("invalid_request", 422) from None
        binding, created = await router().bind(owner, body.agent_id, body.session_id, body.model_alias)
        response.status_code = 201 if created else 200
        response.headers["Cache-Control"] = "no-store"
        return router().view(binding)

    @application.get("/sessions/{agent_id}/{session_id}")
    async def session(agent_id: str, session_id: str, request: Request) -> JSONResponse:
        binding = await router().get(tenant(request), agent_id, session_id)
        return JSONResponse(router().view(binding), headers={"Cache-Control": "no-store"})

    @application.get("/sessions/{agent_id}/{session_id}/v1/models")
    async def models(agent_id: str, session_id: str, request: Request) -> JSONResponse:
        binding = await router().get(tenant(request), agent_id, session_id)
        return JSONResponse({
            "object": "list",
            "data": [{"id": binding.model_alias, "object": "model", "owned_by": "model-service"}],
        }, headers={"Cache-Control": "no-store"})

    @application.post("/sessions/{agent_id}/{session_id}/v1/chat/completions")
    async def completions(agent_id: str, session_id: str, request: Request) -> Response:
        binding = await router().get(tenant(request), agent_id, session_id)
        proxy = getattr(application.state, "proxy", None)
        if not isinstance(proxy, InferenceProxy):
            raise LivePocError("router_not_ready", 503)
        return await proxy.forward(request, binding)

    return application


app = create_app()
