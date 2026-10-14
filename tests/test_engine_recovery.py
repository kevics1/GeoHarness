"""Real tool loop regression for empty provider responses after successful tools."""

from openharness.api.client import ApiMessageCompleteEvent, ApiRetryEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import BaseTool, ToolRegistry, ToolResult
from pydantic import BaseModel

from geoharness.engine import GeoQueryEngine


def _engine(client, tmp_path):
    return GeoQueryEngine(
        api_client=client, tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings()),
        cwd=tmp_path, model="test", system_prompt="test",
    )


class UnsettledClient:
    def __init__(self):
        self.pending = UsageSnapshot()

    def ack_usage(self, usage):
        self.pending = UsageSnapshot(
            input_tokens=max(0, self.pending.input_tokens - usage.input_tokens),
            output_tokens=max(0, self.pending.output_tokens - usage.output_tokens),
        )

    def consume_unsettled_usage(self):
        pending, self.pending = self.pending, UsageSnapshot()
        return pending

    async def stream_message(self, request):
        self.pending = UsageSnapshot(input_tokens=23, output_tokens=100)
        yield ApiRetryEvent(message="empty", attempt=1, max_attempts=2, delay_seconds=0)
        raise AssertionError("Closed recovery must not send another request")


async def test_engine_keeps_billed_usage_when_closed_during_recovery(tmp_path):
    engine = _engine(UnsettledClient(), tmp_path)
    stream = engine.submit_message("test")
    await anext(stream)
    await stream.aclose()
    assert engine.total_usage.input_tokens == 23
    assert engine.total_usage.output_tokens == 100


async def test_partial_answer_is_saved_and_reported_incomplete_without_double_usage(tmp_path):
    from openharness.engine.stream_events import ErrorEvent

    class PartialClient(UnsettledClient):
        last_response_diagnostic = "Answer interrupted; incomplete"
        last_failure_metadata = {"category": "provider_sse", "raw_chunk_count": 1}

        async def stream_message(self, request):
            self.pending = UsageSnapshot(input_tokens=23, output_tokens=5)
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[TextBlock(text="Partial")]),
                usage=self.pending, stop_reason="transport_error",
            )

    client = PartialClient()
    engine = _engine(client, tmp_path)
    events = [event async for event in engine.submit_message("test")]
    assert engine.messages[-1].text == "Partial"
    assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
        client.last_response_diagnostic,
    ]
    assert engine.total_usage.output_tokens == 5
    assert engine.total_usage.input_tokens == 23
    assert client.consume_unsettled_usage().total_tokens == 0
    assert engine.tool_metadata["geo_request_failures"] == [client.last_failure_metadata]


class EchoInput(BaseModel):
    code: str = "print('valid output')"


class SuccessfulTool(BaseTool):
    name = "geo_python"
    description = "Synthetic tool for lifecycle checks."
    input_model = EchoInput
    calls = 0

    async def execute(self, arguments, context):
        self.calls += 1
        return ToolResult(output="successful tool output", is_error=False)

    def is_read_only(self, arguments):
        return True


class ToolThenEmptyClient:
    last_empty_response_diagnostic = "Synthetic empty response diagnostic"
    def __init__(self):
        self.calls = 0

    async def stream_message(self, request):
        self.calls += 1
        message = ConversationMessage(role="assistant", content=[])
        if self.calls == 1:
            message.content.append(ToolUseBlock(id="call-1", name="geo_python", input={}))
        yield ApiMessageCompleteEvent(
            message=message, usage=UsageSnapshot(output_tokens=16384), stop_reason="length",
        )


