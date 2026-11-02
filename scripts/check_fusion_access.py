import argparse
import asyncio
import json
import subprocess
from tempfile import TemporaryDirectory

from copilot import CopilotClient, RuntimeConnection

from live_pocs.fusion import configure_sdk_logging, deny_permission, stop_client


def github_token(expected_login: str) -> str:
    try:
        identity = subprocess.run(
            ["gh", "api", "--hostname", "github.com", "user", "--jq", ".login"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if identity.returncode or identity.stdout.strip() != expected_login:
            raise RuntimeError("GitHub CLI identity does not match the explicitly approved account")
        credential = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com", "--user", expected_login],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("GitHub CLI authentication lookup failed") from None
    token = credential.stdout.strip()
    if credential.returncode or not token or not token.isascii():
        raise RuntimeError("The approved GitHub credential is not available")
    return token


async def probe(
    expected_login: str,
    selector: str,
    *,
    cli_catalog_only: bool = False,
    validate_session_selector: bool = False,
    experimental_session: bool = False,
) -> dict[str, object]:
    configure_sdk_logging()
    token = github_token(expected_login)
    with TemporaryDirectory(prefix="fusion-discovery-") as directory:
        client = CopilotClient(
            connection=RuntimeConnection.for_stdio(),
            mode="copilot-cli" if cli_catalog_only else "empty",
            github_token=token,
            use_logged_in_user=False,
            base_directory=directory,
            working_directory=directory,
            log_level="none",
            telemetry=None,
        )
        try:
            await asyncio.wait_for(client.start(), timeout=45)
            auth = await asyncio.wait_for(client.get_auth_status(), timeout=15)
            runtime = await asyncio.wait_for(client.get_status(), timeout=15)
            models = await asyncio.wait_for(client.list_models(), timeout=30)
            candidates = [
                {"id": model.id, "name": model.name}
                for model in models
                if "fusion" in model.id.lower() or "fusion" in model.name.lower()
            ]
            selector_accepted: bool | None = None
            selector_error: str | None = None
            if validate_session_selector:
                try:
                    session = await asyncio.wait_for(
                        client.create_session(
                            model=selector,
                            enable_experimental_mode=experimental_session,
                            github_token=token,
                            on_permission_request=deny_permission,
                            available_tools=[],
                            skip_custom_instructions=True,
                            enable_config_discovery=False,
                            enable_skills=False,
                            enable_file_hooks=False,
                            enable_host_git_operations=False,
                            enable_session_store=False,
                            working_directory=directory,
                        ),
                        timeout=25,
                    )
                except Exception as exc:
                    selector_accepted = False
                    selector_error = type(exc).__name__
                else:
                    selector_accepted = True
                    await session.disconnect()
                    await client.delete_session(session.session_id)
            return {
                "mode": "sdk_catalog_discovery_only",
                "client_mode": "copilot-cli" if cli_catalog_only else "empty",
                "github_login": expected_login,
                "sdk_authenticated": auth.isAuthenticated,
                "sdk_login": auth.login,
                "runtime_version": runtime.version,
                "catalog_model_count": len(models),
                "configured_selector": selector,
                "configured_selector_available": any(model.id == selector for model in models),
                "fusion_catalog_entries": candidates,
                "selector_session_accepted": selector_accepted,
                "selector_validation_error_type": selector_error,
                "experimental_session": experimental_session,
                "model_inference_calls": 0,
            }
        finally:
            await stop_client(client)


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover Fusion availability; never send a prompt.")
    parser.add_argument("--github-account", required=True)
    parser.add_argument("--model", default="fusion")
    parser.add_argument(
        "--cli-catalog-only", action="store_true",
        help="Metadata-only diagnostic; never used by the hosted runner and never sends prompts.",
    )
    parser.add_argument(
        "--validate-session-selector", action="store_true",
        help="Create and delete a tool-disabled empty session to validate the selector; never sends a prompt.",
    )
    parser.add_argument(
        "--experimental-session", action="store_true",
        help="Opt into the SDK's documented experimental-mode setting for a metadata-only session.",
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(
            probe(
                args.github_account,
                args.model,
                cli_catalog_only=args.cli_catalog_only,
                validate_session_selector=args.validate_session_selector,
                experimental_session=args.experimental_session,
            )
        )
    except Exception as exc:
        print(json.dumps({"error": "fusion_catalog_discovery_failed", "error_type": type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, indent=2))
    if not result["configured_selector_available"] and not result["selector_session_accepted"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
