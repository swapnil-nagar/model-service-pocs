import asyncio
import importlib.util
import json
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
from azure_functions_agents import HostedSkill
from pydantic import ValidationError
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent-app"))
sys.path.insert(0, str(ROOT))

from tools.inventory import StockQuery, stock_result  # noqa: E402

import smoke_runtime  # noqa: E402
from smoke_runtime import verify_answer, verify_stock_result  # noqa: E402


@pytest.mark.parametrize("fail", [False, True])
async def test_smoke_persists_honest_partial_or_complete_evidence(monkeypatch, tmp_path, fail):
    output = tmp_path / "evidence.json"
    monkeypatch.setattr(sys, "argv", [
        "smoke_runtime.py", "--approve-paid-calls", "--output", str(output), "--pause-seconds", "0",
    ])

    async def verify(client, url, customer, checks, pause_seconds):
        checks.append({"customer": customer, "mode": "json", "passed": True})
        if fail:
            raise RuntimeError("Synthetic incomplete stream.")
        checks.append({"customer": customer, "mode": "stream", "passed": True})
        return {"customer": customer, "passed": True}

    monkeypatch.setattr(smoke_runtime, "verify_customer", verify)
    if fail:
        with pytest.raises(RuntimeError, match="Synthetic incomplete stream"):
            await smoke_runtime.main()
    else:
        await smoke_runtime.main()
    evidence = json.loads(output.read_text())
    assert evidence["passed"] is not fail
    assert len(evidence["checks"]) == (1 if fail else 4)
    assert len(evidence["customer_runs"]) == (0 if fail else 2)
    assert evidence.get("failure_type") == ("RuntimeError" if fail else None)


@pytest.mark.parametrize(("customer", "stock"), [("A", 7), ("B", 2)])
def test_customer_fixture(customer, stock):
    assert stock_result("A-101", customer) == {
        "sku": "A-101", "on_hand": stock, "reserved": 2,
        "available_to_ship": max(0, stock - 2), "source": f"customer-{customer}-inventory"
    }


@pytest.mark.parametrize(("sku", "customer", "error"), [
    ("A-101", "foreign", RuntimeError), ("foreign", "A", ValueError)
])
def test_fixture_rejects_foreign_scope(sku, customer, error):
    with pytest.raises(error):
        stock_result(sku, customer)


def test_model_cannot_supply_customer_scope():
    with pytest.raises(ValidationError):
        StockQuery(sku="A-101", customer="B")


@pytest.mark.parametrize("customer", ["A", "B"])
def test_smoke_validates_actual_tool_output(customer):
    verify_stock_result(json.dumps(stock_result("A-101", customer)), customer)
    verify_answer(f"{5 if customer == 'A' else 0} units can ship; source: customer-{customer}-inventory.", customer)


@pytest.mark.parametrize("answer", [
    "0 units can ship; source: customer-A-inventory.",
    "5 units can ship; source: customer-B-inventory.",
    "0 of 5 units can ship; source: customer-A-inventory.",
])
def test_smoke_rejects_incorrect_answer(answer):
    with pytest.raises(RuntimeError):
        verify_answer(answer, "A")


@pytest.mark.parametrize("result", [
    "Error: Argument parsing failed.",
    json.dumps(stock_result("A-101", "B")),
    None,
])
def test_smoke_rejects_failed_or_foreign_tool_result(result):
    with pytest.raises((RuntimeError, json.JSONDecodeError)):
        verify_stock_result(result, "A")


