"""Small offline checks for automatic continuation of exhausted tool batches."""

import json

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.config.settings import PermissionSettings
from openharness.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.engine.stream_events import ErrorEvent, StatusEvent, ToolExecutionCompleted
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import BaseTool, ToolRegistry, ToolResult
from pydantic import BaseModel

from geoharness.config.settings import GeoConfig
from geoharness.engine import GeoQueryEngine


def test_offloaded_repeat_ignores_call_identity_without_following_artifact_paths():
    def event(call):
        return ToolExecutionCompleted(tool_name="probe", output=(
            "[Tool output truncated]\nTool: probe\n"
            f"Tool use id: call-{call}\nOriginal size: 90000 chars\n"
            f"Full output saved to: Z:/untrusted/{call}.txt\n"
            "Inline preview: first 4 chars (89996 chars omitted)\n\nPreview:\nsame"
        ))

    assert GeoQueryEngine._progress_fingerprint(event(1)) == (
        GeoQueryEngine._progress_fingerprint(event(2)))


def test_execution_receipts_do_not_turn_an_unchanged_result_into_progress():
    def event(identity, count=13):
        return ToolExecutionCompleted(tool_name='geo_python', output=json.dumps({
            'result': {'console_result': {'count': count}},
            'operation_id': identity, 'execution_id': identity,
            'target_operation_id': identity,
            'execution_evidence_call': {'action': 'evidence', 'operation_id': identity},
        }))

    fingerprint = GeoQueryEngine._progress_fingerprint
    assert fingerprint(event('a')) == fingerprint(event('b'))
    assert fingerprint(event('a')) != fingerprint(event('b', count=14))


class Input(BaseModel):
    index: int = 0


def test_waiting_review_counters_are_not_new_progress():
    def event(hits):
        return ToolExecutionCompleted(tool_name="geo_map_review", output=json.dumps({
            "status": "unverified", "diagnostics": {
                "stage": "cache", "http_status": 503, "error_type": "InternalServerError",
                "failure_stage": "provider", "next_action": "wait_then_retry_same_image",
                "image_cache_hits": hits, "service_retry_after_seconds": 60 - hits,
            }}) + f"\nReport: workspace/preview.review-{hits}/report.json")
    assert GeoQueryEngine._progress_fingerprint(event(1)) == (
        GeoQueryEngine._progress_fingerprint(event(2)))


class ProbeTool(BaseTool):
    name = "probe"
    description = "Offline continuation probe."
    input_model = Input

    def __init__(self, *, error=False, volatile=False, changing=False, failure_indices=()):
        self.calls = 0
        self.error = error
        self.volatile = volatile
        self.changing = changing
        self.failure_indices = failure_indices

    async def execute(self, arguments, context):
        self.calls += 1
        output = str(arguments.index)
        if self.changing:
            output = f"updated source content: {self.calls}"
        if self.volatile:
            output = json.dumps({
                "status": "succeeded", "stdout": "same result",
                "run_dir": f"run-{self.calls}", "elapsed_seconds": self.calls,
            })
        failed = self.error or arguments.index in self.failure_indices
        return ToolResult(output="same tool failure" if failed else output, is_error=failed)

    def is_read_only(self, arguments):
        return True


class ProbeClient:
    def __init__(self, *, tool_turns=40, repeat=False):
        self.calls = 0
        self.tool_turns = tool_turns
        self.repeat = repeat
        self.requests = []

    async def stream_message(self, request):
        self.calls += 1
        self.requests.append(request)
        content = [TextBlock(text="done")]
        if self.calls <= self.tool_turns:
            content = [ToolUseBlock(
                id=f"call-{self.calls}", name="probe",
                input={"index": 0 if self.repeat else self.calls},
            )]
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role="assistant", content=content),
            usage=UsageSnapshot(input_tokens=3, output_tokens=2),
            stop_reason="tool_calls" if self.calls <= self.tool_turns else "stop",
        )


