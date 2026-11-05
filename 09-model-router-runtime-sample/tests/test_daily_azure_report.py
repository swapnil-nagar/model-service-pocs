import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parents[1] / "azure-functions-agents-runtime"
sys.path.insert(0, str(ROOT))

from test_basic_chat import call_endpoint, response_for  # noqa: E402
from test_flow import ProviderStream, stream_chunks  # noqa: E402

import smoke_daily_azure_report  # noqa: E402
from native_samples import REPORT_ADDENDUM, REPORT_SUBSCRIPTION, prepare_profile  # noqa: E402
from smoke_daily_azure_report import verify_fixture_result, verify_stream_events  # noqa: E402

spec = importlib.util.spec_from_file_location("report_fixture_tool", ROOT / "daily-azure-report" / "azure_rest.py")
fixture_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_tool)
SUMMARY = {
    "total_resources": 3,
    "by_type": {"Microsoft.Web/sites": 2, "Microsoft.Storage/storageAccounts": 1},
    "by_location": {"eastus2": 2, "westus": 1},
}


@pytest.fixture
def native_report(monkeypatch, tmp_path):
    profile_root = tmp_path / "profile"
    profile = prepare_profile(RUNTIME, profile_root, "daily-azure-report")
    for name in (
        "AZURE_OPENAI_BASE_URL", "OPENAI_BASE_URL", "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "APPLICATIONINSIGHTS_AUTHENTICATION_STRING", "AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER",
        "ACA_SESSION_POOL_ENDPOINT",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "AZURE_FUNCTIONS_AGENTS_PROVIDER": "azure_openai",
        "AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT": "false",
        "AZURE_OPENAI_ENDPOINT": "https://poc09.example.invalid/llm",
        "AZURE_OPENAI_API_KEY": "synthetic-apim-key",
        "AZURE_OPENAI_API_VERSION": "2024-10-21",
        "AZURE_OPENAI_DEPLOYMENT": "service-default",
        "AZURE_FUNCTIONS_AGENTS_APP_ROOT": profile["app_root"],
        "AZURE_FUNCTIONS_AGENTS_SESSION_DIR": str(tmp_path / "sessions"),
        "AzureWebJobsStorage": "",
        "SUBSCRIPTION_ID": REPORT_SUBSCRIPTION,
        "POC09_CUSTOMER": "A",
    }.items():
        monkeypatch.setenv(name, value)
    from azure_functions_agents.config import paths

    monkeypatch.setattr(paths, "_app_root", None)
    spec = importlib.util.spec_from_file_location(
        "original_daily_report", Path(profile["script_root"]) / "function_app.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == (
        RUNTIME / "samples" / "daily-azure-report" / "src" / "function_app.py"
    ).resolve()
    return {fn.get_function_name(): fn for fn in module.app.get_functions()}


def install_transport(monkeypatch, *, summary=False, invalid_tool_path=False, interrupted=False):
    observed = {"requests": [], "terminal_sent": False}

    async def send(client, request, *, stream=False, **kwargs):
        assert request.url.host == "poc09.example.invalid"
        assert request.url.path == "/llm/openai/v1/responses"
        assert request.headers["api-key"] == "synthetic-apim-key"
        body = json.loads(request.content)
        assert body["model"] == "service-default" and body["store"] is False
        assert body["max_output_tokens"] == 256 and "previous_response_id" not in body
        assert {tool["name"] for tool in body["tools"]} == {"azure_rest"}
        tool = body["tools"][0]
        assert set(tool["parameters"]["properties"]) == {"path", "method"}
        assert tool["parameters"]["additionalProperties"] is False
        assert "SYNTHETIC dataset" in json.dumps(body) and "MUST call azure_rest" in json.dumps(body)
        observed["requests"].append(body)
        outputs = [item for item in body["input"] if item.get("type") == "function_call_output"]
        if outputs:
            assert outputs[0]["call_id"] == "call-azure-1"
            if invalid_tool_path:
                assert "synthetic inventory path" in outputs[0]["output"]
                result = response_for("Tool unavailable: the requested path is not permitted. Email not sent.")
            else:
                output = json.loads(outputs[0]["output"])
                assert output["summary"] == SUMMARY and output["synthetic"] is True
                assert output["email_sent"] is False
                result = response_for(json.dumps(SUMMARY) if summary else (
                    "SYNTHETIC - DRY RUN - EMAIL NOT SENT. 3 resources: 1 created, 1 updated."
                ))
        else:
            result = response_for("")
            result["output"] = [{
                "id": "fc-azure-1", "call_id": "call-azure-1", "type": "function_call",
                "name": "azure_rest", "status": "completed",
                "arguments": json.dumps({
                    "path": "/subscriptions/foreign/resources?api-version=2021-04-01"
                    if invalid_tool_path else fixture_tool.RESOURCE_PATH, "method": "GET",
                }),
            }]
        if body.get("stream"):
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  stream=ProviderStream(stream_chunks(result, interrupted), observed, interrupted))
        return httpx.Response(200, request=request, json=result)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return observed


