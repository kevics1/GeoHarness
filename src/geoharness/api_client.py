"""Provider extensions and bounded recovery around the upstream OpenAI parser."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from openharness.api.client import (
    ApiMessageCompleteEvent,
    ApiMessageRequest,
    ApiRetryEvent,
    ApiStreamEvent,
    ApiTextDeltaEvent,
)
from openharness.api.openai_client import OpenAICompatibleClient
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, TextBlock

from geoharness.config.settings import AgentConfig


@dataclass
class _RequestState:
    extra_body: dict[str, Any] = field(default_factory=dict)
    reasoning_chars: int = 0
    raw_content_chars: int = 0
    visible_chars: int = 0
    visible_text: list[str] = field(default_factory=list)
    reported_usage: UsageSnapshot = field(default_factory=UsageSnapshot)
    include_stream_usage: bool = True
    record_usage: Callable[[UsageSnapshot], None] | None = None
    active_streams: list[Any] = field(default_factory=list)


def _add_usage(left: UsageSnapshot, right: UsageSnapshot) -> UsageSnapshot:
    return UsageSnapshot(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
    )


async def _close_provider_stream(stream: Any, state: _RequestState | None) -> None:
    if state is not None:
        if stream not in state.active_streams:
            return
        state.active_streams.remove(stream)
    close = getattr(stream, "close", None) or getattr(stream, "aclose", None)
    if close is not None:
        await close()


class _CompletionProxy:
    """Observe metadata and inject extensions without copying the upstream parser."""

    def __init__(self, completions: Any, state: ContextVar[_RequestState | None]) -> None:
        self._completions = completions
        self._state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._completions, name)

    async def create(self, **params: Any) -> Any:
        state = self._state.get()
        if state is not None and state.extra_body:
            params["extra_body"] = dict(state.extra_body)
        if state is not None:
            if state.include_stream_usage:
                params["stream_options"] = {
                    **params.get("stream_options", {}), "include_usage": True,
                }
            else:
                params.pop("stream_options", None)
        stream = await self._completions.create(**params)
        if state is not None:
            state.active_streams.append(stream)

        async def observed_stream() -> AsyncIterator[Any]:
            previous_usage = UsageSnapshot()
            try:
                async for chunk in stream:
                    if state is not None and chunk.usage:
                        # Providers may repeat cumulative usage in multiple chunks.
                        usage = UsageSnapshot(
                            input_tokens=max(chunk.usage.prompt_tokens or 0, 0),
                            output_tokens=max(chunk.usage.completion_tokens or 0, 0),
                        )
                        delta_usage = UsageSnapshot(
                            input_tokens=max(usage.input_tokens - previous_usage.input_tokens, 0),
                            output_tokens=max(
                                usage.output_tokens - previous_usage.output_tokens, 0,
                            ),
                        )
                        state.reported_usage = _add_usage(state.reported_usage, delta_usage)
                        if state.record_usage is not None:
                            state.record_usage(delta_usage)
                        previous_usage = UsageSnapshot(
                            input_tokens=max(usage.input_tokens, previous_usage.input_tokens),
                            output_tokens=max(usage.output_tokens, previous_usage.output_tokens),
                        )
                    if state is not None and chunk.choices:
                        delta = chunk.choices[0].delta
                        reasoning = getattr(delta, "reasoning_content", None) or ""
                        state.reasoning_chars += len(reasoning)
                        state.raw_content_chars += len(delta.content or "")
                    yield chunk
            finally:
                await _close_provider_stream(stream, state)

        return observed_stream()


class _ChatProxy:
    def __init__(self, chat: Any, state: ContextVar[_RequestState | None]) -> None:
        self._chat = chat
        self.completions = _CompletionProxy(chat.completions, state)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)


class _ClientProxy:
    def __init__(self, client: Any, state: ContextVar[_RequestState | None]) -> None:
        self._client = client
        self.chat = _ChatProxy(client.chat, state)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class GeoOpenAICompatibleClient(OpenAICompatibleClient):
    """Retry only an empty response with a configured alternative request.

    Recovery stays inside this API call: tools have not executed, and the original
    messages and tool schemas are replayed unchanged. Completed failed requests
    are included in the one terminal event's usage. The engine uses the diagnostic
    property to replace its generic empty-message error without hiding the failure.
    """

    def __init__(
        self, api_key: str, *, base_url: str | None = None,
        timeout: float | None = None, settings: AgentConfig | None = None,
    ) -> None:
        super().__init__(api_key, base_url=base_url, timeout=timeout)
        self.settings = settings or AgentConfig()
        self._request_state: ContextVar[_RequestState | None] = ContextVar(
            "geoharness_api_request", default=None,
        )
        self._empty_diagnostic: ContextVar[str | None] = ContextVar(
            "geoharness_empty_response_diagnostic", default=None,
        )
        self._response_diagnostic: ContextVar[str | None] = ContextVar(
            "geoharness_response_diagnostic", default=None,
        )
        self._unsettled_usage: ContextVar[UsageSnapshot | None] = ContextVar(
            "geoharness_unsettled_usage", default=None,
        )
        # Avoid stacking the SDK's retry loop inside the upstream client's loop.
        self._sdk_client = self._client.with_options(max_retries=0)
        self._client = _ClientProxy(self._sdk_client, self._request_state)

    @property
    def last_empty_response_diagnostic(self) -> str | None:
        return self._empty_diagnostic.get()

    @property
    def last_response_diagnostic(self) -> str | None:
        return self._response_diagnostic.get()

    def _record_usage(self, usage: UsageSnapshot) -> None:
        self._unsettled_usage.set(_add_usage(self._unsettled_usage.get() or UsageSnapshot(), usage))

    def ack_usage(self, usage: UsageSnapshot) -> None:
        """Acknowledge usage already charged by the engine, including compaction events."""
        pending = self._unsettled_usage.get() or UsageSnapshot()
        self._unsettled_usage.set(UsageSnapshot(
            input_tokens=max(pending.input_tokens - usage.input_tokens, 0),
            output_tokens=max(pending.output_tokens - usage.output_tokens, 0),
        ))

    def consume_unsettled_usage(self) -> UsageSnapshot:
        """Take confirmed provider usage once after cancellation or an exception."""
        pending = self._unsettled_usage.get() or UsageSnapshot()
        self._unsettled_usage.set(UsageSnapshot())
        return pending

    def _set_empty_diagnostic(self, diagnostic: str) -> None:
        self._empty_diagnostic.set(diagnostic)
        self._response_diagnostic.set(diagnostic)

    def _is_retryable(self, exc: Exception) -> bool:
        state = self._request_state.get()
        # Once text is visible, replaying would duplicate the user's streamed answer.
        if state is not None and state.visible_chars:
            return False
        return super()._is_retryable(exc)

    async def _stream_request(
        self, request: ApiMessageRequest, state: _RequestState,
    ) -> AsyncIterator[ApiStreamEvent]:
        """Scope provider state to each advancement, never to a suspended yield.

        An async generator may be resumed or finalized by a different task. Tokens
        must be reset in the same context that created them, before handing control
        back to the consumer. Closing the upstream iterator follows the same rule.
        """
        stream = super().stream_message(request)
        try:
            while True:
                token = self._request_state.set(state)
                try:
                    event = await anext(stream)
                except StopAsyncIteration:
                    return
                finally:
                    self._request_state.reset(token)
                yield event
        finally:
            token = self._request_state.set(state)
            try:
                try:
                    await stream.aclose()
                finally:
                    # The upstream outer iterator does not explicitly close its
                    # nested parser; release HTTP resources before returning.
                    for provider_stream in list(state.active_streams):
                        await _close_provider_stream(provider_stream, state)
            finally:
                self._request_state.reset(token)

    async def stream_message(self, request: ApiMessageRequest) -> AsyncIterator[ApiStreamEvent]:
        self._empty_diagnostic.set(None)
        self._response_diagnostic.set(None)
        usage = UsageSnapshot()
        current = request
        extras = dict(self.settings.request_extra_body)
        fallback_extras = {**extras, **self.settings.empty_response_fallback_extra_body}
        fallback_model = self.settings.empty_response_fallback_model.strip() or request.model
        alternative = fallback_model != request.model or fallback_extras != extras
        attempts = 1 + (self.settings.empty_response_retries if alternative else 0)
        last_empty: ApiMessageCompleteEvent | None = None
        last_diagnostic = ""
        for attempt in range(attempts):
            state = _RequestState(
                extra_body=extras, include_stream_usage=self.settings.include_stream_usage,
                record_usage=self._record_usage,
            )
            final: ApiMessageCompleteEvent | None = None
            try:
                async with aclosing(self._stream_request(current, state)) as stream:
                    async for event in stream:
                        if isinstance(event, ApiMessageCompleteEvent):
                            final = event
                        else:
                            if isinstance(event, ApiTextDeltaEvent):
                                state.visible_chars += len(event.text)
                                state.visible_text.append(event.text)
                            yield event
            except Exception as exc:
                usage = _add_usage(usage, state.reported_usage)
                if state.visible_chars:
                    self._response_diagnostic.set(
                        f"模型 {current.model} 的回答传输中断（{type(exc).__name__}）；"
                        "已保留收到的部分正文，回答尚未完成，未完成的工具调用不会执行"
                    )
                    yield ApiMessageCompleteEvent(
                        message=ConversationMessage(role="assistant", content=[
                            TextBlock(text="".join(state.visible_text)),
                        ]),
                        usage=usage, stop_reason="transport_error",
                    )
                    return
                if last_empty is None:
                    raise
                # Keep the billed first response even if the recovery transport fails.
                self._set_empty_diagnostic(
                    f"{last_diagnostic}；恢复请求失败（{type(exc).__name__}），"
                    "工具结果已保留，可继续处理"
                )
                yield replace(last_empty, usage=usage)
                return
            if final is None:
                raise RuntimeError("Model stream finished without a final message")
            # Normally the proxy observes these tokens before the parser's final event.
            # Retain compatibility if an upstream stream adapter reports usage only here.
            unobserved = UsageSnapshot(
                input_tokens=max(
                    final.usage.input_tokens - state.reported_usage.input_tokens, 0,
                ),
                output_tokens=max(
                    final.usage.output_tokens - state.reported_usage.output_tokens, 0,
                ),
            )
            self._record_usage(unobserved)
            usage = _add_usage(usage, _add_usage(state.reported_usage, unobserved))
            empty = final.message.is_effectively_empty()
            if not empty:
                yield replace(final, usage=usage)
                return
            diagnostic = self._diagnostic(current, final, state, attempt + 1)
            last_empty = final
            last_diagnostic = diagnostic
            if state.visible_chars or attempt + 1 >= attempts:
                self._set_empty_diagnostic(diagnostic)
                yield replace(final, usage=usage)
                return
            yield ApiRetryEvent(
                message=(
                    f"{diagnostic}；仅重试模型请求，临时使用 {fallback_model}，"
                    "已完成的工具不会再次执行"
                ),
                attempt=attempt + 1, max_attempts=attempts, delay_seconds=0,
            )
            recovery_limit = self.settings.empty_response_max_tokens or request.max_tokens
            current = replace(
                request, model=fallback_model,
                max_tokens=min(request.max_tokens, recovery_limit),
            )
            extras = fallback_extras

    @staticmethod
    def _diagnostic(
        request: ApiMessageRequest, event: ApiMessageCompleteEvent,
        state: _RequestState, attempt: int,
    ) -> str:
        reason = event.stop_reason or "unknown"
        explanation = (
            "输出预算耗尽，仅返回推理内容，没有可见回答或工具调用"
            if reason in {"length", "max_tokens"} and state.reasoning_chars
            else "服务未返回可见回答或工具调用"
        )
        return (
            f"模型 {request.model} 空回复：{explanation}"
            f"（finish_reason={reason}, output_tokens={event.usage.output_tokens}, "
            f"max_tokens={request.max_tokens}, reasoning_chars={state.reasoning_chars}, "
            f"raw_content_chars={state.raw_content_chars}, request_attempts={attempt}）。"
            "这不表示先前的工具输出为空"
        )
