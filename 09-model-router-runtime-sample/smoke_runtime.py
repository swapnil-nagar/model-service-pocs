import argparse
import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx


def verify_answer(answer: str, customer: str) -> None:
    expected = {"A": 5, "B": 0}[customer]
    if not re.fullmatch(rf"{expected} units can ship; source: customer-{customer}-inventory\.", answer.strip()):
        raise RuntimeError(f"Customer {customer}: incorrect quantity, source, or answer format: {answer!r}.")


def verify_stock_result(result: object, customer: str) -> None:
    if not isinstance(result, str):
        raise RuntimeError("The inventory tool result must be its JSON string.")
    expected = {
        "sku": "A-101", "on_hand": {"A": 7, "B": 2}[customer], "reserved": 2,
        "available_to_ship": {"A": 5, "B": 0}[customer], "source": f"customer-{customer}-inventory",
    }
    if json.loads(result) != expected:
        raise RuntimeError(f"Customer {customer}: incorrect runtime-observed inventory result.")


async def verify_customer(
    client: httpx.AsyncClient, base_url: str, customer: str,
    checks: list[dict[str, object]] | None = None,
    pause_seconds: int = 15,
) -> dict[str, object]:
    url = urlsplit(base_url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Runtime test URLs must be local HTTP addresses.")
    root = base_url.rstrip("/") + "/agents/inventory"
    prompt = 'Use lookup_stock for A-101. How many units can ship today? Reply exactly "<quantity> units can ship; source: <source>." Substitute the calculated digit and tool source.'
    response = await client.post(root + "/chat", json={"prompt": prompt})
    response.raise_for_status()
    result = response.json()
    verify_answer(result["response"], customer)
    calls = result.get("tool_calls", [])
    stock_calls = [call for call in calls if call.get("tool_name") == "lookup_stock"]
    if not stock_calls:
        raise RuntimeError("The JSON run has no runtime-observed inventory tool call.")
    for call in stock_calls:
        if call.get("success") is not True:
            raise RuntimeError("The inventory tool call did not succeed.")
        verify_stock_result(call.get("result"), customer)
    if checks is not None:
        checks.append({"customer": customer, "mode": "json", "passed": True, "tool_calls": len(stock_calls)})
    if pause_seconds:
        await asyncio.sleep(pause_seconds)
    start = time.monotonic()
    first_event = None
    events = []
    async with client.stream("POST", root + "/chatstream", json={"prompt": prompt}) as stream:
        stream.raise_for_status()
        if not stream.headers.get("content-type", "").startswith("text/event-stream"):
            raise RuntimeError("The runtime did not return SSE.")
        async for line in stream.aiter_lines():
            if line.startswith("data: "):
                if first_event is None:
                    first_event = time.monotonic() - start
                events.append(json.loads(line[6:]))
    if not events or events[-1].get("type") != "done":
        kinds = [event.get("type") for event in events]
        raise RuntimeError(f"Runtime stream ended without a done event; observed event types: {kinds!r}.")
    if any(event.get("type") in {"error", "stream_truncated"} for event in events):
        raise RuntimeError("Runtime stream reported an error or truncation.")
    stock_ids = {
        event["tool_call_id"] for event in events
        if event.get("type") == "tool_start" and event.get("tool_name") == "lookup_stock" and event.get("tool_call_id")
    }
    if not stock_ids:
        raise RuntimeError("The stream contains no runtime-observed tool call.")
    completed_ids = set()
    for event in events:
        if event.get("type") == "tool_end" and event.get("tool_call_id") in stock_ids:
            verify_stock_result(event.get("result"), customer)
            completed_ids.add(event["tool_call_id"])
    if completed_ids != stock_ids:
        raise RuntimeError("The stream did not complete every inventory tool call.")
    deltas = [event.get("content") for event in events if event.get("type") == "delta" and event.get("content")]
    if len(deltas) < 2:
        raise RuntimeError("The answer was not delivered in multiple incremental deltas.")
    answer = "".join(deltas)
    verify_answer(answer, customer)
    if checks is not None:
        checks.append({
            "customer": customer, "mode": "stream", "passed": True,
            "tool_calls": len(completed_ids), "delta_count": len(deltas), "terminal": "done",
        })
    return {
        "customer": customer,
        "json_tool_calls": len(calls),
        "stream_event_count": len(events),
        "runtime_first_event_seconds": first_event,
        "runtime_total_seconds": time.monotonic() - start,
        "passed": True,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--customer-a-url", default="http://localhost:7071")
    parser.add_argument("--customer-b-url", default="http://localhost:7072")
    parser.add_argument("--approve-paid-calls", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pause-seconds", type=int, default=15)
    args = parser.parse_args()
    if not 0 <= args.pause_seconds <= 300:
        parser.error("--pause-seconds must be between 0 and 300.")
    if not args.approve_paid_calls:
        parser.error("This sends real model requests through your running apps; pass --approve-paid-calls.")
    results = []
    checks = []
    evidence = {
        "scope": "native-maf-runtime-live-model-router",
        "customer_runs": results,
        "checks": checks,
        "passed": False,
        "pause_between_runtime_requests_seconds": args.pause_seconds,
        "limits": "Runtime events do not prove the synthetic 250 ms APIM forwarding target, selected-model attribution, or cross-model continuation.",
    }
    try:
        async with httpx.AsyncClient(timeout=150, follow_redirects=False, trust_env=False) as client:
            for customer, url in (("A", args.customer_a_url), ("B", args.customer_b_url)):
                if results and args.pause_seconds:
                    await asyncio.sleep(args.pause_seconds)
                results.append(await verify_customer(client, url, customer, checks, args.pause_seconds))
        evidence["passed"] = True
    except Exception as exc:
        evidence["failure_type"] = type(exc).__name__
        raise
    finally:
        output = json.dumps(evidence, indent=2)
        if args.output:
            args.output.write_text(output + "\n", encoding="utf-8")
        print(output)


if __name__ == "__main__":
    asyncio.run(main())
