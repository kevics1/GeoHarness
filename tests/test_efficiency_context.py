import asyncio
import json
from pathlib import Path

import pytest
from openharness.api.client import ApiMessageRequest
from openharness.engine.messages import (
    ConversationMessage,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.tools.base import ToolExecutionContext, ToolResult
from pydantic import BaseModel

from geoharness.config.settings import AgentConfig
from geoharness.model_context import project_history
from geoharness.performance import MeasuredRegistry
from geoharness.telemetry import safe_metrics, timing_summary


def history():
    messages = [ConversationMessage.from_user_text("必须保留全部居民点")]
    for i in range(9):
        messages.append(ConversationMessage(role="assistant", content=[
            ToolUseBlock(id=str(i), name="geo_python", input={"code": "print('original')"})]))
        messages.append(ConversationMessage(role="user", content=[ToolResultBlock(
            tool_use_id=str(i), content=json.dumps({"status": "failed" if i == 0 else "ok",
                "error_summary": "actual failure" if i == 0 else "",
                "measured": ["真实的旧字段" * 1000], "manifest_path": f"run-{i}/manifest.json"}),
            is_error=i == 0)]))
    return ApiMessageRequest(model="same-model", messages=messages)


def test_history_projection_is_recoverable_keeps_pairs_and_recent_context(tmp_path):
    request = history()
    before = [m.model_dump() for m in request.messages]
    projected, metrics = project_history(request, cwd=tmp_path, settings=AgentConfig())
    assert metrics["archived_results"] == 3 and metrics["saved_chars"] > 10000
    assert before == [m.model_dump() for m in request.messages]
    assert projected.messages[0] == request.messages[0]
    assert projected.messages[-12:] == request.messages[-12:]
    for old, new in zip(request.messages, projected.messages):
        assert old.role == new.role
        assert old.tool_uses == new.tool_uses
    result = projected.messages[2].content[0]
    assert result.tool_use_id == "0" and result.is_error
    compact = json.loads(result.content)
    assert compact["error_summary"] == "actual failure"
    assert Path(compact["full_result_path"]).read_text(encoding="utf-8") == (
        request.messages[2].content[0].content)
    again, second = project_history(request, cwd=tmp_path, settings=AgentConfig())
    assert again == projected and second == metrics


def test_projection_failure_and_non_json_keep_full_context(tmp_path, monkeypatch):
    from geoharness import model_context

    def broken(*args, **kwargs):
        raise OSError("no disk")

    monkeypatch.setattr(model_context, "atomic_write_text", broken)
    request = history()
    projected, metrics = project_history(request, cwd=tmp_path, settings=AgentConfig())
    assert projected == request and metrics["archived_results"] == 0
    request.messages[2].content[0].content = "not JSON " * 2000
    assert project_history(request, cwd=tmp_path, settings=AgentConfig())[0] == request


def test_old_successful_code_projection_is_explicit_recoverable_and_never_saves_secrets(tmp_path):
    request = history()
    original_code = "print('measured')\n" * 500
    request.messages[3].content[0].input['code'] = original_code
    projected, stats = project_history(request, cwd=tmp_path, settings=AgentConfig())
    block = projected.messages[3].content[0]
    assert stats['archived_inputs'] == 1 and block.id == '1'
    assert request.messages[3].content[0].input['code'] == original_code
    archive = next(tmp_path.rglob('*.py'))
    assert archive.read_text(encoding='utf-8') == original_code
    assert str(archive) in block.input['code'] and '不能' in block.input['code']
    guarded = tmp_path / 'guarded'
    no_archive, stats = project_history(request, cwd=guarded, settings=AgentConfig(),
                                        secrets=('measured',))
    assert stats['archived_inputs'] == 0
    assert no_archive.messages[3].content[0] == request.messages[3].content[0]


@pytest.mark.asyncio
async def test_tool_metrics_measure_parallel_execution_independently_and_keep_result():
    class Input(BaseModel):
        action: str = "probe"

    class Tool:
        name = "geo_probe"
        input_model = Input

        async def execute(self, arguments, context):
            await asyncio.sleep(0.01)
            return ToolResult(output='{"status":"ok","spec_ref":"qgis-spec:real"}')

    class Registry:
        def get(self, name):
            return Tool()

    metadata = {}
    registry = MeasuredRegistry(Registry(), metadata)
    context = ToolExecutionContext(cwd=".", metadata=metadata)
    results = await asyncio.gather(*(registry.get("geo_probe").execute(Input(), context)
                                     for _ in range(3)))
    metrics = metadata["geo_tool_metrics"]
    assert len(metrics) == 3 and len({m["execution_id"] for m in metrics}) == 3
    assert all(m["elapsed_seconds"] > 0 for m in metrics)
    assert all(not result.is_error for result in results)
    assert metadata["geo_current_state"]["geo_probe"]["spec_ref"] == "qgis-spec:real"


def test_persisted_metrics_exclude_payloads_credentials_and_nonfinite_values():
    result = safe_metrics([{"request_id": "one", "elapsed_seconds": 1.1,
        "input_tokens": 12, "is_error": False, "raw_prompt": "private",
        "model": "secret-key", "reasoning_tokens": float("inf"), "code": "private"}],
        limit=10, secrets=("secret-key",))
    assert result == [{"request_id": "one", "elapsed_seconds": 1.1,
                       "input_tokens": 12, "is_error": False}]


def test_timing_summary_uses_interval_union_not_parallel_sum():
    summary = timing_summary({'geo_task_timing': {'started_at': 90},
        'geo_tool_metrics': [{'started_at': 100, 'elapsed_seconds': 10},
                             {'started_at': 105, 'elapsed_seconds': 10}],
        'geo_request_metrics': [{'started_at': 115, 'elapsed_seconds': 5}]}, now=130)
    assert summary['wall_elapsed_seconds'] == 40
    assert summary['measured_busy_seconds'] == 20
    assert summary['unattributed_seconds'] == 20
