"""Replay query tool turns through the real SDK with an offline HTTP transport."""

import asyncio
import json
from dataclasses import asdict, replace
from functools import partial
from uuid import UUID

import httpx
import pytest
from openai import AsyncOpenAI
from openharness.api import openai_client as upstream
from openharness.api.client import ApiMessageRequest, ApiRetryEvent
from openharness.api.errors import RequestFailure
from openharness.config.settings import PermissionSettings
from openharness.engine.messages import ConversationMessage
from openharness.engine.query import (
    _extract_completion_token_limit,
    _is_completion_token_limit_error,
    _is_prompt_too_long_error,
)
from openharness.engine.stream_events import AssistantTextDelta, StatusEvent
from openharness.permissions.checker import PermissionChecker
from openharness.tools.base import ToolRegistry, ToolResult

from geoharness.api_client import GeoOpenAICompatibleClient, QueryRecoveryState
from geoharness.config.settings import AgentConfig
from geoharness.engine import GeoQueryEngine
from geoharness.tools.geo_python import GeoPythonTool


def sse_chunk(
    *, text=None, reasoning=None, finish=None, arguments=None, usage=None, tool_name="geo_python",
):
    delta = {}
    if text is not None:
        delta["content"] = text
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if arguments is not None:
        delta["tool_calls"] = [{"index": 0, "id": "call-synthetic", "type": "function",
                                "function": {"name": tool_name, "arguments": arguments}}]
    value = {"id": "completion-synthetic", "object": "chat.completion.chunk", "created": 0,
             "model": "synthetic", "choices": [{"index": 0, "delta": delta,
                                                  "finish_reason": finish}]}
    if usage is not None:
        value["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1],
                          "total_tokens": sum(usage)}
    return "data: " + json.dumps(value) + "\n\n"


def sdk_client(monkeypatch, responses, **settings):
    calls = []

    async def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        response = (await responses(body) if callable(responses) else responses[len(calls) - 1])
        if isinstance(response, Exception):
            raise response
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=response + "data: [DONE]\n\n")

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(upstream, "AsyncOpenAI", partial(AsyncOpenAI, http_client=transport))
    client = GeoOpenAICompatibleClient("test-only", base_url="https://offline.invalid/v1",
                                      settings=AgentConfig(**settings))
    return client, calls


class RecordingPythonTool(GeoPythonTool):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def execute(self, arguments, context):
        self.calls.append(arguments.code)
        return ToolResult(output="synthetic execution completed")

    def is_read_only(self, arguments):
        return True


def api_request(label="synthetic"):
    return ApiMessageRequest(model="primary(max)", messages=[
        ConversationMessage.from_user_text(label),
    ], max_tokens=16384, tools=[{
        "name": "geo_python", "description": "Synthetic execution", "input_schema": {
            "type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"],
        },
    }])


async def query_request(client, state, request=None):
    events = []
    stream = client.stream_message(request or api_request())
    try:
        while True:
            with client.query_recovery_scope(state):
                try:
                    event = await anext(stream)
                except StopAsyncIteration:
                    break
            events.append(event)
    finally:
        with client.query_recovery_scope(state):
            await stream.aclose()
    return events


@pytest.mark.asyncio
async def test_sdk_sse_error_preserves_safe_packet_and_exception_metadata(monkeypatch):
    response = "data: " + json.dumps({"error": {
        "message": "private provider content synthetic-secret", "code": "synthetic_error",
    }}) + "\n\n"
    client, calls = sdk_client(monkeypatch, [response])
    try:
        with pytest.raises(RequestFailure):
            _ = [event async for event in client.stream_message(api_request())]
        metadata = client.last_failure_metadata
        assert metadata["category"] == "provider_sse"
        assert metadata["exception_type"] == "RequestFailure"
        assert "APIError" in metadata["exception_chain"]
        assert metadata["raw_chunk_count"] == 1
        assert metadata["raw_chunk_source"] == "sse"
        assert metadata["provider_attempts"] == 1
        assert metadata["last_packet_at"] > 0
        assert metadata["elapsed_seconds"] >= 0
        assert "provider_sse" in client.last_failure_diagnostic
        assert "APIError" in client.last_failure_diagnostic
        safe = json.dumps(metadata) + client.last_failure_diagnostic
        assert "private provider content" not in safe
        assert "synthetic-secret" not in safe
        assert len(calls) == 1
    finally:
        await client._sdk_client.close()


