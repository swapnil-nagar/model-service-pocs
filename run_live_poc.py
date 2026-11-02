import argparse
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from model_service.worker import NoRedirect
from scripts.cloud_cli import CloudCommandError, audit_commands, az_json, subscription_id, vault_secret


@dataclass(frozen=True)
class Tenant:
    name: str
    key: str = field(repr=False)


class LiveAcceptanceError(RuntimeError):
    def __init__(self, code: str, *, run_id: str | None = None, provider_code: str | None = None):
        super().__init__(code)
        self.code = code
        self.run_id = run_id
        self.provider_code = provider_code


@dataclass
class AcceptanceProgress:
    path: Path | None = field(default=None, repr=False)
    stage: str = "preflight"
    turn_submit_attempts: int = 0
    accepted_turns: int = 0
    checks: dict[str, bool] = field(default_factory=dict)

    def view(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "turn_submit_attempts": self.turn_submit_attempts,
            "accepted_turns": self.accepted_turns,
            "checks": dict(self.checks),
        }

    def checkpoint(self, stage: str) -> None:
        self.stage = stage
        if self.path is not None:
            self.path.write_text(json.dumps(self.view(), indent=2) + "\n", encoding="utf-8")


def gateway_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("An HTTPS APIM gateway root URL is required") from None
    if (
        parsed.scheme != "https"
        or not (parsed.hostname or "").endswith(".azure-api.net")
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("An HTTPS APIM gateway root URL is required")
    return value.rstrip("/")


def deployment_target(path: Path, subscription: str) -> tuple[str, str, str]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(state, dict)
        or state.get("status") != "configuration_healthy"
        or state.get("subscriptionId") != subscription
    ):
        raise RuntimeError("Deployment evidence does not match the explicitly approved target")
    group, vault, base = (
        state.get("resourceGroup"), state.get("keyVaultName"), state.get("gatewayUrl")
    )
    if not all(isinstance(value, str) and value for value in (group, vault, base)):
        raise RuntimeError("Deployment evidence is missing target fields")
    if not isinstance(group, str) or not isinstance(vault, str) or not isinstance(base, str):
        raise RuntimeError("Invalid deployment target types")
    return gateway_url(base), vault, group


def verify_owned_target(base: str, vault: str, group: str, subscription: str) -> None:
    hostname = urlsplit(base).hostname
    if hostname is None:
        raise RuntimeError("Invalid gateway hostname")
    apim_name = hostname.split(".")[0]
    actual = az_json(
        ["apim", "show", "--name", apim_name, "--resource-group", group,
         "--query", "gatewayUrl"],
        subscription,
    )
    actual_vault = az_json(
        ["keyvault", "show", "--name", vault, "--resource-group", group, "--query", "name"],
        subscription,
    )
    if not isinstance(actual, str) or actual.rstrip("/") != base or actual_vault != vault:
        raise RuntimeError("Gateway and credential store do not match the owned deployment")


def warm_runner(path: Path, group: str, subscription: str) -> None:
    evidence = json.loads(path.read_text(encoding="utf-8"))
    fqdn, image = evidence.get("runnerFqdn"), evidence.get("image")
    if (
        not isinstance(fqdn, str)
        or not re.fullmatch(r"[a-z0-9.-]+\.azurecontainerapps\.io", fqdn)
        or not isinstance(image, str) or not image
    ):
        raise LiveAcceptanceError("invalid_runner_readiness_target")
    runners = az_json(
        ["containerapp", "list", "--resource-group", group,
         "--query", "[].{fqdn:properties.configuration.ingress.fqdn,image:properties.template.containers[0].image}"],
        subscription,
    )
    if not isinstance(runners, list) or not any(
        isinstance(runner, dict) and runner.get("fqdn") == fqdn and runner.get("image") == image
        for runner in runners
    ):
        raise LiveAcceptanceError("runner_readiness_target_not_owned")
    for attempt in range(3):
        try:
            status, state = call("https://" + fqdn, "/healthz", tenant=None)
        except LiveAcceptanceError as exc:
            if exc.code != "gateway_request_failed_or_timeout":
                raise
        else:
            if (
                status == 200 and isinstance(state, dict)
                and state.get("status") == "configured"
                and state.get("mode") == "foundry"
                and state.get("live_enabled") is True
            ):
                return
        if attempt < 2:
            print("Waiting for owned runner configuration readiness; no model request is being submitted.")
            time.sleep(10 * (attempt + 1))
    raise LiveAcceptanceError("runner_readiness_failed")


