import argparse
import asyncio
import json
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def verify_local_url(base_url: str) -> str:
    url = urlsplit(base_url)
    if (
        url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"}
        or url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}
    ):
        raise ValueError("Use a local HTTP runtime base URL without a path, credentials, query, or fragment.")
    return base_url.rstrip("/")


async def verify_basic_chat(
    client: httpx.AsyncClient, base_url: str, checks: list[dict[str, object]], pause_seconds: int = 15,
) -> None:
    root = verify_local_url(base_url) + "/agents/main"
    ui = await client.get(root + "/")
    ui.raise_for_status()
    if "text/html" not in ui.headers.get("content-type", ""):
        raise RuntimeError("The native basic-chat UI did not return HTML.")
    code = "ORBIT-" + secrets.token_hex(4).upper()
    first = await client.post(root + "/chat", json={
        "prompt": f"Remember the code {code} for this conversation. Reply with only that code.",
    })
    first.raise_for_status()
    result = first.json()
    session_id = result.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("The runtime did not return a resumable conversation ID.")
    if result.get("response", "").strip() != code or result.get("tool_calls"):
        raise RuntimeError("The initial JSON chat did not return the requested code without tools.")
    checks.append({"mode": "json", "turn": 1, "passed": True})
    headers = {"x-ms-session-id": session_id}
    await asyncio.sleep(pause_seconds)
    second = await client.post(root + "/chat", headers=headers, json={
        "prompt": "What code did I ask you to remember? Reply with only that code.",
    })
    second.raise_for_status()
    result = second.json()
    if result.get("session_id") != session_id or result.get("response", "").strip() != code or result.get("tool_calls"):
        raise RuntimeError("The second JSON chat did not recall the first turn in the same conversation.")
    checks.append({"mode": "json", "turn": 2, "history_recalled": True, "passed": True})
    await asyncio.sleep(pause_seconds)
    await verify_stream(client, root, session_id, code, checks)


async def verify_stream(
    client: httpx.AsyncClient, root: str, session_id: str, code: str,
    checks: list[dict[str, object]],
) -> None:
    headers = {"x-ms-session-id": session_id}
    events = []
    data = []
    async with client.stream("POST", root + "/chatstream", headers=headers, json={
        "prompt": (
            "Recall the code I asked you to remember. Reply only: Saved code: <the code>"
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
    if data or not events or events[-1].get("type") != "done":
        raise RuntimeError("The native SSE stream ended without a complete done event.")
    if any(event.get("type") in {"error", "stream_truncated", "tool_start", "tool_end"} for event in events):
        raise RuntimeError("The basic-chat stream failed, was truncated, or executed an unexpected tool.")
    sessions = [event for event in events if event.get("type") == "session"]
    if not sessions or sessions[0].get("session_id") != session_id:
        raise RuntimeError("The native SSE stream did not continue the existing runtime conversation.")
    deltas = [event["content"] for event in events if event.get("type") == "delta" and event.get("content")]
    history_recalled = re.fullmatch(rf"Saved code: {re.escape(code)}\.?", "".join(deltas).strip()) is not None
    check: dict[str, object] = {
        "mode": "sse", "turn": 3, "history_recalled": history_recalled,
        "delta_count": len(deltas), "terminal": "done", "passed": False,
    }
    checks.append(check)
    if len(deltas) < 2 or not history_recalled:
        raise RuntimeError("The streamed answer was not incremental or did not match the saved conversation code.")
    check["passed"] = True


async def main() -> None:
    parser = argparse.ArgumentParser(description="Three paid inference requests against native basic-chat.")
    parser.add_argument("--base-url", default="http://localhost:7073")
    parser.add_argument("--approve-paid-calls", action="store_true")
    parser.add_argument("--pause-seconds", type=int, default=15)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.approve_paid_calls:
        parser.error("Pass --approve-paid-calls only after approving three live model calls.")
    if not 0 <= args.pause_seconds <= 300:
        parser.error("--pause-seconds must be between 0 and 300.")
    verify_local_url(args.base_url)
    checks: list[dict[str, object]] = []
    evidence: dict[str, object] = {
        "scope": "original-basic-chat-native-maf-poc09", "checks": checks, "passed": False,
    }
    try:
        async with httpx.AsyncClient(timeout=150, trust_env=False, follow_redirects=False) as client:
            await verify_basic_chat(client, args.base_url, checks, args.pause_seconds)
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
