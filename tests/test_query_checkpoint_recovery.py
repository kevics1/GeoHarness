"""Pending tools resume through a new real SDK client and a saved Geo checkpoint."""

import json
from functools import partial

import httpx
from openai import AsyncOpenAI
from openharness.api import openai_client as upstream
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.engine.messages import ConversationMessage
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import ToolRegistry

from geoharness.api_client import GeoOpenAICompatibleClient
from geoharness.config.settings import AgentConfig, GeoConfig
from geoharness.engine import GeoQueryEngine
from geoharness.session_state import GeoSessionBackend, restore_geo_session_state


def _packet(delta, finish, usage=0):
    return "data: " + json.dumps({
        "id": "synthetic", "object": "chat.completion.chunk", "created": 0, "model": "test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": usage, "total_tokens": usage + 1},
    }) + "\n\ndata: [DONE]\n\n"


async def test_saved_pending_query_uses_verified_recovery_policy_in_new_client(
    monkeypatch, tmp_path,
):
    from openharness.tools.base import ToolResult

    from geoharness.tools.geo_python import GeoPythonTool

    class RecordedTool(GeoPythonTool):
        calls = 0

        async def execute(self, arguments, context):
            self.calls += 1
            return ToolResult(output="one completed synthetic tool")

        def is_read_only(self, arguments):
            return True

    config = GeoConfig(model="primary(max)", agent=AgentConfig(
        empty_response_fallback_model="configured-fallback", empty_response_max_tokens=4096,
    ))
    responses = [
        _packet({"reasoning_content": "synthetic"}, "length", 16384),
        _packet({"tool_calls": [{"index": 0, "id": "saved-tool", "type": "function",
            "function": {"name": "geo_python", "arguments": '{"code":"print(1)"}'}}]},
            "tool_calls", 2),
        _packet({}, "stop"),
        _packet({"content": "Complete"}, "stop", 1),
    ]
    requests = []

    async def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=responses[len(requests) - 1])

    def client():
        transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        monkeypatch.setattr(upstream, "AsyncOpenAI", partial(AsyncOpenAI, http_client=transport))
        return GeoOpenAICompatibleClient("test-only", settings=config.agent)

    tool = RecordedTool()
    registry = ToolRegistry()
    registry.register(tool)

    def engine(api):
        return GeoQueryEngine(
            api_client=api, tool_registry=registry, cwd=tmp_path,
            permission_checker=PermissionChecker(settings=PermissionSettings()),
            model=config.model, system_prompt="test", max_tokens=config.agent.max_tokens,
            tool_metadata={"geoharness_config": config}, max_turns=5,
        )

    monkeypatch.setattr("geoharness.session_state.get_config_dir", lambda: tmp_path / "state")
    first_api = client()
    first = engine(first_api)
    try:
        [event async for event in first.submit_message("Map the data")]
        assert first.has_pending_continuation() and tool.calls == 1
        assert first.total_usage.output_tokens == 16386
        backend = GeoSessionBackend()
        backend.save_snapshot(
            cwd=tmp_path, model=config.model, system_prompt="test", messages=first.messages,
            usage=first.total_usage, tool_metadata=first.tool_metadata,
        )
        saved = backend.load_latest(tmp_path)
    finally:
        await first_api._sdk_client.close()
    second_api = client()
    second = engine(second_api)
    try:
        second.load_messages([ConversationMessage.model_validate(item)
                              for item in saved["messages"]])
        assert restore_geo_session_state(second.tool_metadata, saved["tool_metadata"],
                                         cwd=tmp_path, config=config)
        [event async for event in second.submit_message("继续")]
        assert requests[-1]["model"] == "configured-fallback"
        assert requests[-1]["max_tokens"] == 4096
        assert tool.calls == 1 and second.messages[-1].text == "Complete"
        assert second.tool_metadata["task_focus_state"]["goal"] == "Map the data"
        with second_api.query_recovery_scope(second._query_recovery_state):
            assert second_api.consume_unsettled_usage() == UsageSnapshot()
    finally:
        await second_api._sdk_client.close()
