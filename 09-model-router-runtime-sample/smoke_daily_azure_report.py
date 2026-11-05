import argparse
import asyncio
import json
import re
from pathlib import Path

import httpx

from native_samples import REPORT_SUBSCRIPTION
from smoke_basic_chat import verify_local_url

EXPECTED_SUMMARY = {
    "total_resources": 3,
    "by_type": {"Microsoft.Web/sites": 2, "Microsoft.Storage/storageAccounts": 1},
    "by_location": {"eastus2": 2, "westus": 1},
}


def verify_fixture_result(raw: str) -> None:
    result = json.loads(raw)
    if (
        result.get("status") != "ok" or result.get("synthetic") is not True
        or result.get("email_sent") is not False or result.get("summary") != EXPECTED_SUMMARY
        or result.get("changes") != {"created": 1, "updated": 1}
        or result.get("lookback_hours") != 24
    ):
        raise RuntimeError("The tool did not return the expected synthetic read-only inventory.")


def verify_stream_events(events: list[dict[str, object]]) -> int:
    if not events or events[-1].get("type") != "done":
        raise RuntimeError("The native report stream ended without done.")
    if any(event.get("type") in {"error", "stream_truncated"} for event in events):
        raise RuntimeError("The native report stream failed or was truncated.")
    starts = [event for event in events if event.get("type") == "tool_start"]
    ends = [event for event in events if event.get("type") == "tool_end"]
    if (
        len(starts) != 1 or len(ends) != 1 or starts[0].get("tool_name") != "azure_rest"
        or not isinstance(starts[0].get("tool_call_id"), str) or not starts[0]["tool_call_id"]
        or starts[0].get("tool_call_id") != ends[0].get("tool_call_id")
    ):
        raise RuntimeError("Expected one matched runtime-owned azure_rest tool execution.")
    result = ends[0].get("result")
    if not isinstance(result, str):
        raise RuntimeError("Missing serialized tool result in the native stream.")
    verify_fixture_result(result)
    deltas: list[str] = []
    for event in events:
        content = event.get("content")
        if event.get("type") == "delta" and isinstance(content, str) and content:
            deltas.append(content)
    answer = "".join(deltas).lower()
    if (
        len(deltas) < 2 or "synthetic" not in answer or "email not sent" not in answer
        or re.search(r"\b3\b", answer) is None
    ):
        raise RuntimeError("The answer was not incremental, did not report the count, or omitted the dry-run disclosure.")
    return len(deltas)


async def verify_report(
    client: httpx.AsyncClient, base_url: str, checks: list[dict[str, object]], pause_seconds: int = 15,
) -> None:
    root = verify_local_url(base_url)
    ui = await client.get(root + "/agents/main/")
    ui.raise_for_status()
    if "text/html" not in ui.headers.get("content-type", ""):
        raise RuntimeError("The native report UI did not return HTML.")
    timer = await client.get(root + "/admin/functions/daily_azure_report")
    timer.raise_for_status()
    if timer.json().get("isDisabled") is not True:
        raise RuntimeError("The automatic daily report timer must be disabled before testing.")
    checks.append({"mode": "timer-configuration", "disabled": True, "passed": True})
    summary = await client.post(root + "/resource-summary", json={"subscription_id": REPORT_SUBSCRIPTION})
    summary.raise_for_status()
    if summary.json() != EXPECTED_SUMMARY:
        raise RuntimeError("The native resource-summary response did not match the authoritative fixture counts.")
    checks.append({"mode": "resource-summary-json", "schema_and_counts": True, "passed": True})
    await asyncio.sleep(pause_seconds)
    events: list[dict[str, object]] = []
    data: list[str] = []
    async with client.stream("POST", root + "/agents/main/chatstream", json={
        "prompt": (
            "Call azure_rest once to summarize the inventory. Reply briefly with the total number "
            "of resources, created and updated counts. Explicitly say synthetic and email not sent."
        ),
    }) as response:
        response.raise_for_status()
        if not response.headers.get("content-type", "").startswith("text/event-stream"):
            raise RuntimeError("The runtime did not return native SSE.")
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line and data:
                events.append(json.loads("\n".join(data)))
                data = []
    if data:
        raise RuntimeError("The stream ended with an unterminated SSE frame.")
    count = verify_stream_events(events)
    checks.append({"mode": "sse-tool-round-trip", "delta_count": count, "terminal": "done", "passed": True})


async def main() -> None:
    parser = argparse.ArgumentParser(description="Two native report requests, normally four paid model calls.")
    parser.add_argument("--base-url", default="http://localhost:7074")
    parser.add_argument("--approve-paid-calls", action="store_true")
    parser.add_argument("--pause-seconds", type=int, default=15)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.approve_paid_calls:
        parser.error("Pass --approve-paid-calls only after approving live model calls.")
    if not 0 <= args.pause_seconds <= 300:
        parser.error("--pause-seconds must be between 0 and 300.")
    verify_local_url(args.base_url)
    checks: list[dict[str, object]] = []
    evidence: dict[str, object] = {
        "scope": "original-daily-azure-report-native-maf-poc09", "inventory": "synthetic",
        "email_sent": False, "checks": checks, "passed": False,
    }
    try:
        async with httpx.AsyncClient(timeout=150, trust_env=False, follow_redirects=False) as client:
            await verify_report(client, args.base_url, checks, args.pause_seconds)
        evidence["passed"] = True
    except Exception as exc:
        evidence["failure_type"] = type(exc).__name__
        raise
    finally:
        if args.output:
            args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
