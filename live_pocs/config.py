import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from urllib.parse import urlsplit


class BackendMode(StrEnum):
    FOUNDRY = "foundry"
    FUSION = "fusion"


def _required(name: str) -> str:
    value = os.environ.get(name, "")
    if not value.strip():
        raise RuntimeError(f"Missing required setting: {name}")
    return value


def _integer(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        raise RuntimeError(f"Invalid integer setting: {name}") from None
    if value < 1 or value > maximum:
        raise RuntimeError(f"Out-of-range setting: {name}")
    return value


@dataclass(frozen=True)
class Settings:
    mode: BackendMode
    backend_key: str = field(repr=False)
    live_enabled: bool = False
    allowed_tenants: tuple[str, ...] = ("customer-a", "customer-b")
    max_sessions: int = 20
    max_turns: int = 10
    max_pending_runs: int = 20
    run_timeout_seconds: float = 90
    foundry_gateway_base_url: str = ""
    model_deployment: str = ""
    foundry_model_name: str = "gpt-4.1-mini"
    tenant_a_key: str = field(default="", repr=False)
    tenant_b_key: str = field(default="", repr=False)
    github_token: str = field(default="", repr=False)
    github_login: str = ""
    github_model: str = "fusion"

    @classmethod
    def from_environment(cls) -> "Settings":
        try:
            mode = BackendMode(_required("POC_MODE"))
        except ValueError:
            raise RuntimeError("Invalid setting: POC_MODE") from None
        backend_key = _required("POC_BACKEND_KEY")
        if len(backend_key) < 24 or not backend_key.isascii():
            raise RuntimeError("Invalid setting: POC_BACKEND_KEY")
        enabled = os.environ.get("POC_LIVE_ENABLED", "false").lower()
        if enabled not in {"true", "false"}:
            raise RuntimeError("Invalid setting: POC_LIVE_ENABLED")
        tenants = tuple(
            value.strip() for value in _required("POC_ALLOWED_TENANTS").split(",")
        )
        if len(tenants) != 2 or set(tenants) != {"customer-a", "customer-b"}:
            raise RuntimeError("This pilot requires customer-a and customer-b only")
        common = cls(
            mode=mode,
            backend_key=backend_key,
            live_enabled=enabled == "true",
            allowed_tenants=tenants,
            max_sessions=_integer("POC_MAX_SESSIONS", 20, 100),
            max_turns=_integer("POC_MAX_TURNS", 10, 10),
            max_pending_runs=_integer("POC_MAX_PENDING_RUNS", 20, 100),
            run_timeout_seconds=float(_integer("POC_RUN_TIMEOUT_SECONDS", 90, 120)),
        )
        if mode == BackendMode.FOUNDRY:
            base_url = _required("POC_FOUNDRY_GATEWAY_BASE_URL").rstrip("/")
            try:
                parsed = urlsplit(base_url)
                port = parsed.port
            except ValueError:
                raise RuntimeError("Invalid setting: POC_FOUNDRY_GATEWAY_BASE_URL") from None
            if (
                parsed.scheme != "https"
                or not (parsed.hostname or "").endswith(".azure-api.net")
                or port not in {None, 443}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path != "/model"
                or parsed.query
                or parsed.fragment
            ):
                raise RuntimeError("Invalid setting: POC_FOUNDRY_GATEWAY_BASE_URL")
            return replace(
                common,
                foundry_gateway_base_url=base_url,
                model_deployment=_required("POC_MODEL_DEPLOYMENT"),
                foundry_model_name=_required("POC_FOUNDRY_MODEL_NAME"),
                tenant_a_key=_required("POC_FOUNDRY_TENANT_A_KEY"),
                tenant_b_key=_required("POC_FOUNDRY_TENANT_B_KEY"),
            )
        selector = os.environ.get("POC_GITHUB_MODEL", "fusion")
        if selector != "fusion":
            raise RuntimeError("POC_GITHUB_MODEL must use the documented fusion selector")
        return replace(
            common,
            github_token=_required("POC_GITHUB_TOKEN"),
            github_login=_required("POC_GITHUB_LOGIN"),
            github_model=selector,
        )