@pytest.mark.asyncio
async def test_sdk_http_retry_events_and_terminal_metadata_omit_provider_body(
    monkeypatch, caplog, capsys,
):
    client, calls = sdk_client(monkeypatch, [httpx.Response(503, json={"error": {
        "message": "private provider body synthetic-secret", "type": "server_error",
    }}) for _ in range(4)])
    monkeypatch.setattr(upstream, "BASE_DELAY", 0)
    events = []
    try:
        with pytest.raises(RequestFailure) as failure:
            async for event in client.stream_message(api_request()):
                events.append(event)
        retries = [event for event in events if isinstance(event, ApiRetryEvent)]
        assert len(retries) == 3
        assert len(calls) == 4
        metadata = client.last_failure_metadata
        assert metadata["category"] == "http"
        assert metadata["status_code"] == 503
        assert metadata["provider_attempts"] == 4
        assert metadata["raw_chunk_count"] == 0
        assert metadata["last_packet_at"] is None
        diagnostic = json.dumps(metadata) + client.last_failure_diagnostic
        assert "private provider body" not in diagnostic
        assert "synthetic-secret" not in diagnostic
        assert all("synthetic-secret" not in event.message for event in retries)
        assert all("http" in event.message for event in retries)
        assert "synthetic-secret" not in str(failure.value)
        assert "synthetic-secret" not in caplog.text
        captured = capsys.readouterr()
        assert "synthetic-secret" not in captured.out + captured.err
        # The diagnostic seam changes messages, while callers can still inspect
        # the SDK's original HTTP payload explicitly if they need it.
        assert failure.value.__cause__.body["message"] == (
            "private provider body synthetic-secret"
        )
        assert failure.value.__cause__.response.json()["error"]["message"] == (
            "private provider body synthetic-secret"
        )
        UUID(metadata["request_id"])
        assert metadata["started_at"] <= metadata["end_at"]
    finally:
        await client._sdk_client.close()


@pytest.mark.asyncio
async def test_sdk_invalid_sse_json_is_parser_failure_with_raw_packet_count(monkeypatch):
    client, calls = sdk_client(monkeypatch, ["data: {private invalid synthetic-secret\n\n"])
    try:
        with pytest.raises(RequestFailure):
            _ = [event async for event in client.stream_message(api_request())]
        metadata = client.last_failure_metadata
        assert metadata["category"] == "parser"
        assert "JSONDecodeError" in metadata["exception_chain"]
        assert metadata["raw_chunk_count"] == 1
        assert metadata["raw_chunk_source"] == "sse"
        assert metadata["last_packet_at"] > 0
        assert "synthetic-secret" not in json.dumps(metadata) + client.last_failure_diagnostic
        assert len(calls) == 1
    finally:
        await client._sdk_client.close()