async def test_successful_tool_result_survives_empty_provider_followup(tmp_path):
    client = ToolThenEmptyClient()
    tool = SuccessfulTool()
    registry = ToolRegistry()
    registry.register(tool)
    engine = GeoQueryEngine(
        api_client=client, tool_registry=registry,
        permission_checker=PermissionChecker(settings=PermissionSettings()),
        cwd=tmp_path, model="test-model", system_prompt="Complete the task.", max_turns=4,
    )
    events = [event async for event in engine.submit_message("inspect files")]
    assert events and tool.calls == 1
    results = [block for message in engine.messages for block in message.content
               if isinstance(block, ToolResultBlock)]
    assert len(results) == 1
    assert results[0].tool_use_id == "call-1"
    assert results[0].content == "successful tool output"
    assert engine.has_pending_continuation()
    from openharness.engine.stream_events import ErrorEvent

    assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
        client.last_empty_response_diagnostic,
    ]
    continued = [event async for event in engine.continue_pending()]
    assert [event.message for event in continued if isinstance(event, ErrorEvent)] == [
        client.last_empty_response_diagnostic,
    ]
    assert tool.calls == 1 and engine.has_pending_continuation()

    class RecoveredClient:
        async def stream_message(self, request):
            results = [block for message in request.messages for block in message.content
                       if isinstance(block, ToolResultBlock)]
            assert len(results) == 1 and results[0].content == "successful tool output"
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[TextBlock(text="Done")]),
                usage=UsageSnapshot(output_tokens=2), stop_reason="stop",
            )

    engine.set_api_client(RecoveredClient())
    [event async for event in engine.continue_pending()]
    assert tool.calls == 1
    assert engine.messages[-1].text == "Done"
    assert not engine.has_pending_continuation()


async def test_plain_continue_resumes_tool_result_and_preserves_task_state(tmp_path):
    from copy import deepcopy

    client = ToolThenEmptyClient()
    tool = SuccessfulTool()
    engine = _engine(client, tmp_path)
    engine._tool_registry.register(tool)
    [event async for event in engine.submit_message("Make the emergency map")]
    engine.tool_metadata["map_workflow_state"].update(
        reviews=2, maps={"map": {"renders": 3, "reviews": 2}},
        cache={"review": {"status": "issues"}},
    )
    engine.tool_metadata["map_job_state"] = {"request_id": "same-request"}
    engine.tool_metadata["map_deterministic_errors"] = {"same-input": "failed preflight"}
    before = deepcopy(engine.tool_metadata)
    message_count = len(engine.messages)

    [event async for event in engine.submit_message("继续。")]

    assert tool.calls == 1
    assert engine.has_pending_continuation()
    assert len(engine.messages) == message_count
    for key in ("map_workflow_state", "map_job_state", "map_deterministic_errors"):
        assert engine.tool_metadata[key] == before[key]
    assert engine.tool_metadata["task_focus_state"]["goal"] == "Make the emergency map"


async def test_new_instruction_after_pending_is_a_new_task(tmp_path):
    client = ToolThenEmptyClient()
    engine = _engine(client, tmp_path)
    engine._tool_registry.register(SuccessfulTool())
    [event async for event in engine.submit_message("Make the emergency map")]
    engine.tool_metadata["map_workflow_state"]["reviews"] = 3

    [event async for event in engine.submit_message("继续分析另一份数据")]

    assert engine.messages[-1].text == "继续分析另一份数据"
    assert engine.tool_metadata["map_workflow_state"]["reviews"] == 0
    assert engine.tool_metadata["task_focus_state"]["goal"] == "继续分析另一份数据"


async def test_plain_continue_after_max_turns_uses_saved_results(tmp_path):
    import pytest
    from openharness.engine.query import MaxTurnsExceeded

    client = ToolThenEmptyClient()
    tool = SuccessfulTool()
    engine = _engine(client, tmp_path)
    engine._max_turns = 1
    engine._tool_registry.register(tool)
    with pytest.raises(MaxTurnsExceeded):
        [event async for event in engine.submit_message("Make the emergency map")]
    assert engine.has_pending_continuation() and tool.calls == 1

    [event async for event in engine.submit_message("continue")]

    assert engine.has_pending_continuation() and tool.calls == 1
    assert any(isinstance(block, ToolResultBlock) for block in engine.messages[-1].content)
    assert engine.tool_metadata["task_focus_state"]["goal"] == "Make the emergency map"


