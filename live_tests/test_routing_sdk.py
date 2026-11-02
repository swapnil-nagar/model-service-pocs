import asyncio
import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import uvicorn
from copilot import CopilotClient
from copilot.session import ProviderConfig
from copilot.session_events import AssistantMessageData

from live_pocs.fusion import deny_permission
from live_pocs.routing_app import create_app
from live_pocs.routing_storage import SQLiteRegistry
from live_tests.test_routing import CREDENTIALS, routing_settings
from live_tests.test_routing_app import completion, sse


class RoutingSdkTests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(
        os.environ.get("MODEL_ROUTING_NATIVE_SDK_TEST") == "true",
        "Opt-in native SDK test; ordinary tests do not require a Copilot CLI runtime",
    )
    async def test_real_copilot_sdk_uses_the_bound_url_for_multiple_context_packed_turns(self):
        requests = []

        def provider(request):
            requests.append(request)
            if not json.loads(request.content).get("stream", False):
                return httpx.Response(200, json=completion(choices=[{
                    "index": 0, "message": {"role": "assistant", "content": "ACK"}, "finish_reason": "stop",
                }]))
            body = sse(
                {
                    "id": "fixture-chat", "object": "chat.completion.chunk", "created": 1793635200,
                    "model": "small-model-2026-01-01",
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ACK"}, "finish_reason": None}],
                },
                {
                    "id": "fixture-chat", "object": "chat.completion.chunk", "created": 1793635200,
                    "model": "small-model-2026-01-01",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 64, "completion_tokens": 1, "total_tokens": 65},
                },
            )
            return httpx.Response(200, content=body, headers={"Content-Type": "text/event-stream"})

        with tempfile.TemporaryDirectory(prefix="routing-native-sdk-") as directory, patch.dict(os.environ, CREDENTIALS):
            root = Path(directory)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                address = "http://127.0.0.1:" + str(listener.getsockname()[1])
                configured = routing_settings(root / "bindings.sqlite3").model_copy(update={"public_base_url": address})
                async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as upstream:
                    app = create_app(configured, registry=SQLiteRegistry(root / "bindings.sqlite3"), client=upstream)
                    server = uvicorn.Server(uvicorn.Config(
                        app, host="127.0.0.1", port=0, log_level="critical",
                        log_config=None, access_log=False,
                    ))
                    task = asyncio.create_task(server.serve(sockets=[listener]))
                    client = CopilotClient(
                        working_directory=str(root), base_directory=str(root / "sdk-state"),
                        mode="empty", use_logged_in_user=False, log_level="error",
                    )
                    try:
                        async with asyncio.timeout(15):
                            while not server.started:
                                if task.done():
                                    await task
                                    self.fail("Local model service did not start")
                                await asyncio.sleep(0.01)
                        async with httpx.AsyncClient(base_url=address, trust_env=False) as http:
                            response = await http.post(
                                "/sessions",
                                headers={"Authorization": "Bearer " + CREDENTIALS["ROUTING_CUSTOMER_A"]},
                                json={"session_id": "sdk-conversation", "model_alias": "economy"},
                            )
                            self.assertEqual(response.status_code, 201, response.text)
                            metadata = response.json()
                        await asyncio.wait_for(client.start(), timeout=20)
                        limits = metadata["provider_limits"]
                        session = await asyncio.wait_for(client.create_session(
                            session_id=metadata["sdk_session_id"], model=metadata["model_alias"],
                            provider=ProviderConfig(
                                type="openai", base_url=metadata["provider_base_url"],
                                wire_api="completions", api_key=CREDENTIALS["ROUTING_CUSTOMER_A"],
                                max_prompt_tokens=limits["max_prompt_tokens"],
                                max_output_tokens=limits["max_output_tokens"],
                            ),
                            on_permission_request=deny_permission,
                            available_tools=[], enable_config_discovery=False, enable_skills=False,
                            streaming=True,
                        ), timeout=20)
                        first = await session.send_and_wait("SYNTHETIC_SDK_FIRST_TURN", timeout=30)
                        second = await session.send_and_wait("SYNTHETIC_SDK_SECOND_TURN", timeout=30)
                        self.assertIsNotNone(first)
                        self.assertIsNotNone(second)
                        assert first is not None and second is not None
                        assert isinstance(first.data, AssistantMessageData)
                        assert isinstance(second.data, AssistantMessageData)
                        self.assertEqual(first.data.content, "ACK")
                        self.assertEqual(second.data.content, "ACK")
                        self.assertGreaterEqual(len(requests), 2)
                        for request in requests:
                            self.assertEqual(str(request.url), "https://small-provider.invalid/v1/chat/completions")
                            self.assertEqual(json.loads(request.content)["model"], "small-deployment")
                        last_messages = json.dumps(json.loads(requests[-1].content)["messages"])
                        self.assertIn("SYNTHETIC_SDK_FIRST_TURN", last_messages)
                        self.assertIn("SYNTHETIC_SDK_SECOND_TURN", last_messages)
                    finally:
                        try:
                            await asyncio.wait_for(client.stop(), timeout=10)
                        except TimeoutError:
                            await asyncio.wait_for(client.force_stop(), timeout=5)
                            raise
                        finally:
                            server.should_exit = True
                            await asyncio.wait_for(task, timeout=10)
