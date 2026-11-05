import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT.parents[1] / "azure-functions-agents-runtime"
sys.path.insert(0, str(ROOT))

from test_flow import ProviderStream, stream_chunks  # noqa: E402

from native_samples import prepare_profile  # noqa: E402
from smoke_basic_chat import verify_basic_chat, verify_local_url  # noqa: E402


@pytest.fixture
def native_basic_chat(monkeypatch, tmp_path):
    profile_root = tmp_path / "profile"
    profile = prepare_profile(RUNTIME, profile_root)
    assert (profile_root / "main.agent.md").read_bytes() == (
        RUNTIME / "samples" / "basic-chat" / "src" / "main.agent.md"
    ).read_bytes()
    assert profile["entrypoint_sha256"] == hashlib.sha256(
        (Path(profile["script_root"]) / "function_app.py").read_bytes()
    ).hexdigest()
    for name in (
        "AZURE_OPENAI_BASE_URL", "OPENAI_BASE_URL", "APPLICATIONINSIGHTS_CONNECTION_STRING",
        "APPLICATIONINSIGHTS_AUTHENTICATION_STRING", "AZURE_FUNCTIONS_AGENTS_SESSION_CONTAINER",
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
    }.items():
        monkeypatch.setenv(name, value)
    from azure_functions_agents.config import paths

    monkeypatch.setattr(paths, "_app_root", None)
    spec = importlib.util.spec_from_file_location(
        "original_basic_chat", Path(profile["script_root"]) / "function_app.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == (
        RUNTIME / "samples" / "basic-chat" / "src" / "function_app.py"
    ).resolve()
    return {fn.get_function_name(): fn for fn in module.app.get_functions()}


async def call_endpoint(app, prompt, *, streaming=False, session_id=None):
    suffix = "chatstream" if streaming else "chat"
    function = app[f"agent_main_builtin_{suffix}"]

    async def receive():
        return {"type": "http.request", "body": json.dumps({"prompt": prompt}).encode(), "more_body": False}

    headers = [(b"content-type", b"application/json")]
    if session_id:
        headers.append((b"x-ms-session-id", session_id.encode()))
    request = Request({
        "type": "http", "method": "POST", "path": f"/agents/main/{suffix}",
        "headers": headers, "query_string": b"", "scheme": "http",
        "server": ("localhost", 7073), "client": ("127.0.0.1", 1), "http_version": "1.1",
    }, receive)
    return await function.get_user_function()(request)


def response_for(text):
    return {
        "id": "resp-basic", "object": "response", "created_at": 1793890800,
        "model": "gpt-4.1-mini", "status": "completed", "error": None, "incomplete_details": None,
        "output": [{
            "id": "msg-basic", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {"input_tokens": 10, "output_tokens": 12, "total_tokens": 22},
    }


@pytest.mark.parametrize("streaming", [False, True])
async def test_original_basic_chat_native_history_and_protocol(monkeypatch, native_basic_chat, streaming):
    observed = {"requests": [], "terminal_sent": False}
    code = "ORBIT-731"

    async def send(client, request, *, stream=False, **kwargs):
        assert request.url.host == "poc09.example.invalid"
        assert request.url.path == "/llm/openai/v1/responses"
        assert request.headers["api-key"] == "synthetic-apim-key"
        body = json.loads(request.content)
        assert body["model"] == "service-default" and body["store"] is False
        assert body["max_output_tokens"] == 256
        assert not body.get("tools") and "previous_response_id" not in body
        assert "You are a helpful assistant." in json.dumps(body)
        assert code in json.dumps(body["input"]), "Runtime did not resend prior conversation history."
        observed["requests"].append(body)
        result = response_for(code if len(observed["requests"]) == 1 else f"Saved code: {code}.")
        if body.get("stream"):
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  stream=ProviderStream(stream_chunks(result), observed))
        return httpx.Response(200, request=request, json=result)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    first = await call_endpoint(native_basic_chat, f"Remember {code}.")
    assert first.status_code == 200
    first_result = json.loads(first.body)
    assert first_result["response"] == code and first_result["tool_calls"] == []
    session_id = first_result["session_id"]
    assert session_id
    second = await call_endpoint(
        native_basic_chat, "What code did I ask you to remember?", streaming=streaming, session_id=session_id,
    )
    assert second.status_code == 200
    if streaming:
        events = []
        async for chunk in second.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            events.append(json.loads(text.removeprefix("data:").strip()))
        assert events[-1]["type"] == "done"
        assert not any(event["type"] in {"error", "tool_start", "tool_end"} for event in events)
        assert next(event for event in events if event["type"] == "session")["session_id"] == session_id
        deltas = [event["content"] for event in events if event["type"] == "delta"]
        assert len(deltas) >= 2 and "".join(deltas) == f"Saved code: {code}."
    else:
        result = json.loads(second.body)
        assert result["response"] == f"Saved code: {code}." and result["session_id"] == session_id
    assert len(observed["requests"]) == 2
    assert "What code did I ask" in json.dumps(observed["requests"][1]["input"])


@pytest.mark.parametrize("streaming", [False, True])
async def test_original_basic_chat_surfaces_gateway_auth_failure(monkeypatch, native_basic_chat, streaming):
    async def send(client, request, **kwargs):
        return httpx.Response(401, request=request, json={
            "error": {"code": "invalid_subscription", "message": "Synthetic rejected APIM key."},
        })

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    response = await call_endpoint(native_basic_chat, "Hello", streaming=streaming)
    if streaming:
        events = []
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            events.append(json.loads(text.removeprefix("data:").strip()))
        assert any(event["type"] == "error" for event in events)
        assert not any(event["type"] == "done" for event in events)
    else:
        assert response.status_code == 500 and "error" in json.loads(response.body)


def test_prepare_profile_rejects_missing_sample(tmp_path):
    with pytest.raises(RuntimeError, match="native basic-chat sample is missing"):
        prepare_profile(tmp_path / "missing", tmp_path / "profile")


def test_launcher_removes_base_url_variables_not_just_their_values():
    powershell = shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is required to exercise the Windows launcher environment.")
    source = (ROOT / "Start-Agent.ps1").read_text(encoding="utf-8")
    cleanup = source.split("foreach ($name in @(", 1)[1].split("$appDirectory =", 1)[0]
    script = "foreach ($name in @(" + cleanup
    script += (
        f"\n& '{sys.executable}' -c "
        "\"import os; assert 'AZURE_OPENAI_BASE_URL' not in os.environ; "
        "assert 'OPENAI_BASE_URL' not in os.environ\""
        "\nexit $LASTEXITCODE"
    )
    subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-Command", script],
        env=dict(os.environ, AZURE_OPENAI_BASE_URL="", OPENAI_BASE_URL=""),
        check=True, capture_output=True, text=True,
    )