@pytest.mark.asyncio
async def test_sdk_transport_failure_reuses_existing_bounded_retry_loop(monkeypatch):
    client, calls = sdk_client(monkeypatch, [
        httpx.ConnectError("private connection detail synthetic-secret") for _ in range(4)
    ], empty_response_fallback_model="configured-fallback")
    monkeypatch.setattr(upstream, "BASE_DELAY", 0)
    state = QueryRecoveryState()
    try:
        with pytest.raises(RequestFailure):
            await query_request(client, state)
        metadata = client.last_failure_metadata
        assert metadata["category"] == "transport"
        assert metadata["exception_chain"] == [
            "RequestFailure", "APIConnectionError", "ConnectError",
        ]
        assert metadata["provider_attempts"] == 4
        assert metadata["raw_chunk_count"] == 0
        assert len(calls) == 4
        assert {call["model"] for call in calls} == {"primary(max)"}
        assert not state.fallback_active
        assert "synthetic-secret" not in json.dumps(metadata) + client.last_failure_diagnostic
    finally:
        await client._sdk_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [
    "maximum context length exceeded; private synthetic-secret",
    "max_tokens supports at most 4096 completion tokens; private synthetic-secret",
    "max_completion_tokens is too large; private synthetic-secret",
    "max_tokens must be <= 8,192; private synthetic-secret",
])
async def test_sdk_safe_error_preserves_upstream_context_and_completion_cap_semantics(
    monkeypatch, message,
):
    def classifications(error):
        return (_is_prompt_too_long_error(error), _is_completion_token_limit_error(error),
                _extract_completion_token_limit(error))

    expected = classifications(ValueError(message))
    client, calls = sdk_client(monkeypatch, [httpx.Response(400, json={"error": {
        "message": message, "type": "invalid_request_error",
    }})])
    try:
        with pytest.raises(RequestFailure) as failure:
            _ = [event async for event in client.stream_message(api_request())]
        assert classifications(failure.value) == expected
        assert "synthetic-secret" not in str(failure.value) + client.last_failure_diagnostic
        assert "synthetic-secret" not in json.dumps(client.last_failure_metadata)
        assert len(calls) == 1
    finally:
        await client._sdk_client.close()


@pytest.mark.asyncio
async def test_sdk_engine_retries_http_completion_limit_with_lower_request_cap(
    monkeypatch, tmp_path, caplog,
):
    client, calls = sdk_client(monkeypatch, [
        httpx.Response(400, json={"error": {
            "message": "max_tokens supports at most 4096 completion tokens; synthetic-secret",
            "type": "invalid_request_error",
        }}),
        sse_chunk(text="Done after cap recovery", finish="stop", usage=(3, 1)),
    ])
    engine = GeoQueryEngine(
        api_client=client, tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings()),
        cwd=tmp_path, model="primary(max)", system_prompt="Synthetic cap recovery.",
        max_tokens=16384, max_turns=3,
    )
    try:
        events = [event async for event in engine.submit_message("recover the output cap")]
        assert [call["max_tokens"] for call in calls] == [16384, 4096]
        assert [call["model"] for call in calls] == ["primary(max)", "primary(max)"]
        assert engine.messages[-1].text == "Done after cap recovery"
        assert any(isinstance(event, StatusEvent) and "provider limit 4096" in event.message
                   for event in events)
        assert "synthetic-secret" not in caplog.text
        assert engine.total_usage.total_tokens == 4
    finally:
        await client._sdk_client.close()


@pytest.mark.asyncio
async def test_sdk_query_state_isolated_from_fresh_query_and_caps_smaller_request(monkeypatch):
    client, calls = sdk_client(monkeypatch, [
        sse_chunk(reasoning="private", finish="length"),
        sse_chunk(text="Recovered", finish="stop"),
        sse_chunk(text="Fresh", finish="stop"),
        sse_chunk(text="Continued", finish="stop"),
    ], request_extra_body={"reasoning_effort": "high", "metadata": {"token": "synthetic-secret"}},
        empty_response_fallback_model="configured-fallback",
        empty_response_fallback_extra_body={"reasoning_effort": "none"},
        empty_response_max_tokens=4096)
    original = QueryRecoveryState()
    try:
        first = await query_request(client, original)
        assert first[-1].message.text == "Recovered"
        fresh = await query_request(client, QueryRecoveryState())
        assert fresh[-1].message.text == "Fresh"
        continued = await query_request(client, original, replace(api_request(), max_tokens=1024))
        assert continued[-1].message.text == "Continued"
        assert [call["model"] for call in calls] == [
            "primary(max)", "configured-fallback", "primary(max)", "configured-fallback",
        ]
        assert [call["max_tokens"] for call in calls] == [16384, 4096, 16384, 1024]
        assert [call["reasoning_effort"] for call in calls] == ["high", "none", "high", "none"]
        serialized_state = json.dumps(asdict(original), default=lambda value: value.model_dump())
        assert "synthetic-secret" not in serialized_state
        assert "metadata" not in asdict(original)
    finally:
        await client._sdk_client.close()