async def test_review_limit_pauses_before_another_main_model_request(tmp_path):
    class ReviewTool(ProbeTool):
        name = 'geo_map_review'

        async def execute(self, arguments, context):
            self.calls += 1
            return ToolResult(output=json.dumps({'status': 'issues',
                'automatic_review_stopped': True, 'summary': '严重缺陷未解决'}))

    class ReviewClient(ProbeClient):
        async def stream_message(self, request):
            self.calls += 1
            assert self.calls == 1, '上限后不应再请求主模型继续修图'
            yield ApiMessageCompleteEvent(message=ConversationMessage(role='assistant', content=[
                ToolUseBlock(id='review-cap', name='geo_map_review', input={})]),
                usage=UsageSnapshot(), stop_reason='tool_calls')

    client, tool = ReviewClient(), ReviewTool()
    runtime = engine(tmp_path, client, tool, turns=1)
    events = [event async for event in runtime.submit_message('检查地图')]
    assert client.calls == tool.calls == 1
    assert any(isinstance(e, StatusEvent) and '已达到自动审图上限' in e.message for e in events)


def engine(tmp_path, client, tool, *, turns=1):
    registry = ToolRegistry()
    registry.register(tool)
    return GeoQueryEngine(
        api_client=client, tool_registry=registry,
        permission_checker=PermissionChecker(settings=PermissionSettings()),
        cwd=tmp_path, model="offline", system_prompt="Complete original task.",
        max_turns=turns, tool_metadata={"geoharness_config": GeoConfig()},
    )


async def test_default_forty_turn_batch_finishes_without_replaying_tools(tmp_path):
    client, tool = ProbeClient(), ProbeTool()
    runtime = engine(tmp_path, client, tool, turns=40)
    events = [event async for event in runtime.submit_message("original task")]
    assert tool.calls == 40 and client.calls == 41
    assert runtime.messages[-1].text == "done"
    assert not runtime.has_pending_continuation()
    assert runtime.tool_metadata["task_focus_state"]["goal"] == "original task"
    assert [m.text for m in runtime.messages if m.role == "user" and m.text] == ["original task"]
    results = [b for m in runtime.messages for b in m.content if isinstance(b, ToolResultBlock)]
    assert len(results) == 40 and len({b.tool_use_id for b in results}) == 40
    assert runtime.total_usage.input_tokens == 123
    assert runtime.total_usage.output_tokens == 82
    assert any(isinstance(event, StatusEvent) and "自动继续" in event.message for event in events)


async def test_repeated_successful_output_stops_automatic_loop(tmp_path):
    client, tool = ProbeClient(tool_turns=100, repeat=True), ProbeTool()
    runtime = engine(tmp_path, client, tool)
    events = [event async for event in runtime.submit_message("original task")]
    assert client.calls == tool.calls == 2
    assert runtime.has_pending_continuation()
    assert any(isinstance(event, ErrorEvent) and "自动续跑已停止" in event.message
               for event in events)


async def test_new_run_directory_and_duration_are_not_progress(tmp_path):
    client, tool = ProbeClient(tool_turns=100), ProbeTool(volatile=True)
    runtime = engine(tmp_path, client, tool)
    [event async for event in runtime.submit_message("original task")]
    assert client.calls == tool.calls == 2
    assert runtime.has_pending_continuation()


async def test_changed_result_at_same_input_is_progress(tmp_path):
    client, tool = ProbeClient(tool_turns=2, repeat=True), ProbeTool(changing=True)
    runtime = engine(tmp_path, client, tool)
    [event async for event in runtime.submit_message("original task")]
    assert client.calls == 3 and tool.calls == 2
    assert runtime.messages[-1].text == "done"


async def test_elapsed_performance_target_does_not_stop_useful_continuation(tmp_path, monkeypatch):
    from geoharness import task_budget
    from geoharness.config.settings import AgentConfig

    def already_elapsed(metadata, config):
        metadata["geo_task_timing"] = {"started_at": 1}

    monkeypatch.setattr(task_budget, "start_task", already_elapsed)
    client, tool = ProbeClient(tool_turns=2), ProbeTool()
    runtime = engine(tmp_path, client, tool)
    runtime.tool_metadata["geoharness_config"] = GeoConfig(
        agent=AgentConfig(task_budget_seconds=1200))
    events = [event async for event in runtime.submit_message("finish all checks")]
    assert tool.calls == 2 and client.calls == 3
    assert runtime.messages[-1].text == "done"
    assert not any(isinstance(event, ErrorEvent) for event in events)


