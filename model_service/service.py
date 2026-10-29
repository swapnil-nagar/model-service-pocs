import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from urllib.parse import urlsplit

from .authorization import authorize_request, valid_identifier
from .contracts import Approach, Customer, ModelServiceError, Request, Result

_WORKER = Path(__file__).with_name("worker.py").resolve()


class ModelService:
    """Trusted local dispatcher. Its in-memory state is scoped to this instance."""

    def __init__(
        self, approach: Approach, customers: tuple[Customer, ...], *, timeout: float = 5
    ) -> None:
        if not customers or timeout <= 0:
            raise ValueError("Customers and a positive worker timeout are required")
        tenants = [customer.tenant_id for customer in customers]
        tokens = [customer.caller_token for customer in customers]
        endpoints = [customer.target.endpoint for customer in customers]
        credentials = [customer.target.credential for customer in customers]
        for values in (tenants, tokens, endpoints, credentials):
            if len(set(values)) != len(values) or any(not value for value in values):
                raise ValueError("Tenant identities, tokens, endpoints and keys must be distinct")
        for customer in customers:
            if not valid_identifier(customer.tenant_id):
                raise ValueError("Invalid tenant identifier")
            endpoint = urlsplit(customer.target.endpoint)
            if (
                endpoint.scheme != "http"
                or endpoint.hostname != "127.0.0.1"
                or not endpoint.port
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.path
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("Only generated loopback mock endpoints are allowed")
        self.approach = approach
        self.timeout = timeout
        self._customers = MappingProxyType({c.tenant_id: c for c in customers})
        self._caller_tokens = MappingProxyType({c.tenant_id: c.caller_token for c in customers})
        self._history: dict[tuple[str, str, str, str], list[dict[str, str]]] = {}
        self._locks: dict[tuple[str, str, str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def run(self, caller_token: str, request: Request) -> Result:
        tenant = authorize_request(self._caller_tokens, caller_token, request)
        customer = self._customers[tenant]
        scope = (
            customer.tenant_id,
            request.agent_id,
            request.session_id,
            request.model_alias,
        )
        with self._guard:
            lock = self._locks.setdefault(scope, threading.Lock())
        with lock:
            history = self._history.get(scope, [])
            if len(history) >= 20:
                raise ModelServiceError("session_turn_limit")
            messages = [*history, {"role": "user", "content": request.prompt}]
            payload = {
                "approach": self.approach.value,
                "endpoint": customer.target.endpoint,
                "credential": customer.target.credential,
                "deployment": customer.target.deployment,
                "messages": messages,
            }
            environment = {
                key: value
                for key, value in os.environ.items()
                if key.upper() in {"SYSTEMROOT", "WINDIR"}
            }
            with TemporaryDirectory(prefix="model-run-") as workspace:
                environment.update(TEMP=workspace, TMP=workspace)
                try:
                    completed = subprocess.run(
                        [sys.executable, "-I", "-B", str(_WORKER)],
                        input=json.dumps(payload),
                        text=True,
                        encoding="utf-8",
                        capture_output=True,
                        cwd=workspace,
                        env=environment,
                        timeout=self.timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired:
                    raise ModelServiceError("worker_timeout") from None
                except OSError:
                    raise ModelServiceError("worker_start_failed") from None
            if completed.returncode != 0:
                raise ModelServiceError("worker_failed")
            try:
                output = json.loads(completed.stdout)
                if "error" in output:
                    raise ModelServiceError(output["error"])
                content = output["content"]
                pid = output["worker_pid"]
                workspace_id = output["workspace_id"]
                names = {name.upper() for name in output["environment_names"]}
                if (
                    not isinstance(content, str)
                    or not content
                    or not isinstance(pid, int)
                    or pid == os.getpid()
                    or not isinstance(workspace_id, str)
                    or not workspace_id.startswith("model-run-")
                    or names - {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LC_CTYPE"}
                ):
                    raise ModelServiceError("invalid_worker_response")
            except (ValueError, KeyError, TypeError):
                raise ModelServiceError("invalid_worker_response") from None
            self._history[scope] = [
                *messages,
                {"role": "assistant", "content": content},
            ]
            return Result(content, pid, workspace_id, len(history) // 2 + 1)