@pytest.mark.asyncio
async def test_sdk_concurrent_query_states_keep_successful_modes_separate(monkeypatch):
    async def responses(body):
        await asyncio.sleep(0)
        label = body["messages"][0]["content"]
        if label == "A" and body["reasoning_effort"] == "high":
            return sse_chunk(reasoning="private A", finish="length")
        return sse_chunk(text=label, finish="stop")

    client, calls = sdk_client(monkeypatch, responses,
        request_extra_body={"reasoning_effort": "high"},
        empty_response_fallback_extra_body={"reasoning_effort": "none"},
        empty_response_max_tokens=4096)

    async def run(label):
        state = QueryRecoveryState()
        for _ in range(2):
            result = await query_request(client, state, api_request(label))
            assert result[-1].message.text == label
        return state

    try:
        state_a, state_b = await asyncio.gather(run("A"), run("B"))
        assert state_a.fallback_active
        assert not state_b.fallback_active
        calls_a = [call for call in calls if call["messages"][0]["content"] == "A"]
        calls_b = [call for call in calls if call["messages"][0]["content"] == "B"]
        assert [call["reasoning_effort"] for call in calls_a] == ["high", "none", "none"]
        assert [call["max_tokens"] for call in calls_a] == [16384, 4096, 4096]
        assert [call["reasoning_effort"] for call in calls_b] == ["high", "high"]
        assert [call["max_tokens"] for call in calls_b] == [16384, 16384]
    finally:
        await client._sdk_client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["ordinary_empty", "fallback_length", "partial_error", "repair"])
async def test_sdk_query_does_not_activate_fallback_for_incomplete_or_argument_recovery(
    monkeypatch, case,
):
    first = sse_chunk(reasoning="private", finish="length")
    second = sse_chunk(text="Recovered", finish="stop")
    if case == "ordinary_empty":
        first = sse_chunk(finish="stop")
    elif case == "fallback_length":
        second = sse_chunk(text="Partial", finish="length")
    elif case == "partial_error":
        second = sse_chunk(text="Partial", arguments='{"code":') + (
            'data: {"error":{"message":"private synthetic-secret"}}\n\n'
        )
    elif case == "repair":
        first = sse_chunk(arguments='{"code":"truncated', finish="length")
        second = sse_chunk(arguments=json.dumps({"code": "repaired"}),
                           tool_name="geoharness_repair_0", finish="tool_calls")
    client, calls = sdk_client(monkeypatch, [first, second, sse_chunk(text="Next", finish="stop")],
        empty_response_fallback_model="configured-fallback", empty_response_max_tokens=4096)
    state = QueryRecoveryState()
    try:
        result = await query_request(client, state)
        assert not state.fallback_active
        assert len(calls) == 2
        if case == "partial_error":
            assert result[-1].message.text == "Partial"
            assert not result[-1].message.tool_uses
            assert result[-1].stop_reason == "transport_error"
            assert "provider_sse" in client.last_response_diagnostic
            assert client.last_failure_metadata["request_id"] in client.last_response_diagnostic
        if case == "repair":
            assert result[-1].message.tool_uses[0].input == {"code": "repaired"}
        next_result = await query_request(client, state)
        assert next_result[-1].message.text == "Next"
        assert calls[-1]["model"] == "primary(max)"
        assert calls[-1]["max_tokens"] == 16384
    finally:
        await client._sdk_client.close()