async def test_failed_last_tool_batch_does_not_retry_automatically(tmp_path):
    client, tool = ProbeClient(tool_turns=100), ProbeTool(error=True)
    runtime = engine(tmp_path, client, tool)
    events = [event async for event in runtime.submit_message("original task")]
    assert client.calls == tool.calls == 1
    assert runtime.has_pending_continuation()
    assert any(isinstance(event, ErrorEvent) and "没有新的成功工具结果" in event.message
               for event in events)


async def test_validation_rejections_are_counted_without_invented_execution_time(tmp_path):
    class InvalidClient(ProbeClient):
        async def stream_message(self, request):
            async for event in super().stream_message(request):
                if event.message.tool_uses:
                    event.message.tool_uses[0].input['index'] = 'not-an-integer'
                yield event

    runtime = engine(tmp_path, InvalidClient(tool_turns=1), ProbeTool(), turns=2)
    _ = [event async for event in runtime.submit_message('invalid parameter')]
    assert runtime.tool_metadata['geo_tool_call_counts']['probe'] == {'calls': 1, 'errors': 1}
    assert runtime.tool_metadata['geo_tool_metrics'] == []


def test_restored_request_metrics_merge_only_when_new_completion_arrives(tmp_path):
    client = ProbeClient()
    client.request_metrics = [{'request_id': 'new', 'attempt': 1, 'elapsed_seconds': 2}]
    runtime = engine(tmp_path, client, ProbeTool())
    runtime.tool_metadata['geo_request_metrics'] = [
        {'request_id': 'restored', 'attempt': 1, 'elapsed_seconds': 1}]
    runtime._persist_query_mode()
    saved = runtime.tool_metadata['geo_request_metrics']
    assert [m['request_id'] for m in saved] == ['restored', 'new']
    runtime._persist_query_mode()
    assert runtime.tool_metadata['geo_request_metrics'] is saved


async def test_fresh_error_at_boundary_gets_a_followup_chance(tmp_path):
    client, tool = ProbeClient(tool_turns=2), ProbeTool(failure_indices=(2,))
    runtime = engine(tmp_path, client, tool, turns=2)
    [event async for event in runtime.submit_message("original task")]
    assert client.calls == 3 and tool.calls == 2
    assert runtime.messages[-1].text == "done"


async def test_repeated_error_at_boundary_stops_despite_other_successes(tmp_path):
    client, tool = ProbeClient(tool_turns=100), ProbeTool(failure_indices=(2, 4))
    runtime = engine(tmp_path, client, tool, turns=2)
    events = [event async for event in runtime.submit_message("original task")]
    assert client.calls == tool.calls == 4
    assert runtime.has_pending_continuation()
    assert any(isinstance(event, ErrorEvent) and "末组工具重复失败" in event.message
               for event in events)


async def test_manual_recovery_then_auto_continuation_saves_final_answer(tmp_path):
    client, tool = ProbeClient(tool_turns=2), ProbeTool(error=True)
    runtime = engine(tmp_path, client, tool)
    [event async for event in runtime.submit_message("original task")]
    assert runtime.has_pending_continuation()
    tool.error = False
    [event async for event in runtime.continue_pending()]
    assert client.calls == 3 and tool.calls == 2
    assert runtime.messages[-1].text == "done"
    assert not runtime.has_pending_continuation()


async def test_closing_at_batch_boundary_does_not_start_another_request(tmp_path):
    client, tool = ProbeClient(tool_turns=100), ProbeTool()
    runtime = engine(tmp_path, client, tool)
    stream = runtime.submit_message("original task")
    async for event in stream:
        if isinstance(event, StatusEvent) and "自动继续" in event.message:
            break
    await stream.aclose()
    assert client.calls == tool.calls == 1
    assert runtime.has_pending_continuation()
