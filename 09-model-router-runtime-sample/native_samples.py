import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Literal

ROOT = Path(__file__).parent
REPORT_SUBSCRIPTION = "00000000-0000-0000-0000-000000000009"
REPORT_ADDENDUM = f"""

## POC 09 dry-run contract (overrides conflicting instructions above)

- Before answering ANY inventory or report request, you MUST call azure_rest
  exactly once in this turn. Never answer from memory, invent example values,
  or copy counts from the response example. No tool result means no report.
- This is a fixed SYNTHETIC dataset owned by the tool, NOT an invitation to
  generate imaginary inventory. It is not live Azure data. Use only the azure_rest
  fixture tool with GET and path
  /subscriptions/{REPORT_SUBSCRIPTION}/resources?api-version=2021-04-01.
- The tool computes authoritative summary counts and changes in the last
  24 hours. Do not invent resources or recalculate those counts.
- Email is unavailable. Never send email or claim it was sent. For reports,
  return a compact HTML preview labelled "SYNTHETIC - DRY RUN - EMAIL NOT SENT".
  Include the counts and changed resource names; stay within 256 output tokens.
- For the structured resource-summary endpoint, return only the tool's summary
  object matching the response contract, without prose or additional fields.
- For interactive chat, report the counts and say "synthetic" and "email not sent".
- Azure writes, external HTTP, documentation MCP and Outlook are unavailable.
  If a requested capability is missing or a tool fails, explicitly explain
  that failure. Do not fabricate a successful report or silently substitute data.
"""


def prepare_profile(
    runtime_repo: Path, profile_root: Path,
    sample: Literal["basic-chat", "daily-azure-report"] = "basic-chat",
) -> dict[str, str]:
    if sample not in {"basic-chat", "daily-azure-report"}:
        raise ValueError("Unsupported native sample.")
    source = runtime_repo.resolve() / "samples" / sample / "src"
    agents = ["main.agent.md"]
    if sample == "daily-azure-report":
        agents.extend(["daily_azure_report.agent.md", "resource_summary.agent.md"])
    for name in ("function_app.py", "host.json", *agents):
        if not (source / name).is_file():
            raise RuntimeError(f"The native {sample} sample is missing {source / name}.")
    if (source / "local.settings.json").exists():
        raise RuntimeError(
            f"The native {sample} sample has local.settings.json, which can override the gateway "
            "environment. Use a clean runtime checkout; do not overwrite existing settings."
        )
    if sample == "daily-azure-report" and profile_root.exists():
        expected = {*agents, "agents.config.yaml", "tools/azure_rest.py"}
        for path in profile_root.rglob("*"):
            if path.is_file():
                relative = path.relative_to(profile_root)
                if "__pycache__" in relative.parts and path.suffix == ".pyc":
                    continue
                if relative.as_posix() not in expected:
                    raise RuntimeError(f"Unexpected report profile file: {path}. Use a fresh profile directory.")
    profile_root.mkdir(parents=True, exist_ok=True)
    for name in agents:
        content = (source / name).read_bytes()
        if sample == "daily-azure-report":
            content += REPORT_ADDENDUM.encode("utf-8")
        (profile_root / name).write_bytes(content)
    shutil.copyfile(ROOT / "basic-chat" / "agents.config.yaml", profile_root / "agents.config.yaml")
    if sample == "daily-azure-report":
        (profile_root / "tools").mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "daily-azure-report" / "azure_rest.py", profile_root / "tools" / "azure_rest.py")
    return {
        "script_root": str(source),
        "app_root": str(profile_root.resolve()),
        "entrypoint_sha256": hashlib.sha256((source / "function_app.py").read_bytes()).hexdigest(),
        "agent_sha256": hashlib.sha256((source / "main.agent.md").read_bytes()).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare an opt-in profile for an original native sample.")
    parser.add_argument("--runtime-repo", required=True, type=Path)
    parser.add_argument("--profile-root", required=True, type=Path)
    parser.add_argument("--sample", choices=["basic-chat", "daily-azure-report"], default="basic-chat")
    args = parser.parse_args()
    print(json.dumps(prepare_profile(args.runtime_repo, args.profile_root, args.sample)))


if __name__ == "__main__":
    main()
