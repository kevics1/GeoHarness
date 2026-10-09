"""GeoHarness query lifecycle extensions without changes to OpenHarness."""

from __future__ import annotations

from typing import AsyncIterator

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.engine.query import QueryContext, remember_user_goal, run_query
from openharness.engine.query_engine import QueryEngine
from openharness.engine.stream_events import AssistantTurnComplete, ErrorEvent, StreamEvent
from openharness.hooks import HookEvent

from geoharness.cartography.jobs import begin_map_request
from geoharness.cartography.workflow import reset_map_workflow


class GeoQueryEngine(QueryEngine):
    async def submit_message(self, prompt: str | ConversationMessage) -> AsyncIterator[StreamEvent]:
        # continue_pending uses the original budget. Only actual new user input resets it.
        user_message = (
            prompt if isinstance(prompt, ConversationMessage)
            else ConversationMessage.from_user_text(prompt)
        )
        reset_map_workflow(self.tool_metadata)
        config = self.tool_metadata.get("geoharness_config")
        if config is not None:
            continuation = user_message.text.strip().strip("。.!！").lower() in {
                "继续", "接着做", "continue", "resume",
            }
            begin_map_request(self.tool_metadata, cwd=self._cwd, config=config,
                              continuation=continuation)
        if user_message.text.strip() and not self._tool_metadata.pop(
            "_suppress_next_user_goal", False,
        ):
            remember_user_goal(self._tool_metadata, user_message.text)
        self._messages.append(user_message)
        query_messages = list(self._messages)
        try:
            if self._hook_executor is not None:
                await self._hook_executor.execute(
                    HookEvent.USER_PROMPT_SUBMIT,
                    {"event": HookEvent.USER_PROMPT_SUBMIT.value, "prompt": user_message.text},
                )
            context = QueryContext(
                api_client=self._api_client, tool_registry=self._tool_registry,
                permission_checker=self._permission_checker, cwd=self._cwd,
                model=self._model, system_prompt=self._system_prompt, max_tokens=self._max_tokens,
                context_window_tokens=self._context_window_tokens,
                auto_compact_threshold_tokens=self._auto_compact_threshold_tokens,
                max_turns=self._max_turns, permission_prompt=self._permission_prompt,
                ask_user_prompt=self._ask_user_prompt, hook_executor=self._hook_executor,
                tool_metadata=self._tool_metadata,
            )
            coordinator_context = self._build_coordinator_context_message()
            if coordinator_context is not None:
                query_messages.append(coordinator_context)
            async for event, usage in run_query(context, query_messages):
                # Tool results are appended between events, and an empty follow-up
                # has no AssistantTurnComplete. Persist every mutation, including
                # the final failure.
                self._messages = list(query_messages)
                if usage is not None:
                    self._cost_tracker.add(usage)
                    self._ack_usage(usage)
                yield self._diagnose_event(event)
                if isinstance(event, AssistantTurnComplete):
                    diagnostic = getattr(self._api_client, "last_response_diagnostic", None)
                    if diagnostic:
                        yield ErrorEvent(message=diagnostic)
        finally:
            self._messages = list(query_messages)
            self._settle_usage()

    async def continue_pending(self, *, max_turns: int | None = None) -> AsyncIterator[StreamEvent]:
        previous_usage = self.total_usage.model_copy()
        try:
            async for event in super().continue_pending(max_turns=max_turns):
                current_usage = self.total_usage.model_copy()
                self._ack_usage(UsageSnapshot(
                    input_tokens=current_usage.input_tokens - previous_usage.input_tokens,
                    output_tokens=current_usage.output_tokens - previous_usage.output_tokens,
                ))
                previous_usage = current_usage
                yield self._diagnose_event(event)
                if isinstance(event, AssistantTurnComplete):
                    diagnostic = getattr(self._api_client, "last_response_diagnostic", None)
                    if diagnostic:
                        yield ErrorEvent(message=diagnostic)
        finally:
            self._settle_usage()

    def _ack_usage(self, usage) -> None:
        acknowledge = getattr(self._api_client, "ack_usage", None)
        if acknowledge is not None:
            acknowledge(usage)

    def _settle_usage(self) -> None:
        consume = getattr(self._api_client, "consume_unsettled_usage", None)
        if consume is not None:
            self._cost_tracker.add(consume())

    def _diagnose_event(self, event: StreamEvent) -> StreamEvent:
        if isinstance(event, ErrorEvent) and event.message.startswith(
            "Model returned an empty assistant message.",
        ):
            diagnostic = getattr(self._api_client, "last_empty_response_diagnostic", None)
            if diagnostic:
                return ErrorEvent(message=diagnostic)
        return event