def test_gateway_policy_and_routes():
    policy = ET.parse(ROOT / "infra" / "policy.xml").getroot()
    forward = policy.find("backend/forward-request")
    assert forward is not None and forward.get("buffer-response") == "false"
    assert {node.get("resource") for node in policy.findall(".//authentication-managed-identity")} == {
        "https://cognitiveservices.azure.com", "https://ai.azure.com"
    }
    assert policy.find("outbound/set-body") is None
    text = (ROOT / "infra" / "policy.xml").read_text()
    assert "poc09-a" in text and "poc09-b" in text and "invalid_model" in text
    template = (ROOT / "infra" / "main.bicep").read_text()
    assert "name: 'Developer'" in template
    assert "/openai/deployments/service-default/chat/completions" in template
    assert "header: 'api-key'" in template
    assert "disableLocalAuth: true" in template
    assert "gpt-4.1-mini" in template and "routing:" in template
    assert "https://${foundry.name}.openai.azure.com" in template
    assert "Microsoft.CognitiveServices/accounts/projects@2025-06-01" in template
    assert "allowProjectManagement: true" in template
    assert "scope: project" in template
    assert "/api/projects/__PROJECT_NAME__/openai/v1/responses" in text
    key_loader = (ROOT / "Load-Keys.ps1").read_text()
    assert "/listSecrets?api-version=2024-05-01" in key_loader
    assert "az apim subscription" not in key_loader


class ProviderStream(httpx.AsyncByteStream):
    def __init__(self, chunks, observed, interrupted=False):
        self.chunks = chunks
        self.observed = observed
        self.interrupted = interrupted

    async def __aiter__(self):
        for chunk in self.chunks:
            await asyncio.sleep(0.01)
            if b'"type": "response.completed"' in chunk:
                self.observed["terminal_sent"] = True
            yield chunk
        if self.interrupted:
            raise httpx.ReadError("Synthetic interrupted provider stream.")


def completion(body, customer):
    has_result = any(item.get("type") == "function_call_output" for item in body["input"])
    if has_result:
        tool_output = next(item for item in body["input"] if item.get("type") == "function_call_output")
        assert json.loads(tool_output["output"]) == stock_result("A-101", customer)
        output = [{
            "id": "msg-stock-1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": f"{5 if customer == 'A' else 0} units can ship; source: customer-{customer}-inventory.", "annotations": []}],
        }]
    else:
        output = [{
            "id": "fc-stock-1", "call_id": "call-stock-1", "type": "function_call",
            "name": "lookup_stock", "arguments": '{"sku":"A-101"}', "status": "completed",
        }]
    return {
        "id": "resp-fixture", "object": "response", "created_at": 1793804400,
        "model": "gpt-4.1-mini", "status": "completed", "output": output,
        "error": None, "incomplete_details": None, "parallel_tool_calls": True,
        "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20, "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}},
    }


def stream_chunks(result, interrupted=False):
    item = result["output"][0]
    initial = dict(result, status="in_progress", output=[])
    added_item = dict(item, status="in_progress")
    if item["type"] == "function_call":
        added_item["arguments"] = ""
    else:
        added_item["content"] = []
    events = [
        {"type": "response.created", "response": initial},
        {"type": "response.output_item.added", "output_index": 0, "item": added_item},
    ]
    if item["type"] == "function_call":
        events.extend([
            {"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": 0, "delta": '{"sku":'},
            {"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": 0, "delta": '"A-101"}'},
            {"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": 0, "arguments": item["arguments"]},
        ])
    else:
        content = item["content"][0]
        events.append({"type": "response.content_part.added", "item_id": item["id"], "output_index": 0, "content_index": 0, "part": dict(content, text="")})
        for text in (content["text"][:12], content["text"][12:]):
            events.append({"type": "response.output_text.delta", "item_id": item["id"], "output_index": 0, "content_index": 0, "delta": text})
        events.append({"type": "response.output_text.done", "item_id": item["id"], "output_index": 0, "content_index": 0, "text": content["text"]})
    if interrupted:
        events = events[:3]
    else:
        events.extend([
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": result},
        ])
    chunks = []
    for number, event in enumerate(events):
        event["sequence_number"] = number
        chunks.append((f"event: {event['type']}\ndata: " + json.dumps(event) + "\n\n").encode())
    return chunks