async def test_engine_recovery_scope_survives_resume_and_cross_task_close(tmp_path):
    import asyncio
    from contextlib import contextmanager
    from contextvars import ContextVar

    class ScopedClient(ToolThenEmptyClient):
        def __init__(self):
            super().__init__()
            self.bound = ContextVar("synthetic-query", default=None)
            self.states = []

        @contextmanager
        def query_recovery_scope(self, state):
            token = self.bound.set(state)
            try:
                yield
            finally:
                self.bound.reset(token)

        async def stream_message(self, request):
            self.states.append(self.bound.get())
            async for event in super().stream_message(request):
                yield event

    client = ScopedClient()
    engine = _engine(client, tmp_path)
    engine._tool_registry.register(SuccessfulTool())
    [event async for event in engine.submit_message("Make the emergency map")]
    original_state = client.states[0]
    assert original_state is not None and client.states[1] is original_state
    assert client.bound.get() is None
    [event async for event in engine.submit_message("继续")]
    assert client.states[-1] is original_state
    assert client.bound.get() is None

    stream = engine.submit_message("A different map")
    await asyncio.create_task(anext(stream))
    await asyncio.create_task(stream.aclose())
    assert client.bound.get() is None
    assert engine._query_recovery_state is not original_state


async def test_engine_displays_structured_provider_failure_instead_of_network_label(tmp_path):
    from openharness.api.errors import RequestFailure
    from openharness.engine.stream_events import ErrorEvent

    class ProviderErrorClient:
        last_failure_diagnostic = "模型服务返回流错误（provider_sse）；工具结果已保留"
        last_failure_metadata = {
            "request_id": "59f16777-6c3e-4e26-a3a6-075dcb3cc6c8",
            "category": "provider_sse", "raw_chunk_count": 1,
        }

        async def stream_message(self, request):
            raise RequestFailure("stream stall timeout")
            yield  # Keep the streaming protocol without returning a synthetic final message.

    client = ProviderErrorClient()
    engine = _engine(client, tmp_path)
    events = [event async for event in engine.submit_message("test")]
    assert [event.message for event in events if isinstance(event, ErrorEvent)] == [
        client.last_failure_diagnostic,
    ]
    assert engine.tool_metadata["geo_request_failures"] == [client.last_failure_metadata]
    engine.tool_metadata["geo_request_failures"][0]["raw_chunk_count"] = 2
    assert client.last_failure_metadata["raw_chunk_count"] == 1


async def test_empty_followup_records_failed_recovery_and_keeps_completed_tool(tmp_path):
    class FailedRecoveryClient(ToolThenEmptyClient):
        last_failure_metadata = None

        async def stream_message(self, request):
            async for event in super().stream_message(request):
                if self.calls > 1:
                    self.last_failure_metadata = {"category": "http", "status_code": 503}
                yield event

    client = FailedRecoveryClient()
    engine = _engine(client, tmp_path)
    tool = SuccessfulTool()
    engine._tool_registry.register(tool)
    [event async for event in engine.submit_message("Make the map")]
    assert engine.tool_metadata["geo_request_failures"] == [{
        "category": "http", "status_code": 503,
    }]
    assert tool.calls == 1 and engine.has_pending_continuation()


async def test_geo_backend_error_is_shown_once_and_later_status_is_preserved(capsys):
    import json

    from openharness.ui.backend_host import BackendHostConfig
    from openharness.ui.protocol import BackendEvent, TranscriptItem

    from geoharness.tui_backend import _geo_backend_host_class

    host = _geo_backend_host_class()(BackendHostConfig())
    error = BackendEvent(type="error", message="provider empty")
    duplicate = BackendEvent(
        type="transcript_item", item=TranscriptItem(role="system", text="provider empty"),
    )
    await host._emit(error)
    await host._emit(duplicate)
    # A later independently emitted status with identical text is legitimate.
    await host._emit(duplicate)
    rows = [json.loads(line.removeprefix("OHJSON:"))
            for line in capsys.readouterr().out.splitlines()]
    assert [row["type"] for row in rows] == ["error", "transcript_item"]


