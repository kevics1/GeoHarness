"""GeoHarness query lifecycle extensions without changes to OpenHarness."""

from __future__ import annotations

from contextlib import aclosing, nullcontext
from copy import deepcopy
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
        user_message = (
            prompt if isinstance(prompt, ConversationMessage)
            else ConversationMessage.from_user_text(prompt)
        )
        continuation = user_message.text.strip().strip("。.!！").lower() in {
            "继续", "接着做", "continue", "resume",
        }
        if continuation and self.has_pending_continuation() and all(
            block.type == "text" for block in user_message.content
        ):
            async with aclosing(self.continue_pending()) as stream:
                async for event in stream:
                    yield event
            return
        # Only a new task starts fresh budgets and recovery policy. A pending
        # continuation does not append a user message or replace the task goal.
        self._query_recovery_state = None
        self.tool_metadata.pop("geo_query_recovery", None)
        reset_map_workflow(self.tool_metadata)
        config = self.tool_metadata.get("geoharness_config")
        if config is not None:
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
            async with aclosing(self._scoped_query_events(
                run_query(context, query_messages),
            )) as stream:
                async for event, usage in stream:
                    # Keep every mutation, including tool results before an empty follow-up.
                    self._messages = list(query_messages)
                    if usage is not None:
                        self._cost_tracker.add(usage)
                        self._ack_usage(usage)
                    yield self._diagnose_event(event)
                    if isinstance(event, AssistantTurnComplete):
                        diagnostic = getattr(self._api_client, "last_response_diagnostic", None)
                        if diagnostic:
                            self._record_failure_state()
                            yield ErrorEvent(message=diagnostic)
        finally:
            self._messages = list(query_messages)
            self._settle_usage()

    async def continue_pending(self, *, max_turns: int | None = None) -> AsyncIterator[StreamEvent]:
        previous_usage = self.total_usage.model_copy()
        try:
            async with aclosing(self._scoped_query_events(
                super().continue_pending(max_turns=max_turns),
            )) as stream:
                async for event in stream:
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
                            self._record_failure_state()
                            yield ErrorEvent(message=diagnostic)
        finally:
            self._settle_usage()

    async def _scoped_query_events(self, stream):
        """Bind recovery only while advancing/closing, never across a yielded event.

        Keeping a ContextVar token across yield would break when another task
        resumes or finalizes the async generator. The state object outlives the
        binding, so tool follow-ups and explicit pending continuations share it.
        """
        client = self._api_client
        scope = getattr(client, "query_recovery_scope", None)
        state = getattr(self, "_query_recovery_state", None)
        if scope is not None and (
            state is None or getattr(self, "_query_recovery_client", None) is not client
        ):
            from geoharness.api_client import QueryRecoveryState

            # Advisory hooks run before the agent query and own no recovery mode.
            # Settle their confirmed usage in this task before binding a query;
            # a later task that closes the stream cannot read this ContextVar.
            consume = getattr(client, "consume_unsettled_usage", None)
            if consume is not None:
                self._cost_tracker.add(consume())
            saved = self.tool_metadata.get("geo_query_recovery", {})
            state = QueryRecoveryState(**saved, client_id=id(client)) if saved else (
                QueryRecoveryState()
            )
            self._query_recovery_state = state
            self._query_recovery_client = client

        def binding():
            return scope(state) if scope is not None else nullcontext()

        try:
            while True:
                with binding():
                    try:
                        event = await anext(stream)
                    except StopAsyncIteration:
                        return
                self._persist_query_mode()
                yield event
        finally:
            with binding():
                try:
                    await stream.aclose()
                finally:
                    close = getattr(client, "close_query_streams", None)
                    if close is not None and state is not None:
                        await close(state)
            self._persist_query_mode()

    def _query_binding(self):
        state = getattr(self, "_query_recovery_state", None)
        scope = getattr(self._api_client, "query_recovery_scope", None)
        return scope(state) if state is not None and scope is not None else nullcontext()

    def _persist_query_mode(self) -> None:
        state = getattr(self, "_query_recovery_state", None)
        if state is not None and state.fallback_active:
            self.tool_metadata["geo_query_recovery"] = {
                key: getattr(state, key) for key in (
                    "fallback_active", "model", "max_tokens", "source_model", "config_fingerprint",
                )
            }
        else:
            self.tool_metadata.pop("geo_query_recovery", None)

    def _ack_usage(self, usage) -> None:
        acknowledge = getattr(self._api_client, "ack_usage", None)
        if acknowledge is not None:
            with self._query_binding():
                acknowledge(usage)

    def _settle_usage(self) -> None:
        consume = getattr(self._api_client, "consume_unsettled_usage", None)
        if consume is not None:
            with self._query_binding():
                self._cost_tracker.add(consume())

    def _record_failure_state(self) -> None:
        failure = getattr(self._api_client, "last_failure_metadata", None)
        if isinstance(failure, dict):
            history = self.tool_metadata.setdefault("geo_request_failures", [])
            if not isinstance(history, list):
                history = self.tool_metadata["geo_request_failures"] = []
            if not history or history[-1] != failure:
                history.append(deepcopy(failure))
                del history[:-16]

    def _diagnose_event(self, event: StreamEvent) -> StreamEvent:
        if isinstance(event, AssistantTurnComplete):
            # Save observations before yielding a partial completion: the caller
            # may close the stream immediately after receiving this event.
            self._record_failure_state()
        if isinstance(event, ErrorEvent) and event.message.startswith(
            ("Network error:", "API error:"),
        ):
            diagnostic = getattr(self._api_client, "last_failure_diagnostic", None)
            if diagnostic:
                self._record_failure_state()
                return ErrorEvent(message=diagnostic)
        if isinstance(event, ErrorEvent) and event.message.startswith(
            "Model returned an empty assistant message.",
        ):
            self._record_failure_state()
            diagnostic = getattr(self._api_client, "last_empty_response_diagnostic", None)
            if diagnostic:
                return ErrorEvent(message=diagnostic)
        return event