def summary_request():
    async def receive():
        return {"type": "http.request", "body": json.dumps({
            "subscription_id": REPORT_SUBSCRIPTION,
        }).encode(), "more_body": False}

    return Request({
        "type": "http", "method": "POST", "path": "/resource-summary",
        "headers": [(b"content-type", b"application/json")], "query_string": b"", "scheme": "http",
        "server": ("localhost", 7074), "client": ("127.0.0.1", 1), "http_version": "1.1",
    }, receive)


async def test_native_structured_resource_summary(monkeypatch, native_report):
    observed = install_transport(monkeypatch, summary=True)
    response = await native_report["resource_summary"].get_user_function()(summary_request())
    assert response.status_code == 200 and json.loads(response.body) == SUMMARY
    assert len(observed["requests"]) == 2


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("tool_failure", [False, True])
async def test_native_report_tool_loop_and_failures(monkeypatch, native_report, streaming, tool_failure):
    observed = install_transport(monkeypatch, invalid_tool_path=tool_failure)
    response = await call_endpoint(native_report, "Summarize Azure inventory.", streaming=streaming)
    assert response.status_code == 200
    if streaming:
        events = []
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            events.append(json.loads(text.removeprefix("data:").strip()))
        assert events[-1]["type"] == "done"
        start = next(event for event in events if event["type"] == "tool_start")
        end = next(event for event in events if event["type"] == "tool_end")
        assert start["tool_name"] == "azure_rest" and start["tool_call_id"] == end["tool_call_id"]
        deltas = [event["content"] for event in events if event["type"] == "delta"]
        assert len(deltas) >= 2
        if not tool_failure:
            assert verify_stream_events(events) == len(deltas)
        answer = "".join(deltas)
    else:
        result = json.loads(response.body)
        assert result["tool_calls"][0]["tool_name"] == "azure_rest"
        assert result["tool_calls"][0]["success"] is not tool_failure
        answer = result["response"]
    assert ("Tool unavailable" in answer) is tool_failure
    assert "email not sent" in answer.lower()
    assert len(observed["requests"]) == 2


async def test_native_timer_dry_run_uses_real_agent_and_fixture(monkeypatch, native_report):
    observed = install_transport(monkeypatch)
    timer = native_report["daily_azure_report"]
    bindings = timer.get_dict_repr()["bindings"]
    assert next(item for item in bindings if item["type"] == "timerTrigger")["schedule"] == "0 0 7 * * *"
    await timer.get_user_function()({"isPastDue": False})
    assert len(observed["requests"]) == 2
    assert "Daily Azure Resource Report" in json.dumps(observed["requests"][0])
    assert "EMAIL NOT SENT" in json.dumps(observed["requests"][1])


async def test_native_report_rejects_partial_stream(monkeypatch, native_report):
    install_transport(monkeypatch, interrupted=True)
    response = await call_endpoint(native_report, "Summarize inventory.", streaming=True)
    events = []
    async for chunk in response.body_iterator:
        text = chunk.decode() if isinstance(chunk, bytes) else chunk
        events.append(json.loads(text.removeprefix("data:").strip()))
    assert any(event["type"] in {"error", "stream_truncated"} for event in events)
    assert not any(event["type"] == "done" for event in events)


@pytest.mark.parametrize("customer", ["A", "B"])
def test_fixture_authoritative_counts_and_reporting_window(customer):
    result = fixture_tool.inventory_result(customer, datetime(2026, 11, 5, tzinfo=UTC))
    assert result["summary"] == SUMMARY
    assert result["changes"] == {"created": 1, "updated": 1}
    assert result["lookback_hours"] == 24 and result["email_sent"] is False
    assert result["synthetic"] is True and customer in result["source"]
    assert {resource["change"] for resource in result["value"]} == {"created", "updated", "unchanged"}


@pytest.mark.parametrize("values", [
    {"method": "POST"}, {"body": "{}"}, {"query": "value"}, {"customer": "B"}, {"url": "https://example.invalid"},
])
def test_fixture_schema_rejects_writes_and_scope_overrides(values):
    with pytest.raises(ValidationError):
        fixture_tool.AzureRestParams(path=fixture_tool.RESOURCE_PATH, **values)


def test_fixture_refuses_live_subscription(monkeypatch, caplog):
    monkeypatch.setenv("SUBSCRIPTION_ID", "live-subscription")
    result = fixture_tool.read_inventory(fixture_tool.AzureRestParams(path=fixture_tool.RESOURCE_PATH))
    assert result["status"] == "error" and "summary" not in result
    assert result["error"]["code"] == "unsupported_scope_or_path"
    assert "unsupported_scope_or_path" in caplog.text


