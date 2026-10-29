import hmac
import re
from collections.abc import Mapping

from .contracts import ModelServiceError, Request

_ID = re.compile(r"[a-zA-Z0-9_-]{1,64}\Z")


def valid_identifier(value: str) -> bool:
    return _ID.fullmatch(value) is not None


def authorize_request(
    caller_tokens: Mapping[str, str], caller_token: str, request: Request
) -> str:
    if not caller_token or not caller_token.isascii():
        raise ModelServiceError("unauthenticated")
    tenant = next(
        (
            tenant
            for tenant, token in caller_tokens.items()
            if hmac.compare_digest(token, caller_token)
        ),
        None,
    )
    if tenant is None:
        raise ModelServiceError("unauthenticated")
    if tenant != request.tenant_id:
        raise ModelServiceError("tenant_mismatch")
    if request.agent_id not in {"assistant", "reviewer"}:
        raise ModelServiceError("agent_not_allowed")
    if not valid_identifier(request.session_id):
        raise ModelServiceError("invalid_session")
    if request.model_alias != "chat":
        raise ModelServiceError("model_not_allowed")
    if not request.prompt.strip() or len(request.prompt) > 8192:
        raise ModelServiceError("invalid_prompt")
    return tenant
