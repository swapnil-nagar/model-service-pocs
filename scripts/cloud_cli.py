import json
import logging
import shutil
import subprocess
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_AUDIT = logging.getLogger(__name__)
_AUDIT.setLevel(logging.INFO)
_AUDIT.propagate = False
_AUDIT.addHandler(logging.NullHandler())


class CloudCommandError(RuntimeError):
    pass


@contextmanager
def audit_commands(path: Path) -> Iterator[None]:
    handler = logging.FileHandler(path, encoding="utf-8")
    _AUDIT.addHandler(handler)
    try:
        yield
    finally:
        _AUDIT.removeHandler(handler)
        handler.close()


def audit(command: str, status: str) -> None:
    _AUDIT.info("%s | %s | %s", datetime.now(timezone.utc).isoformat(), command, status)


def subscription_id(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ValueError("A valid explicit Azure subscription ID is required") from None


def az_output(
    arguments: Sequence[str],
    subscription: str,
    *,
    timeout: float = 300,
    secret_values: Sequence[str] = (),
) -> str:
    executable = shutil.which("az")
    if executable is None:
        raise CloudCommandError("Azure CLI is not available")
    selected_subscription = subscription_id(subscription)
    scope = ["--subscription", selected_subscription]
    if tuple(arguments[:2]) == ("bicep", "build"):
        scope = []
    command = [executable, *arguments, *scope, "--only-show-errors"]
    safe_command = subprocess.list2cmdline(command)
    for secret in secret_values:
        if secret:
            safe_command = safe_command.replace(secret, "[redacted]")
    audit(safe_command, "started")
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        audit(safe_command, "failed")
        raise CloudCommandError("Azure CLI command timed out") from None
    except OSError:
        audit(safe_command, "failed")
        raise CloudCommandError("Azure CLI command could not start") from None
    if result.returncode:
        audit(safe_command, "failed")
        message = result.stderr.strip()
        for secret in secret_values:
            if secret:
                message = message.replace(secret, "[redacted]")
        raise CloudCommandError(message or "Azure CLI command failed")
    audit(safe_command, "succeeded")
    return result.stdout.strip()


def az_json(
    arguments: Sequence[str],
    subscription: str,
    *,
    timeout: float = 300,
    secret_values: Sequence[str] = (),
) -> object:
    raw = az_output(
        [*arguments, "-o", "json"], subscription,
        timeout=timeout, secret_values=secret_values,
    )
    try:
        return json.loads(raw)
    except ValueError:
        raise CloudCommandError("Azure CLI returned invalid JSON") from None


def vault_secret(vault: str, name: str, subscription: str) -> str:
    value = az_output(
        ["keyvault", "secret", "show", "--vault-name", vault, "--name", name,
         "--query", "value", "-o", "tsv"],
        subscription, timeout=60,
    )
    if not value or not value.isascii() or any(character.isspace() for character in value):
        raise CloudCommandError("The required pilot credential is missing or invalid")
    return value