def test_report_profile_keeps_original_entrypoint_and_agent_sources(tmp_path):
    root = tmp_path / "profile"
    profile = prepare_profile(RUNTIME, root, "daily-azure-report")
    source = Path(profile["script_root"])
    assert not (root / "function_app.py").exists() and not (root / "mcp.json").exists()
    for name in ("main.agent.md", "daily_azure_report.agent.md", "resource_summary.agent.md"):
        assert (root / name).read_bytes() == (source / name).read_bytes() + REPORT_ADDENDUM.encode()
    assert (root / "tools" / "azure_rest.py").read_bytes() == (
        ROOT / "daily-azure-report" / "azure_rest.py"
    ).read_bytes()
    launcher = (ROOT / "Start-Agent.ps1").read_text()
    assert "AzureWebJobs.daily_azure_report.Disabled', 'true'" in launcher


def test_report_profile_refuses_inherited_connector(tmp_path):
    root = tmp_path / "profile"
    root.mkdir()
    (root / "mcp.json").write_text('{"servers":{"outlook":{}}}')
    with pytest.raises(RuntimeError, match="Unexpected report profile file"):
        prepare_profile(RUNTIME, root, "daily-azure-report")


def test_fixture_rejects_missing_customer_and_naive_clock(monkeypatch):
    monkeypatch.setenv("SUBSCRIPTION_ID", REPORT_SUBSCRIPTION)
    with pytest.raises(RuntimeError, match="customer A or B"):
        fixture_tool.inventory_result("")
    with pytest.raises(ValueError, match="timezone-aware"):
        fixture_tool.inventory_result("A", datetime(2026, 11, 5))


def report_events():
    return [
        {"type": "tool_start", "tool_name": "azure_rest", "tool_call_id": "call-1"},
        {"type": "tool_end", "tool_name": "azure_rest", "tool_call_id": "call-1",
         "result": json.dumps(fixture_tool.inventory_result("A"))},
        {"type": "delta", "content": "Synthetic: 3 resources. "},
        {"type": "delta", "content": "Email not sent."},
        {"type": "done"},
    ]


def test_report_checker_accepts_grounded_incremental_stream():
    assert verify_stream_events(report_events()) == 2
    verify_fixture_result(json.dumps(fixture_tool.inventory_result("B")))


@pytest.mark.parametrize("case", ["missing_done", "no_tool", "wrong_call", "error", "truncated", "wrong_count",
                                "one_delta", "email_claim", "live_data", "wrong_tool_counts"])
def test_report_checker_rejects_ungrounded_or_partial_success(case):
    events = report_events()
    if case == "missing_done":
        events.pop()
    elif case == "no_tool":
        del events[:2]
    elif case == "wrong_call":
        events[1]["tool_call_id"] = "foreign"
    elif case in {"error", "truncated"}:
        events.insert(-1, {"type": "error" if case == "error" else "stream_truncated"})
    elif case == "wrong_count":
        events[2]["content"] = "Synthetic: 13 resources. "
    elif case == "one_delta":
        events[2]["content"] = "Synthetic: 3 resources. Email not sent."
        events.pop(3)
    elif case == "email_claim":
        events[3]["content"] = "Email was sent."
    else:
        result = json.loads(events[1]["result"])
        if case == "live_data":
            result["synthetic"] = False
        else:
            result["summary"]["total_resources"] = 18
        events[1]["result"] = json.dumps(result)
    with pytest.raises(RuntimeError):
        verify_stream_events(events)


@pytest.mark.parametrize("fail", [False, True])
async def test_report_checker_persists_honest_partial_evidence(monkeypatch, tmp_path, fail):
    output = tmp_path / "evidence.json"
    monkeypatch.setattr(sys, "argv", [
        "smoke_daily_azure_report.py", "--approve-paid-calls", "--output", str(output),
    ])

    async def verify(client, base_url, checks, pause_seconds):
        checks.append({"mode": "timer-configuration", "disabled": True, "passed": True})
        if fail:
            raise RuntimeError("Synthetic invented summary.")

    monkeypatch.setattr(smoke_daily_azure_report, "verify_report", verify)
    if fail:
        with pytest.raises(RuntimeError, match="invented summary"):
            await smoke_daily_azure_report.main()
    else:
        await smoke_daily_azure_report.main()
    evidence = json.loads(output.read_text())
    assert evidence["passed"] is not fail and evidence["email_sent"] is False
    assert evidence["checks"][0]["passed"] is True
    assert evidence.get("failure_type") == ("RuntimeError" if fail else None)