def call(
    base: str,
    path: str,
    *,
    tenant: Tenant | None,
    method: str = "GET",
    body: dict[str, object] | None = None,
    claimed_tenant: str | None = None,
) -> tuple[int, object]:
    headers = {"Accept": "application/json"}
    if tenant is not None:
        headers["Ocp-Apim-Subscription-Key"] = tenant.key
    if claimed_tenant is not None:
        headers["X-Poc-Tenant-Id"] = claimed_tenant
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(base + path, data=data, headers=headers, method=method)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=28) as response:
            status, raw = response.status, response.read(1_048_577)
    except HTTPError as exc:
        status, raw = exc.code, exc.read(1_048_577)
    except (URLError, TimeoutError, OSError):
        raise LiveAcceptanceError("gateway_request_failed_or_timeout") from None
    if len(raw) > 1_048_576:
        raise LiveAcceptanceError("gateway_response_too_large")
    try:
        return status, json.loads(raw)
    except ValueError:
        if status >= 400:
            return status, {"error": {"code": f"gateway_http_{status}"}}
        raise LiveAcceptanceError("gateway_non_json_success") from None


def run_turn(
    base: str,
    tenant: Tenant,
    session_id: str,
    prompt: str,
    *,
    new_session: bool,
) -> dict[str, object]:
    status, submitted = call(
        base, "/foundry/runs", tenant=tenant, method="POST",
        body={
            "session_id": session_id, "prompt": prompt,
            "agent_id": "assistant", "model_alias": "chat", "new_session": new_session,
        },
    )
    if status != 202 or not isinstance(submitted, dict):
        raise LiveAcceptanceError(f"run_submission_http_{status}")
    run_id = submitted.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError("Live run submission did not return a correlation ID")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        status, state = call(base, "/foundry/runs/" + run_id, tenant=tenant)
        if status != 200 or not isinstance(state, dict):
            raise RuntimeError(f"Live run status failed with HTTP {status}")
        if state.get("tenant_id") != tenant.name or state.get("session_id") != session_id:
            raise RuntimeError("Live response ownership/correlation mismatch")
        if state.get("run_id") != run_id or state.get("provider") != "foundry":
            raise RuntimeError("Live response provider/correlation mismatch")
        if state.get("status") == "succeeded":
            result = state.get("result")
            if not isinstance(result, dict) or not isinstance(result.get("content"), str):
                raise RuntimeError("Live run returned an invalid success result")
            return state
        if state.get("status") in {"failed", "cancelled"}:
            error = state.get("error")
            code = error.get("code") if isinstance(error, dict) else "unknown_error"
            allowed = {
                "run_timeout", "provider_error", "gateway_timeout", "gateway_unavailable",
                "gateway_tenant_mismatch", "invalid_provider_response",
                "empty_provider_response", "provider_response_too_large",
                "tenant_credential_unavailable", "session_quarantined", "run_cancelled",
            }
            safe_code = (
                code if isinstance(code, str) and (
                    code in allowed or re.fullmatch(r"gateway_http_[1-5][0-9]{2}", code)
                ) else "unclassified_provider_error"
            )
            raise LiveAcceptanceError("live_run_failed", run_id=run_id, provider_code=safe_code)
        time.sleep(1)
    call(base, "/foundry/runs/" + run_id, tenant=tenant, method="DELETE")
    raise RuntimeError("Live run exceeded the test deadline; cancellation was requested")