async def test_geo_backend_restores_complete_tool_turn_without_overwriting_config(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from geoharness.tui_backend import geo_backend_runtime_factory

    registry = ToolRegistry()
    engine = GeoQueryEngine(
        api_client=ToolThenEmptyClient(), tool_registry=registry,
        permission_checker=PermissionChecker(settings=PermissionSettings()),
        cwd=tmp_path, model="test", system_prompt="test",
        tool_metadata={"geoharness_config": "live-config"},
    )
    saved = [
        ConversationMessage(role="assistant", content=[
            ToolUseBlock(id="saved-call", name="geo_python", input={"code": "print(1)"}),
        ]),
        ConversationMessage(role="user", content=[
            ToolResultBlock(tool_use_id="saved-call", content="1"),
        ]),
    ]
    bundle = SimpleNamespace(engine=engine)
    with (
        patch("geoharness.runtime.build_geo_runtime", AsyncMock(return_value=bundle)),
        patch("geoharness.mcp.connect.activate_mcp", AsyncMock(return_value={})),
    ):
        await geo_backend_runtime_factory(
            cwd=tmp_path, restore_messages=[message.model_dump() for message in saved],
            restore_tool_metadata={
                "task_focus_state": {"goal": "resume map"},
                "geoharness_config": "stale-config",
            },
        )
    assert engine.has_pending_continuation()
    assert engine.messages[-1].content[0].content == "1"
    assert engine.tool_metadata["geoharness_config"] == "live-config"
    assert engine.tool_metadata["task_focus_state"]["goal"] == "resume map"


async def test_fresh_init_does_not_start_uninstalled_mcp_servers(tmp_path, monkeypatch):
    from pathlib import Path

    from typer.testing import CliRunner

    from geoharness.cli import app
    from geoharness.config.settings import load_geo_config
    from geoharness.mcp.connect import activate_mcp
    from geoharness.runtime import build_geo_runtime

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = CliRunner().invoke(app, ["init"])
    assert result.exit_code == 0, result.output
    config = load_geo_config(tmp_path / ".geoharness" / "config.yaml")
    assert config.mcp_servers == {}
    bundle = await build_geo_runtime(cwd=tmp_path, config_override=config)
    assert await activate_mcp(bundle) == {}
    assert bundle.tool_registry.get("geo_analysis") is not None
    assert bundle.tool_registry.get("geo_db_data") is not None


async def test_backend_restore_preserves_map_budget_aliases_and_pending_results(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from geoharness.config.settings import GeoConfig
    from geoharness.session_state import GEO_SESSION_CHECKPOINT_KEY, _checkpoint
    from geoharness.tui_backend import geo_backend_runtime_factory

    config = GeoConfig()
    client = ToolThenEmptyClient()
    client.calls = 1  # Resuming starts after the saved tool result.
    engine = _engine(client, tmp_path)
    engine.tool_metadata["geoharness_config"] = config
    shared_budget = {"renders": 3, "reviews": 2}
    checkpoint = _checkpoint({
        "map_workflow_state": {
            "reviews": 2, "seconds": 18.0,
            "maps": {"persistent-map": shared_budget, "old-filename": shared_budget},
        },
    }, tmp_path.resolve(), config)
    saved = [
        ConversationMessage(role="assistant", content=[
            ToolUseBlock(id="saved-call", name="geo_python", input={"code": "print(1)"}),
        ]),
        ConversationMessage(role="user", content=[
            ToolResultBlock(tool_use_id="saved-call", content="1"),
        ]),
    ]
    with (
        patch("geoharness.runtime.build_geo_runtime", AsyncMock(
            return_value=SimpleNamespace(engine=engine),
        )),
        patch("geoharness.mcp.connect.activate_mcp", AsyncMock(return_value={})),
    ):
        await geo_backend_runtime_factory(
            cwd=tmp_path, restore_messages=[message.model_dump() for message in saved],
            restore_tool_metadata={
                "task_focus_state": {"goal": "Make the emergency map"},
                GEO_SESSION_CHECKPOINT_KEY: checkpoint,
            },
        )
    assert engine.has_pending_continuation()
    [event async for event in engine.submit_message("继续")]
    workflow = engine.tool_metadata["map_workflow_state"]
    assert workflow["maps"]["persistent-map"] is workflow["maps"]["old-filename"]
    assert workflow["maps"]["persistent-map"]["renders"] == 3
    assert workflow["reviews"] == 2 and workflow["seconds"] == 18.0
    assert engine.tool_metadata["task_focus_state"]["goal"] == "Make the emergency map"
