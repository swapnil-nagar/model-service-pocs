import hmac
import json
import secrets
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from .contracts import Approach, Customer, Target
from .service import ModelService


class MockEndpoint:
    """An independent loopback server and credential per tenant and resource."""

    def __init__(
        self, approach: Approach, tenant: str, *, backend: Target | None = None
    ) -> None:
        self.approach = approach
        self.tenant = tenant
        self.key = secrets.token_urlsafe(24)
        self.deployment = f"{tenant}-chat"
        self.backend = backend
        self.calls: list[dict[str, str | int]] = []
        self.fail_status: int | None = None
        self.malformed = False
        self._guard = threading.Lock()
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                status, body = endpoint.handle(
                    self.path,
                    dict(self.headers),
                    self.rfile.read(int(self.headers.get("Content-Length", "0"))),
                )
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def target(self) -> Target:
        return Target(f"http://127.0.0.1:{self.server.server_port}", self.key, self.deployment)

    def __enter__(self) -> "MockEndpoint":
        self.thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def handle(self, path: str, headers: dict[str, str], raw: bytes) -> tuple[int, dict]:
        headers = {key.lower(): value for key, value in headers.items()}
        expected = (
            self.key if self.approach == Approach.AZURE_OPENAI else f"Bearer {self.key}"
        )
        provided = headers.get(
            "api-key" if self.approach == Approach.AZURE_OPENAI else "authorization", ""
        )
        if not hmac.compare_digest(provided, expected):
            return 401, {"error": "wrong_resource_credential"}
        expected_path = {
            Approach.AZURE_OPENAI: (
                f"/openai/deployments/{self.deployment}/chat/completions"
                "?api-version=2024-10-21"
            ),
            Approach.FOUNDRY: "/openai/v1/chat/completions",
            Approach.APIM: "/models/chat/completions",
            Approach.AZURE_ML: "/score",
        }[self.approach]
        if path != expected_path:
            return 404, {"error": "wrong_resource_route"}
        try:
            body = json.loads(raw)
            if self.approach in {Approach.FOUNDRY, Approach.APIM}:
                if body["model"] != self.deployment:
                    return 403, {"error": "wrong_model"}
            messages = (
                body["input_data"]["messages"]
                if self.approach == Approach.AZURE_ML
                else body["messages"]
            )
            if not isinstance(messages, list) or not messages:
                return 400, {"error": "invalid_messages"}
            prompts = [m["content"] for m in messages if m["role"] == "user"]
            if not prompts or not all(isinstance(prompt, str) for prompt in prompts):
                return 400, {"error": "invalid_messages"}
        except (ValueError, KeyError, TypeError):
            return 400, {"error": "invalid_body"}
        with self._guard:
            self.calls.append(
                {"tenant": self.tenant, "path": path, "user_turns": len(prompts)}
            )
            status = self.fail_status
            malformed = self.malformed
        if status is not None:
            return status, {"error": "injected_failure"}
        if malformed:
            return 200, {"unexpected": True}
        if self.backend is not None:
            upstream = Request(
                self.backend.endpoint
                + f"/openai/deployments/{self.backend.deployment}/chat/completions"
                "?api-version=2024-10-21",
                json.dumps({"messages": messages, "max_tokens": 128}).encode(),
                {"Content-Type": "application/json", "api-key": self.backend.credential},
                method="POST",
            )
            try:
                with build_opener(ProxyHandler({})).open(upstream, timeout=2) as response:
                    return response.status, json.load(response)
            except HTTPError as exc:
                return exc.code, {"error": "backend_http_failure"}
            except (URLError, TimeoutError, OSError):
                return 502, {"error": "backend_unavailable"}
        content = json.dumps(
            {"mock": True, "tenant": self.tenant, "user_prompts": prompts},
            ensure_ascii=True,
        )
        if self.approach == Approach.AZURE_ML:
            return 200, {"output": content}
        return 200, {"choices": [{"message": {"role": "assistant", "content": content}}]}


class LocalExperiment:
    def __init__(self, approach: Approach) -> None:
        self.approach = approach
        self._stack = ExitStack()
        self.endpoints: dict[str, MockEndpoint] = {}
        self.backends: dict[str, MockEndpoint] = {}
        self.customers: dict[str, Customer] = {}

    def __enter__(self) -> "LocalExperiment":
        with ExitStack() as startup:
            for tenant in ("customer-a", "customer-b"):
                backend = None
                if self.approach == Approach.APIM:
                    resource = startup.enter_context(
                        MockEndpoint(Approach.AZURE_OPENAI, tenant)
                    )
                    self.backends[tenant] = resource
                    backend = resource.target
                endpoint = startup.enter_context(
                    MockEndpoint(self.approach, tenant, backend=backend)
                )
                self.endpoints[tenant] = endpoint
                self.customers[tenant] = Customer(
                    tenant, secrets.token_urlsafe(24), endpoint.target
                )
            self.service = ModelService(self.approach, tuple(self.customers.values()))
            self._stack = startup.pop_all()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stack.close()
