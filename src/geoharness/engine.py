"""GeoHarness query lifecycle extensions without changes to OpenHarness."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import aclosing, nullcontext
from copy import deepcopy
from dataclasses import replace
from typing import AsyncIterator

from openharness.engine.messages import ConversationMessage, ToolResultBlock
from openharness.engine.query import MaxTurnsExceeded, QueryContext, remember_user_goal, run_query
from openharness.engine.query_engine import QueryEngine
from openharness.engine.stream_events import (
    AssistantTurnComplete,
    ErrorEvent,
    StatusEvent,
    StreamEvent,
    ToolExecutionCompleted,
)
from openharness.hooks import HookEvent

from geoharness.cartography.jobs import begin_map_request
from geoharness.cartography.workflow import reset_map_workflow


class _AwaitingUserAnswer(BaseException):
    """Control flow, kept outside upstream API-error/retry handling."""


class _ReviewStopped(BaseException):
    """An operational stop has no question for the user to answer."""


class _QuestionGateClient:
    def __init__(self, client, metadata):
        self.client, self.metadata = client, metadata

    def __getattr__(self, name):
        return getattr(self.client, name)

    async def stream_message(self, request):
        if self.metadata.get('geo_clarification', {}).get('pending'):
            raise _AwaitingUserAnswer()
        if self.metadata.get('geo_review_stop'):
            raise _ReviewStopped()
        async with aclosing(self.client.stream_message(request)) as stream:
            async for event in stream:
                yield event


class GeoQueryEngine(QueryEngine):
    def _review_stop_notice(self):
        stop = self.tool_metadata.get('geo_review_stop', {})
        count = stop.get('review_requests_used')
        limit = stop.get('review_request_limit')
        details = (f'本任务已请求{count}/{limit}次。' if count is not None else '')
        page_count = stop.get('page_review_requests_used')
        if page_count is not None:
            details += f"当前页已请求{page_count}/{stop.get('page_review_request_limit')}次。"
        summary = stop.get('summary', '')
        paths = stop.get('paths', [])
        pages = stop.get('pending_pages', [])
        return ('已达到自动审图上限，自动改图和新增审查已停止。' + details
                + (f'最近审查：{summary}。' if summary else '')
                + (f'未完成页面索引（从0起）：{pages}。' if pages else '')
                + '现有图片和报告已保留；严重缺陷或未完成检查仍保持草稿。'
                + ('已有文件：' + '；'.join(paths) + '。' if paths else '')
                + '这是一条停止通知，无需回答问题。“继续”不会重置次数或重启审图。'
                '可输入“整理草稿和问题清单”，继续整理已有成果；'
                '需要新增审图时，请人工检查后在config.yaml调整审图上限并重启恢复原会话。')

    def _migrate_review_stop(self):
        pending = self.tool_metadata.get('geo_clarification', {}).get('pending')
        config = self.tool_metadata.get('geoharness_config')
        if config and config.map_review.workflow == 'single_pass':
            self.tool_metadata.pop('geo_review_stop', None)
            if (isinstance(pending, dict) and not pending.get('policy_ref')
                    and str(pending.get('question', '')).startswith('已达到自动审图上限')):
                self.tool_metadata['geo_clarification'].pop('pending')
            return
        if (isinstance(pending, dict) and not pending.get('policy_ref')
                and str(pending.get('question', '')).startswith('已达到自动审图上限')):
            self.tool_metadata['geo_clarification'].pop('pending')
            self.tool_metadata.setdefault('geo_review_stop', {'reason': 'review_limit'})
        # Older snapshots omitted the pending notice; the actual final tool
        # result still contains the stop evidence. Do not replay that tool.
        if (not self.tool_metadata.get('geo_review_stop', {}).get('review_requests_used')
                and self._messages):
            for block in self._messages[-1].content:
                if not isinstance(block, ToolResultBlock) or block.is_error:
                    continue
                try:
                    body, _ = json.JSONDecoder().raw_decode(block.content.lstrip())
                    diagnostic = body.get('diagnostics', {})
                    if body.get('status') != 'passed' and (
                            body.get('automatic_review_stopped') or
                            diagnostic.get('automatic_review_stopped')):
                        self._set_review_stop(body)
                except (ValueError, TypeError, AttributeError):
                    continue
        stop = self.tool_metadata.get('geo_review_stop', {})
        config = self.tool_metadata.get('geoharness_config')
        if config and self.tool_metadata.get('geo_task_record') and (
                (isinstance(stop.get('review_request_limit'), int) and
                 config.map_review.max_review_requests_per_task > stop['review_request_limit']) or
                (isinstance(stop.get('page_review_request_limit'), int) and
                 config.map_review.max_review_requests_per_page >
                 stop['page_review_request_limit'])):
            from geoharness.cartography.review_limits import review_limit_status

            if not review_limit_status(self.tool_metadata, config)['automatic_review_stopped']:
                self.tool_metadata.pop('geo_review_stop', None)

    def _set_review_stop(self, review):
        diagnostic = review.get('diagnostics', {})
        self.tool_metadata['geo_review_stop'] = {
            'reason': 'review_limit',
            'summary': str(review.get('summary', ''))[:500],
            'pending_pages': (review.get('pending_pages') or [])[:64],
            **{key: diagnostic.get(key, review.get(key))
               for key in ('review_requests_used', 'review_request_limit',
                           'page_review_requests_used', 'page_review_request_limit')},
            'paths': list(dict.fromkeys(
                str(page[key])
                for page in [review, *(review.get('pages') or []),
                             *(review.get('partial_pages') or [])]
                if isinstance(page, dict)
                for key in ('image_path', 'report_path') if page.get(key)))[:16]}

    async def submit_message(self, prompt: str | ConversationMessage) -> AsyncIterator[StreamEvent]:
        user_message = (
            prompt if isinstance(prompt, ConversationMessage)
            else ConversationMessage.from_user_text(prompt)
        )
        continuation = user_message.text.strip().strip("。.!！").lower() in {
            "继续", "接着做", "continue", "resume",
        }
        self._migrate_review_stop()
        review_stop = self.tool_metadata.get('geo_review_stop')
        if review_stop and continuation:
            yield StatusEvent(message=self._review_stop_notice())
            return
        if review_stop:
            # Explicit follow-up can inspect/summarize the same task. Never start
            # a new task merely to reset its durable review usage.
            self.tool_metadata.pop('geo_review_stop', None)
            self._tool_metadata['_suppress_next_user_goal'] = True
        pending = self.tool_metadata.get('geo_clarification', {}).get('pending')
        if pending:
            if (continuation or not user_message.text.strip()
                    or user_message.text.strip().strip('。.!！').lower() in {
                        '取消', 'cancel', 'cancelled', '(no response)'}):
                yield StatusEvent(message=f"需要先回答：{pending['question']}")
                return
            from geoharness.analysis.policy import resolve_policy_answer

            resolve_policy_answer(self.tool_metadata, user_message.text)
            self.tool_metadata['geo_clarification'].pop('pending', None)
            self._tool_metadata['_suppress_next_user_goal'] = True
        if continuation and self.has_pending_continuation() and all(
            block.type == "text" for block in user_message.content
        ):
            async with aclosing(self.continue_pending()) as stream:
                async for event in stream:
                    yield event
            return
        # Only a new task starts fresh budgets and recovery policy. A pending
        # continuation does not append a user message or replace the task goal.
        fresh_task = not pending and not review_stop and not (
            continuation and self.tool_metadata.get('geo_task_record'))
        if fresh_task:
            self._query_recovery_state = None
            self.tool_metadata.pop("geo_query_recovery", None)
            reset_map_workflow(self.tool_metadata)
        config = self.tool_metadata.get("geoharness_config")
        if config is not None:
            from geoharness.task_budget import start_task

            if fresh_task:
                start_task(self.tool_metadata, config)
                from geoharness.task_state import begin_task

                begin_task(self.tool_metadata, cwd=self._cwd, goal=user_message.text)
            begin_map_request(self.tool_metadata, cwd=self._cwd, config=config,
                              continuation=continuation or bool(pending) or bool(review_stop))
        if not continuation and user_message.text.strip() and not self._tool_metadata.pop(
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
                self._run_query_batches(context, query_messages),
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
        self._migrate_review_stop()
        if self.tool_metadata.get('geo_review_stop'):
            yield StatusEvent(message=self._review_stop_notice())
            return
        context = QueryContext(
            api_client=self._api_client, tool_registry=self._tool_registry,
            permission_checker=self._permission_checker, cwd=self._cwd,
            model=self._model, system_prompt=self._system_prompt, max_tokens=self._max_tokens,
            context_window_tokens=self._context_window_tokens,
            auto_compact_threshold_tokens=self._auto_compact_threshold_tokens,
            max_turns=max_turns if max_turns is not None else self._max_turns,
            permission_prompt=self._permission_prompt, ask_user_prompt=self._ask_user_prompt,
            hook_executor=self._hook_executor, tool_metadata=self._tool_metadata,
        )
        try:
            async with aclosing(self._scoped_query_events(
                self._run_query_batches(context, self._messages),
            )) as stream:
                async for event, usage in stream:
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
            self._settle_usage()

    async def _run_query_batches(self, context, messages):
        """Resume only exhausted batches with new successful work, using the same history.

        A normal stop, provider error, cancellation, or closed consumer never
        triggers another request. Repeated successful calls do not constitute
        progress. A fresh failure at the boundary gets a recovery opportunity;
        repeated failures or a whole batch without new results need intervention.
        """
        config = self.tool_metadata.get("geoharness_config")
        automatic = getattr(getattr(config, "agent", None), "auto_continue", False)
        self.tool_metadata.setdefault('geo_clarification', {})
        context = replace(context, api_client=_QuestionGateClient(
            context.api_client, self.tool_metadata))
        from geoharness.performance import MeasuredRegistry

        context = replace(context, tool_registry=MeasuredRegistry(
            context.tool_registry, self.tool_metadata))
        seen: set[str] = set()
        seen_failures: set[str] = set()
        while True:
            successful: set[str] = set()
            failures: dict[str, int] = {}
            try:
                async with aclosing(run_query(context, messages)) as stream:
                    async for event, usage in stream:
                        if isinstance(event, ToolExecutionCompleted):
                            counts = self.tool_metadata.setdefault('geo_tool_call_counts', {})
                            count = counts.setdefault(event.tool_name, {'calls': 0, 'errors': 0})
                            count['calls'] += 1
                            count['errors'] += int(event.is_error)
                            if (event.tool_name in {'geo_map_review', 'geo_cartography',
                                                   'geo_python', 'geo_qgis_project'}
                                    and not event.is_error):
                                try:
                                    review, _ = json.JSONDecoder().raw_decode(event.output.lstrip())
                                    stopped = review.get('automatic_review_stopped') or review.get(
                                        'diagnostics', {}).get('automatic_review_stopped')
                                    once = config and config.map_review.workflow == 'single_pass'
                                    if once:
                                        stopped = False
                                    if stopped and review.get('status') != 'passed':
                                        self._set_review_stop(review)
                                    progress = review.get('diagnostics', {}).get(
                                        'repair_progress', {})
                                    if (not once and not stopped
                                            and progress.get('requires_user_decision') is True):
                                        self.tool_metadata['geo_clarification']['pending'] = {
                                            'question': '多轮审图仍报告同组问题，尚不能确认继续'
                                            '改图的收益。请选择局部放大、保留当前草稿，'
                                            '或说明希望优先解决的具体问题。'}
                                except (ValueError, TypeError, AttributeError):
                                    pass
                            fingerprint = self._progress_fingerprint(event)
                            if event.is_error:
                                failures[fingerprint] = failures.get(fingerprint, 0) + 1
                            else:
                                successful.add(fingerprint)
                        yield event, usage
                return
            except _ReviewStopped:
                self._messages[:] = messages
                yield StatusEvent(message=self._review_stop_notice()), None
                return
            except _AwaitingUserAnswer:
                self._messages[:] = messages
                question = self.tool_metadata['geo_clarification']['pending']['question']
                yield StatusEvent(message=f"等待用户回答：{question}"), None
                return
            except MaxTurnsExceeded:
                # run_query appends its tool results after yielding the final
                # ToolExecutionCompleted; preserve that final mutation as well.
                self._messages[:] = messages
                if self.tool_metadata.get('geo_review_stop'):
                    yield StatusEvent(message=self._review_stop_notice()), None
                    return
                if self.tool_metadata.get('geo_clarification', {}).get('pending'):
                    question = self.tool_metadata['geo_clarification']['pending']['question']
                    yield StatusEvent(message=f"等待用户回答：{question}"), None
                    return
                if not automatic or not self.has_pending_continuation():
                    raise
                last_results = [
                    block for block in messages[-1].content if isinstance(block, ToolResultBlock)
                ]
                repeated_failure = any(
                    count > 1 or key in seen_failures for key, count in failures.items()
                )
                if not successful.difference(seen) or (
                    all(r.is_error for r in last_results) and repeated_failure
                ):
                    yield ErrorEvent(message=(
                        "自动续跑已停止：本批没有新的成功工具结果，或末组工具重复失败。"
                        "已保留原任务和全部工具结果，请先处理错误后继续。"
                    )), None
                    return
                seen.update(successful)
                seen_failures.update(failures)
                yield StatusEvent(message=(
                    "本批回合已结束，正在根据已有工具结果自动继续原任务。"
                )), None

    @staticmethod
    def _progress_fingerprint(event: ToolExecutionCompleted) -> str:
        """Count changed results, not fresh run timestamps or repeated read calls."""
        if event.tool_name == 'geo_task':
            try:
                packet = json.loads(event.output)
                if isinstance(packet.get('nodes'), list):
                    return hashlib.sha256(json.dumps([
                        {'id': n['id'], 'status': n['status']} for n in packet['nodes']
                    ], sort_keys=True).encode()).hexdigest()
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
        if event.tool_name == "geo_map_review":
            try:
                review, _ = json.JSONDecoder().raw_decode(event.output.lstrip())
                diagnostic = review.get("diagnostics", {})
                quality = diagnostic.get("semantic_quality", {})
                if quality.get("complete") and quality.get("signature") and quality.get("map_id"):
                    return hashlib.sha256(json.dumps({
                        "tool": event.tool_name, "status": review.get("status"),
                        "quality": quality["signature"],
                    }, sort_keys=True).encode()).hexdigest()
                if review.get("status") == "unverified" and diagnostic.get("stage") == "cache":
                    # Waiting/counter updates are not completed work or new service attempts.
                    held = {key: diagnostic.get(key) for key in (
                        "error_type", "http_status", "failure_stage", "next_action")}
                    return hashlib.sha256(json.dumps(held, sort_keys=True).encode()).hexdigest()
            except (TypeError, ValueError, AttributeError):
                pass
        volatile = {
            "run_dir", "script_path", "manifest_path", "stdout_path", "stderr_path",
            "duration_seconds", "elapsed_seconds", "duration_ms",
            "operation_id", "evidence_call", "evidence_path", "evidence_sha256",
            "execution_id", "target_operation_id", "execution_evidence_call",
        }

        def stable(value):
            if isinstance(value, dict):
                return {key: stable(item) for key, item in value.items() if key not in volatile}
            if isinstance(value, list):
                return [stable(item) for item in value]
            return value

        try:
            output = json.dumps(
                stable(json.loads(event.output)), sort_keys=True, ensure_ascii=False,
            )
        except (TypeError, ValueError):
            output = event.output
        if output.startswith("[Tool output truncated]\n"):
            # OpenHarness adds a fresh call id and artifact path to each preview.
            # Neither identifies new work. Do not follow paths from tool text.
            header, separator, preview = output.partition("\n\nPreview:\n")
            header = re.sub(r"(?m)^(?:Tool use id|Full output saved to):[^\n]*\n?", "", header)
            output = header + (separator + preview if separator else "")
        output = re.sub(r"\d{8}T\d{6}Z-[0-9a-f]{12}\b", "<run>", output)
        return hashlib.sha256(f"{event.tool_name}\n{output}".encode("utf-8")).hexdigest()

    async def _scoped_query_events(self, stream):
        """Bind recovery only while advancing/closing, never across a yielded event.

        Keeping a ContextVar token across yield would break when another task
        resumes or finalizes the async generator. The state object outlives the
        binding, so tool follow-ups and explicit pending continuations share it.
        """
        self.tool_metadata["geoharness_permission_checker"] = self._permission_checker
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
        metrics = getattr(self._api_client, "request_metrics", None)
        if metrics and metrics[-1] is not getattr(self, '_last_saved_metric', None):
            # Streaming text can yield thousands of events. Merge only when a request
            # finishes, preserving restored diagnostics without quadratic per-token work.
            saved = self.tool_metadata.get('geo_request_metrics', [])
            indexed = {(m.get('request_id'), m.get('attempt')): m for m in saved
                       if isinstance(m, dict)}
            indexed.update({(m.get('request_id'), m.get('attempt')): m for m in metrics})
            config = self.tool_metadata.get('geoharness_config')
            limit = getattr(getattr(config, 'agent', None), 'request_metrics_limit', 2048)
            self.tool_metadata['geo_request_metrics'] = list(indexed.values())[-limit:]
            self._last_saved_metric = metrics[-1]
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
