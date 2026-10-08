"""Local compatibility for advisory hooks consuming the provider event stream."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import replace

from openharness.api.client import (
    ApiMessageRequest,
    ApiRetryEvent,
    ApiStreamEvent,
    SupportsStreamingMessages,
)
from openharness.hooks.executor import HookExecutionContext, HookExecutor
from openharness.hooks.loader import HookRegistry
from openharness.hooks.types import HookResult


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