def runtime_app(monkeypatch, customer):
    for name, value in {
        "AZURE_FUNCTIONS_AGENTS_PROVIDER": "azure_openai",
        "AZURE_OPENAI_ENDPOINT": "https://poc09.example.invalid/llm",
        "AZURE_OPENAI_API_KEY": "synthetic-apim-key",
        "AZURE_OPENAI_API_VERSION": "2024-10-21",
        "AZURE_OPENAI_DEPLOYMENT": "service-default",
        "AZURE_FUNCTIONS_AGENTS_ENABLE_COPILOT": "false",
        "AzureWebJobsStorage": "",
        "POC09_CUSTOMER": customer,
    }.items():
        monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location(f"poc09_app_{customer}", ROOT / "agent-app" / "function_app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app


def install_transport(monkeypatch, customer, *, interrupted=False):
    observed = {"requests": [], "terminal_sent": False}

    async def send(client, request, *, stream=False, **kwargs):
        body = json.loads(request.content)
        assert request.url.host == "poc09.example.invalid"
        assert request.url.path == "/llm/openai/v1/responses"
        assert request.headers["api-key"] == "synthetic-apim-key"
        assert body["model"] == "service-default"
        assert body["store"] is False
        assert "Reserve two units of every SKU" in json.dumps(body)
        inventory_tool = next(tool for tool in body["tools"] if tool["name"] == "lookup_stock")
        assert set(inventory_tool["parameters"]["properties"]) == {"sku"}
        assert inventory_tool["parameters"]["additionalProperties"] is False
        assert set(body) <= {
            "model", "input", "instructions", "tools", "tool_choice", "stream", "store",
            "temperature", "top_p", "max_output_tokens", "parallel_tool_calls", "text",
            "reasoning", "include", "metadata", "background", "truncation",
        }
        observed["requests"].append(body)
        result = completion(body, customer)
        if body.get("stream"):
            return httpx.Response(
                200, request=request, headers={"content-type": "text/event-stream"},
                stream=ProviderStream(stream_chunks(result, interrupted), observed, interrupted),
            )
        return httpx.Response(200, request=request, json=result)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    return observed


@pytest.mark.parametrize("name", ["AZURE_OPENAI_BASE_URL", "OPENAI_BASE_URL"])
def test_runtime_rejects_nonempty_base_url_override(monkeypatch, name):
    monkeypatch.setenv(name, "https://unexpected.example.invalid")
    with pytest.raises(RuntimeError, match="Remove base-URL overrides"):
        runtime_app(monkeypatch, "A")


async def test_native_runtime_removes_empty_base_url_aliases(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_BASE_URL", "")
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    app = runtime_app(monkeypatch, "A")
    assert "AZURE_OPENAI_BASE_URL" not in os.environ
    assert "OPENAI_BASE_URL" not in os.environ
    observed = install_transport(monkeypatch, "A")

    @app.hosted_skill(arg_name="skill", agent_name="inventory")
    async def invoke(skill: HostedSkill):
        return await skill.run("How many units of A-101 can ship?")

    result = await invoke()
    verify_answer(result.content, "A")
    assert len(observed["requests"]) == 2
    verify_stock_result(result.tool_calls[0]["result"], "A")


@pytest.mark.parametrize("customer", ["A", "B"])
async def test_native_runtime_json_tool_round_trip(monkeypatch, customer):
    app = runtime_app(monkeypatch, customer)
    observed = install_transport(monkeypatch, customer)

    @app.hosted_skill(arg_name="skill", agent_name="inventory")
    async def invoke(skill: HostedSkill):
        return await skill.run("How many units of A-101 can ship?")

    result = await invoke()
    assert f"{5 if customer == 'A' else 0} units" in result.content
    assert any(call["tool_name"] == "lookup_stock" for call in result.tool_calls)
    for call in result.tool_calls:
        assert call["success"] is True
        verify_stock_result(call["result"], customer)
    assert len(observed["requests"]) == 2
    output = next(item for item in observed["requests"][1]["input"] if item.get("type") == "function_call_output")
    assert output["call_id"] == "call-stock-1"
    assert json.loads(output["output"]) == stock_result("A-101", customer)


async def test_native_runtime_streams_and_assembles_tool_call(monkeypatch):
    app = runtime_app(monkeypatch, "A")
    observed = install_transport(monkeypatch, "A")
    events = []

    @app.hosted_skill(arg_name="skill", agent_name="inventory")
    async def invoke(skill: HostedSkill):
        async for event in skill.stream("How many units of A-101 can ship?"):
            if event.kind.value == "tool_end":
                assert observed["terminal_sent"]
            events.append(event.to_dict())

    await invoke()
    assert len(observed["requests"]) == 2
    assert all(request["stream"] for request in observed["requests"])
    output = next(item for item in observed["requests"][1]["input"] if item.get("type") == "function_call_output")
    assert output["call_id"] == "call-stock-1"
    assert json.loads(output["output"]) == stock_result("A-101", "A")
    assert any(event["type"] == "tool_start" and event["tool_name"] == "lookup_stock" for event in events)
    stock_starts = [event for event in events if event["type"] == "tool_start" and event["tool_name"] == "lookup_stock"]
    stock_ends = [event for event in events if event["type"] == "tool_end"]
    assert {event["tool_call_id"] for event in stock_starts} == {event["tool_call_id"] for event in stock_ends}
    for event in stock_ends:
        verify_stock_result(event["result"], "A")
    assert events[-1]["type"] == "done"
    text = "".join(event.get("content") or "" for event in events if event["type"] == "delta")
    verify_answer(text, "A")


async def test_native_runtime_does_not_execute_interrupted_arguments(monkeypatch):
    app = runtime_app(monkeypatch, "A")
    observed = install_transport(monkeypatch, "A", interrupted=True)
    events = []

    @app.hosted_skill(arg_name="skill", agent_name="inventory")
    async def invoke(skill: HostedSkill):
        async for event in skill.stream("How many units of A-101 can ship?"):
            events.append(event.to_dict())

    await invoke()
    assert len(observed["requests"]) == 1
    assert not any(event["type"] in {"tool_start", "tool_end"} for event in events)
    assert any(event["type"] == "error" for event in events)


@pytest.mark.parametrize("customer", ["A", "B"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_native_http_endpoint_shapes(monkeypatch, customer, streaming):
    app = runtime_app(monkeypatch, customer)
    observed = install_transport(monkeypatch, customer)
    suffix = "chatstream" if streaming else "chat"
    function = next(function for function in app.get_functions() if function.get_function_name() == f"agent_inventory_builtin_{suffix}")

    async def receive():
        return {
            "type": "http.request",
            "body": json.dumps({"prompt": "How many units of A-101 can ship?"}).encode(),
            "more_body": False,
        }

    request = Request({
        "type": "http", "method": "POST", "path": f"/agents/inventory/{suffix}",
        "headers": [(b"content-type", b"application/json")], "query_string": b"",
        "scheme": "http", "server": ("localhost", 7089), "client": ("127.0.0.1", 1),
        "http_version": "1.1",
    }, receive)
    response = await function.get_user_function()(request)
    assert response.status_code == 200
    if not streaming:
        result = json.loads(response.body)
        verify_answer(result["response"], customer)
        assert result["tool_calls"][0]["success"] is True
        verify_stock_result(result["tool_calls"][0]["result"], customer)
    else:
        assert response.headers["content-type"].startswith("text/event-stream")
        events = []
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            events.append(json.loads(text.removeprefix("data:").strip()))
        assert events[-1]["type"] == "done"
        start = next(event for event in events if event["type"] == "tool_start")
        end = next(event for event in events if event["type"] == "tool_end")
        assert start["tool_name"] == "lookup_stock"
        assert start["tool_call_id"] == end["tool_call_id"]
        verify_stock_result(end["result"], customer)
        deltas = [event["content"] for event in events if event["type"] == "delta"]
        assert len(deltas) >= 2
        verify_answer("".join(deltas), customer)
    assert len(observed["requests"]) == 2
