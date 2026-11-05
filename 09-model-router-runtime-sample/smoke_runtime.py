import argparse
import asyncio
import json
import math
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict
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


class WeatherEvidence(TypedDict):
    temperature_c: float
    conditions: str | None
    observed_at: str
    station_id: str


def verify_weather_result(result: object) -> WeatherEvidence:
    if not isinstance(result, str):
        raise RuntimeError("The weather tool result must be its JSON string.")
    data = json.loads(result)
    if not isinstance(data, dict) or data.get("status") != "ok" or data.get("source") != "US National Weather Service":
        raise RuntimeError("The weather tool did not return a successful NWS observation.")
    if data.get("requested_location") != {"latitude": 47.6062, "longitude": -122.3321}:
        raise RuntimeError("The weather tool used coordinates other than the requested Seattle location.")
    station = data.get("station")
    if not isinstance(station, dict) or not isinstance(station.get("id"), str):
        raise RuntimeError("The weather tool did not identify its observation station.")
    station_id = station["id"]
    if not re.fullmatch(r"[A-Za-z0-9]{3,12}", station_id):
        raise RuntimeError("The weather tool returned an invalid observation station.")
    if data.get("source_url") != f"https://api.weather.gov/stations/{station['id']}/observations/latest":
        raise RuntimeError("The weather observation has an unexpected provider URL.")
    temperature = data.get("temperature_c")
    if isinstance(temperature, bool) or not isinstance(temperature, int | float) or not math.isfinite(temperature):
        raise RuntimeError("The weather observation has no finite temperature.")
    stamp = data.get("observed_at")
    if not isinstance(stamp, str):
        raise RuntimeError("The weather observation has no timestamp.")
    observed_at = datetime.fromisoformat(stamp)
    if observed_at.tzinfo is None:
        raise RuntimeError("The weather timestamp has no timezone.")
    age = datetime.now(UTC) - observed_at
    if not -timedelta(minutes=5) <= age <= timedelta(hours=2):
        raise RuntimeError("The weather observation is stale or in the future.")
    conditions = data.get("conditions")
    if conditions is not None and not isinstance(conditions, str):
        raise RuntimeError("The weather conditions must be text or explicitly missing.")
    return {
        "temperature_c": float(temperature), "conditions": conditions,
        "observed_at": stamp, "station_id": station_id,
    }


def verify_weather_answer(answer: str, result: object) -> None:
    data = verify_weather_result(result)
    match = re.fullmatch(
        r"Weather near Seattle: (-?\d+(?:\.\d+)?) C; conditions: (.*); observed: (.*); station: ([A-Za-z0-9]+); source: US National Weather Service\.",
        answer.strip(),
    )
    if (
        not match or float(match[1]) != data["temperature_c"]
        or match[2] != (data.get("conditions") or "not reported")
        or match[3] != data["observed_at"] or match[4] != data["station_id"]
    ):
        raise RuntimeError("The weather answer does not match the runtime-observed temperature, conditions, timestamp, and station.")


async def verify_customer(
    client: httpx.AsyncClient, base_url: str, customer: str,
    checks: list[dict[str, object]] | None = None,
    pause_seconds: int = 15,
    scenario: str = "inventory",
) -> dict[str, object]:
    url = urlsplit(base_url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Runtime test URLs must be local HTTP addresses.")
    root = base_url.rstrip("/") + "/agents/inventory"
    prompt = "How many units of A-101 can ship today?"
    tool_name = "lookup_stock"
    if scenario == "weather":
        tool_name = "get_weather"
        prompt = (
            'How is the weather near Seattle? '
            'Reply exactly "Weather near Seattle: <temperature_c> C; conditions: <conditions>; observed: <observed_at>; '
            'station: <id>; source: US National Weather Service." Use the actual tool values unchanged; '
            'use "not reported" only if conditions is null. If the tool fails, explain why and do not invent a reading.'
        )
    elif scenario != "inventory":
        raise ValueError("Scenario must be inventory or weather.")
    response = await client.post(root + "/chat", json={"prompt": prompt})
    response.raise_for_status()
    result = response.json()
    calls = result.get("tool_calls", [])
    matching_calls = [call for call in calls if call.get("tool_name") == tool_name]
    if not matching_calls:
        raise RuntimeError(f"The JSON run has no runtime-observed {tool_name} tool call.")
    for call in matching_calls:
        if call.get("success") is not True:
            raise RuntimeError(f"The {tool_name} tool call did not succeed.")
        if scenario == "weather":
            verify_weather_result(call.get("result"))
        else:
            verify_stock_result(call.get("result"), customer)
    if scenario == "weather":
        verify_weather_answer(result["response"], matching_calls[-1].get("result"))
    else:
        verify_answer(result["response"], customer)
    if checks is not None:
        checks.append({
            "customer": customer, "scenario": scenario, "mode": "json",
            "passed": True, "tool_calls": len(matching_calls),
        })
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
    tool_ids = {
        event["tool_call_id"] for event in events
        if event.get("type") == "tool_start" and event.get("tool_name") == tool_name and event.get("tool_call_id")
    }
    if not tool_ids:
        raise RuntimeError("The stream contains no runtime-observed tool call.")
    completed_ids = set()
    last_result = None
    for event in events:
        if event.get("type") == "tool_end" and event.get("tool_call_id") in tool_ids:
            last_result = event.get("result")
            if scenario == "weather":
                verify_weather_result(last_result)
            else:
                verify_stock_result(last_result, customer)
            completed_ids.add(event["tool_call_id"])
    if completed_ids != tool_ids:
        raise RuntimeError(f"The stream did not complete every {tool_name} tool call.")
    deltas = [event.get("content") for event in events if event.get("type") == "delta" and event.get("content")]
    if len(deltas) < 2:
        raise RuntimeError("The answer was not delivered in multiple incremental deltas.")
    answer = "".join(deltas)
    if scenario == "weather":
        verify_weather_answer(answer, last_result)
    else:
        verify_answer(answer, customer)
    if checks is not None:
        checks.append({
            "customer": customer, "scenario": scenario, "mode": "stream", "passed": True,
            "tool_calls": len(completed_ids), "delta_count": len(deltas), "terminal": "done",
        })
    return {
        "customer": customer,
        "scenario": scenario,
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
    parser.add_argument("--scenario", choices=["inventory", "weather", "mixed"], default="inventory")
    args = parser.parse_args()
    if not 0 <= args.pause_seconds <= 300:
        parser.error("--pause-seconds must be between 0 and 300.")
    if not args.approve_paid_calls:
        parser.error("This sends real model requests through your running apps; pass --approve-paid-calls.")
    results = []
    checks = []
    evidence = {
        "scope": "native-maf-runtime-live-model-router",
        "scenario": args.scenario,
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
                scenario = "weather" if args.scenario == "weather" or (args.scenario == "mixed" and customer == "B") else "inventory"
                results.append(await verify_customer(client, url, customer, checks, args.pause_seconds, scenario))
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
