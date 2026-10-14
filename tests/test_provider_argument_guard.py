"""Malformed provider calls must not execute tools with optional-only inputs."""

from pathlib import Path

from openharness.engine.query import QueryContext, _execute_tool_call
from openharness.hooks.executor import HookExecutionContext
from openharness.hooks.loader import HookRegistry
from openharness.tools.base import BaseTool, ToolRegistry, ToolResult
from pydantic import BaseModel

from geoharness.config.settings import GeoConfig
from geoharness.hooks.executor import GeoHookExecutor
from geoharness.runtime import _build_permission_checker


class OptionalInput(BaseModel):
    value: str = "default"


class OptionalTool(BaseTool):
    name = "optional_tool"
    description = "Count real executions."
    input_model = OptionalInput

    def __init__(self):
        self.executions = 0

    async def execute(self, arguments, context):
        self.executions += 1
        return ToolResult(output=arguments.value)

    def is_read_only(self, arguments):
        return True


async def test_real_tool_boundary_rejects_marker_without_disrupting_valid_calls(tmp_path: Path):
    tool = OptionalTool()
    registry = ToolRegistry()
    registry.register(tool)
    executor = GeoHookExecutor(
        HookRegistry(), HookExecutionContext(cwd=tmp_path, api_client=None, default_model="test")
    )
    context = QueryContext(
        api_client=None, tool_registry=registry,
        permission_checker=_build_permission_checker(GeoConfig()), cwd=tmp_path,
        model="test", system_prompt="", max_tokens=1024, hook_executor=executor,
    )
    result = await _execute_tool_call(context, tool.name, "bad", {
        "_geoharness_argument_error": {
            "category": "invalid_json", "argument_chars": 12,
            "json_error_position": 11, "finish_reason": "length",
            "json_error_message": "Expecting ',' delimiter",
            "unexpected_secret": "must-not-be-echoed",
        },
    })
    assert result.is_error
    assert "no tool was executed" in result.content
    assert '"json_error_position": 11' in result.content
    assert "Expecting ',' delimiter" in result.content
    assert "action+returned plan_ref" in result.content
    assert "must-not-be-echoed" not in result.content
    assert tool.executions == 0
    valid = await _execute_tool_call(context, tool.name, "good", {"value": "complete"})
    assert not valid.is_error and valid.content == "complete"
    assert tool.executions == 1