def test_prepare_profile_preserves_existing_local_settings(tmp_path):
    source = tmp_path / "runtime" / "samples" / "basic-chat" / "src"
    source.mkdir(parents=True)
    for name in ("function_app.py", "main.agent.md", "host.json"):
        (source / name).write_bytes((RUNTIME / "samples" / "basic-chat" / "src" / name).read_bytes())
    settings = source / "local.settings.json"
    settings.write_text('{"Values":{}}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="can override the gateway"):
        prepare_profile(tmp_path / "runtime", tmp_path / "profile")
    assert settings.read_text(encoding="utf-8") == '{"Values":{}}'
    assert not (tmp_path / "profile").exists()


@pytest.mark.parametrize("failure", [None, "wrong_history", "wrong_stream_history", "missing_done", "single_delta", "tool_error"])
async def test_live_verifier_requires_history_incremental_deltas_and_terminal(failure):
    state = {"calls": 0}
    checks = []

    def handle(request):
        if request.method == "GET":
            return httpx.Response(200, text="<html>Native chat UI</html>", headers={"content-type": "text/html"})
        state["calls"] += 1
        prompt = json.loads(request.content)["prompt"]
        if state["calls"] == 1:
            code = prompt.split("Remember the code ", 1)[1].split(" ", 1)[0]
            state["code"] = code
            return httpx.Response(200, json={"session_id": "native-session", "response": code, "tool_calls": []})
        assert request.headers["x-ms-session-id"] == "native-session"
        assert state["code"] not in prompt
        if state["calls"] == 2:
            return httpx.Response(200, json={
                "session_id": "native-session",
                "response": "wrong" if failure == "wrong_history" else state["code"], "tool_calls": [],
            })
        text = "Saved code: wrong" if failure == "wrong_stream_history" else f"Saved code: {state['code']}."
        events = [{"type": "session", "session_id": "native-session"}]
        pieces = [text] if failure == "single_delta" else [text[:12], text[12:]]
        events.extend({"type": "delta", "content": piece} for piece in pieces)
        if failure == "tool_error":
            events.append({"type": "error", "message": "Synthetic failure."})
        if failure != "missing_done":
            events.append({"type": "done"})
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content="".join("data: " + json.dumps(event) + "\n\n" for event in events))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        if failure:
            with pytest.raises(RuntimeError):
                await verify_basic_chat(client, "http://localhost:7073", checks, pause_seconds=0)
        else:
            await verify_basic_chat(client, "http://localhost:7073", checks, pause_seconds=0)
            assert len(checks) == 3 and checks[-1]["delta_count"] == 2


@pytest.mark.parametrize("url", [
    "https://localhost:7073", "http://example.com", "http://user:password@localhost:7073",
    "http://localhost:7073/agents/main", "http://localhost:7073?key=value",
])
def test_smoke_rejects_nonlocal_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        verify_local_url(url)