def execute(
    base: str, a: Tenant, b: Tenant, progress: AcceptanceProgress | None = None,
) -> dict[str, object]:
    progress = progress if progress is not None else AcceptanceProgress()
    session = "live-" + secrets.token_hex(6)
    body = {"session_id": session, "prompt": "Synthetic test", "new_session": True}
    checks: dict[str, bool] = {}
    progress.checks = checks
    progress.checkpoint("negative_authorization_checks")
    missing, _ = call(base, "/foundry/runs", tenant=None, method="POST", body=body)
    invalid, _ = call(
        base, "/foundry/runs", tenant=Tenant("invalid", "invalid-pilot-key"),
        method="POST", body=body,
    )
    spoofed, _ = call(
        base, "/foundry/runs", tenant=a, claimed_tenant=b.name, method="POST", body=body,
    )
    alias, _ = call(
        base, "/foundry/runs", tenant=a, method="POST",
        body={**body, "model_alias": "unapproved-model"},
    )
    checks.update(
        missing_key_rejected=missing == 401,
        invalid_key_rejected=invalid == 401,
        tenant_spoof_rejected=spoofed == 403,
        unapproved_model_rejected=alias == 403,
    )
    progress.checkpoint("negative_authorization_checks_complete")
    if not all(checks.values()):
        raise LiveAcceptanceError("negative_authorization_checks_failed")

    def turn(tenant: Tenant, prompt: str, *, new_session: bool) -> dict[str, object]:
        stage = tenant.name + ("_setup" if new_session else "_recall")
        progress.turn_submit_attempts += 1
        progress.checkpoint(stage + "_submission")
        state = run_turn(base, tenant, session, prompt, new_session=new_session)
        progress.accepted_turns += 1
        progress.checkpoint(stage + "_accepted")
        return state

    marker_a = "SYNTHETIC_A_" + secrets.token_hex(8)
    marker_b = "SYNTHETIC_B_" + secrets.token_hex(8)
    first_a = turn(
        a,
        f"Remember this synthetic codeword for our conversation: {marker_a}. Reply ACK only.",
        new_session=True,
    )
    first_b = turn(
        b,
        f"Remember this synthetic codeword for our conversation: {marker_b}. Reply ACK only.",
        new_session=True,
    )
    foreign, _ = call(base, "/foundry/runs/" + str(first_a["run_id"]), tenant=b)
    checks["cross_tenant_run_read_rejected"] = foreign == 404
    again_a = turn(
        a, "Reply only with the exact codeword I asked you to remember.",
        new_session=False,
    )
    again_b = turn(
        b, "Reply only with the exact codeword I asked you to remember.",
        new_session=False,
    )
    result_a, result_b = again_a["result"], again_b["result"]
    if not isinstance(result_a, dict) or not isinstance(result_b, dict):
        raise RuntimeError("Invalid live continuation result")
    text_a, text_b = str(result_a["content"]), str(result_b["content"])
    checks["own_context_recall_observed"] = marker_a in text_a and marker_b in text_b
    checks["no_other_tenant_canary_observed"] = marker_b not in text_a and marker_a not in text_b
    checks["two_turns_per_tenant"] = again_a["turn"] == again_b["turn"] == 2
    deployments = {
        state["result"].get("deployment")
        for state in (first_a, first_b, again_a, again_b)
        if isinstance(state["result"], dict)
    }
    checks["one_shared_deployment"] = len(deployments) == 1 and None not in deployments
    checks["distinct_run_correlations"] = len({
        state["run_id"] for state in (first_a, first_b, again_a, again_b)
    }) == 4
    progress.checkpoint("continuation_acceptance_checks")
    if not all(checks.values()):
        raise LiveAcceptanceError("continuation_acceptance_checks_failed")
    measurements = []
    for state in (first_a, first_b, again_a, again_b):
        result = state["result"]
        if not isinstance(result, dict):
            raise RuntimeError("Invalid live result metadata")
        measurements.append({
            "tenant": state["tenant_id"],
            "turn": state["turn"],
            "elapsed_ms": state["elapsed_ms"],
            "reported_models": result.get("reported_models"),
            "input_tokens": result.get("input_tokens"),
            "output_tokens": result.get("output_tokens"),
            "cached_input_tokens": result.get("cached_input_tokens"),
        })
    progress.checkpoint("passed")
    return {
        "schema_version": 1,
        "status": "passed",
        "mode": "live_foundry_apim",
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
        "synthetic_tenants": 2,
        "successful_inference_calls": 4,
        "checks": checks,
        "measurements": measurements,
        "state_durability": "ephemeral_single_replica",
        "provider_kv_cache_partitioning": "not_verified",
        "fusion": "not_deployed_feature_enablement_blocked",
        "note": "Canary recall is an observation, not proof of provider cache isolation. Structural history/auth boundaries are also covered by deterministic tests.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run bounded paid Foundry/APIM tests with synthetic data.")
    parser.add_argument("--deployment-file", required=True, type=Path)
    parser.add_argument("--subscription", required=True, type=subscription_id)
    parser.add_argument("--allow-paid-model-calls", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("live-results.json"))
    args = parser.parse_args()
    if not args.allow_paid_model_calls:
        parser.error("--allow-paid-model-calls is required; the default mock runner never invokes this command")
    progress = AcceptanceProgress(args.deployment_file.resolve().parent / "acceptance-progress.json")
    try:
        with audit_commands(args.deployment_file.resolve().parent / "deploy-audit.log"):
            progress.checkpoint("deployment_evidence_validation")
            base, vault, group = deployment_target(args.deployment_file, args.subscription)
            progress.checkpoint("owned_gateway_validation")
            verify_owned_target(base, vault, group, args.subscription)
            progress.checkpoint("runner_configuration_readiness")
            warm_runner(args.deployment_file, group, args.subscription)
            progress.checkpoint("tenant_credential_loading")
            a = Tenant("customer-a", vault_secret(vault, "gateway-customer-a", args.subscription))
            b = Tenant("customer-b", vault_secret(vault, "gateway-customer-b", args.subscription))
            result = execute(base, a, b, progress)
    except (CloudCommandError, RuntimeError, ValueError, OSError) as exc:
        result = {
            "schema_version": 1,
            "status": "failed",
            "mode": "live_foundry_apim",
            "completed_at_utc": datetime.now(timezone.utc).isoformat(),
            "error_code": exc.code if isinstance(exc, LiveAcceptanceError) else "live_acceptance_failed",
            "error_type": type(exc).__name__,
            "failed_run_id": exc.run_id if isinstance(exc, LiveAcceptanceError) else None,
            "provider_error_code": exc.provider_code if isinstance(exc, LiveAcceptanceError) else None,
            "successful_inference_calls": None,
            "progress": progress.view(),
            "note": "The attempt failed. Tokens may already have been consumed; no automatic model retry or success fallback was used.",
        }
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, indent=2))
        raise SystemExit(1) from None
    encoded = json.dumps(result, indent=2)
    args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
