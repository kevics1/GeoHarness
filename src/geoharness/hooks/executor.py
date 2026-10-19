"""Local compatibility for advisory hooks consuming the provider event stream."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import replace

from openharness.api.client import (
    ApiMessageRequest,
    ApiRetryEvent,
    ApiStreamEvent,
    SupportsStreamingMessages,
)
from openharness.hooks.events import HookEvent
from openharness.hooks.executor import HookExecutionContext, HookExecutor
from openharness.hooks.loader import HookRegistry
from openharness.hooks.types import AggregatedHookResult, HookResult


class _PromptHookApiClient:
    """Leave recovery inside the API call without treating its status as JSON text."""

    def __init__(self, client: SupportsStreamingMessages) -> None:
        self._client = client

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        async with aclosing(self._client.stream_message(request)) as stream:
            async for event in stream:
                if not isinstance(event, ApiRetryEvent):
                    yield event


class GeoHookExecutor(HookExecutor):
    def __init__(self, registry: HookRegistry, context: HookExecutionContext) -> None:
        super().__init__(registry, replace(
            context, api_client=_PromptHookApiClient(context.api_client),
        ))

    def update_context(self, *, api_client=None, default_model=None) -> None:
        super().update_context(
            api_client=_PromptHookApiClient(api_client) if api_client is not None else None,
            default_model=default_model,
        )

    async def execute(self, event, payload):
        if event == HookEvent.PRE_TOOL_USE:
            arguments = payload.get("tool_input")
            if isinstance(arguments, dict) and "_geoharness_argument_error" in arguments:
                diagnostic = arguments["_geoharness_argument_error"]
                diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
                category = diagnostic.get("category")
                reason = diagnostic.get("finish_reason")
                safe = {
                    "category": category if isinstance(category, str) and category in {
                        "invalid_json", "non_object_json", "missing_required_arguments",
                    } else "invalid_arguments",
                    "finish_reason": reason if isinstance(reason, str) and reason in {
                        "length", "stop", "tool_calls", "function_call", "content_filter",
                    } else "unknown",
                    **{key: diagnostic[key] for key in (
                        "argument_chars", "json_error_position", "json_error_line",
                        "json_error_column",
                    ) if isinstance(diagnostic.get(key), int)},
                }
                parse_message = diagnostic.get("json_error_message")
                if isinstance(parse_message, str) and parse_message in {
                    "Expecting ',' delimiter", "Expecting ':' delimiter", "Expecting value",
                    "Expecting property name enclosed in double quotes", "Extra data",
                    "Unterminated string starting at", "Invalid \\escape",
                }:
                    safe["json_error_message"] = parse_message
                return AggregatedHookResult(results=[HookResult(
                    hook_type="geoharness_argument_guard", success=False, blocked=True,
                    reason=(
                        "模型返回的工具参数无效；本次工具没有执行。"
                        + json.dumps(safe, ensure_ascii=False)
                        + "。仅重发失败的完整有效调用，保留此前成功结果，不猜测坐标或字段。"
                        "普通制图使用geo_cartography render(scene_ref, edits)；"
                        "审查通过后由geo_deliver一次生成QGIS工程。"
                    ),
                    metadata={"argument_diagnostic": safe},
                )])
            if isinstance(arguments, dict):
                from geoharness.tools.contracts import input_feedback

                feedback = input_feedback(payload.get("tool_name", ""), arguments)
                if feedback:
                    return AggregatedHookResult(results=[HookResult(
                        hook_type="geoharness_contract_guard", success=False, blocked=True,
                        reason=json.dumps(feedback, ensure_ascii=False),
                        metadata={"input_feedback": feedback})])
        return await super().execute(event, payload)

    async def _run_prompt_like_hook(self, hook, event, payload, *, agent_mode):
        try:
            return await super()._run_prompt_like_hook(hook, event, payload, agent_mode=agent_mode)
        except Exception as exc:
            # Advisory hooks must not terminate the user's main request on transport failure.
            # Cancellation remains a BaseException and propagates to the backend's stop handler.
            return HookResult(
                hook_type=hook.type, success=False, blocked=hook.block_on_failure,
                reason=f"Prompt hook failed ({type(exc).__name__})",
                metadata={"error_type": type(exc).__name__},
            )