@pytest.mark.asyncio
async def test_sdk_cross_task_query_close_settles_usage_once_and_keeps_other_query_open(
    monkeypatch, tmp_path,
):
    class OpenSSE(httpx.AsyncByteStream):
        def __init__(self, label, usage):
            self.label = label
            self.usage = usage
            self.close_calls = 0

        async def __aiter__(self):
            yield sse_chunk(text=self.label, usage=self.usage).encode()
            await asyncio.Event().wait()

        async def aclose(self):
            self.close_calls += 1

    bodies = {"A": OpenSSE("A", (5, 1)), "B": OpenSSE("B", (7, 2))}

    async def responses(body):
        label = body["messages"][-1]["content"]
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=bodies[label])

    client, calls = sdk_client(monkeypatch, responses)
    engines = [GeoQueryEngine(
        api_client=client, tool_registry=ToolRegistry(),
        permission_checker=PermissionChecker(settings=PermissionSettings()),
        cwd=tmp_path, model="primary(max)", system_prompt="Synthetic unfinished response.",
        max_tokens=16384, max_turns=3,
    ) for _ in range(2)]
    streams = [engine.submit_message(label) for engine, label in zip(engines, ("A", "B"))]

    async def first_text(stream):
        while True:
            event = await anext(stream)
            if isinstance(event, AssistantTextDelta):
                return event.text

    try:
        texts = await asyncio.gather(
            *(asyncio.create_task(first_text(stream)) for stream in streams)
        )
        assert texts == ["A", "B"]
        await asyncio.create_task(streams[0].aclose())
        assert engines[0].total_usage.input_tokens == 5
        assert engines[0].total_usage.output_tokens == 1
        assert bodies["A"].close_calls == 1  # Released before aclose returns, without loop ticks.
        assert engines[1].total_usage.total_tokens == 0
        assert bodies["B"].close_calls == 0
        await asyncio.create_task(streams[1].aclose())
        assert engines[1].total_usage.input_tokens == 7
        assert engines[1].total_usage.output_tokens == 2
        assert bodies["B"].close_calls == 1
        await asyncio.gather(*(stream.aclose() for stream in streams))
        for _ in range(3):
            await asyncio.sleep(0)
        assert [engine.total_usage.total_tokens for engine in engines] == [6, 9]
        assert [body.close_calls for body in bodies.values()] == [1, 1]
        assert len(calls) == 2
        assert client.consume_unsettled_usage().total_tokens == 0
    finally:
        await asyncio.gather(*(stream.aclose() for stream in streams))
        await client._sdk_client.close()


@pytest.mark.asyncio
async def test_sdk_query_retains_successful_fallback_across_two_tool_turns(monkeypatch, tmp_path):
    client, calls = sdk_client(monkeypatch, [
        sse_chunk(reasoning="private", finish="length", usage=(10, 16384)),
        sse_chunk(arguments=json.dumps({"code": "tool one"}), finish="tool_calls", usage=(12, 5)),
        sse_chunk(arguments=json.dumps({"code": "tool two"}), finish="tool_calls", usage=(14, 5)),
        sse_chunk(text="Done", finish="stop", usage=(16, 2)),
    ], request_extra_body={"reasoning_effort": "high"},
        empty_response_fallback_model="configured-fallback",
        empty_response_fallback_extra_body={"reasoning_effort": "none"},
        empty_response_max_tokens=4096)
    registry = ToolRegistry()
    tool = RecordingPythonTool()
    registry.register(tool)
    engine = GeoQueryEngine(
        api_client=client, tool_registry=registry,
        permission_checker=PermissionChecker(settings=PermissionSettings()),
        cwd=tmp_path, model="primary(max)", system_prompt="Synthetic tool execution.",
        max_tokens=16384, max_turns=5,
    )
    try:
        _ = [event async for event in engine.submit_message("two tool turns")]
        assert tool.calls == ["tool one", "tool two"]
        assert engine.messages[-1].text == "Done"
        modes = [(call["model"], call["reasoning_effort"], call["max_tokens"]) for call in calls]
        assert modes == [
            ("primary(max)", "high", 16384),
            ("configured-fallback", "none", 4096),
            ("configured-fallback", "none", 4096),
            ("configured-fallback", "none", 4096),
        ]
        assert engine.total_usage.total_tokens == 16448
        assert client.consume_unsettled_usage().total_tokens == 0
    finally:
        await client._sdk_client.close()
