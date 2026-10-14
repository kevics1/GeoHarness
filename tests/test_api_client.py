"""Exercise the real upstream streaming parser with synthetic SDK chunks."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from openharness.api.client import ApiMessageCompleteEvent, ApiMessageRequest, ApiRetryEvent
from openharness.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
from pydantic import ValidationError

from geoharness.api_client import GeoOpenAICompatibleClient
from geoharness.config.settings import AgentConfig, GeoConfig
from geoharness.runtime import _build_api_client
from geoharness.tools.geo_python import GeoPythonInput


def chunk(*, text=None, reasoning=None, finish=None, usage=None, tool_calls=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, reasoning_content=reasoning, tool_calls=tool_calls),
            finish_reason=finish,
        )],
        usage=(
            SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None
        ),
    )


async def streamed(*chunks):
    for item in chunks:
        if isinstance(item, Exception):
            raise item
        yield item


def request():
    return ApiMessageRequest(
        model="test-model(max)", messages=[ConversationMessage(
            role="user", content=[TextBlock(text="Inspect this workspace")],
        )], max_tokens=16384,
        tools=[{"name": "geo_python", "description": "Run code", "input_schema": {
            "type": "object", "properties": {"code": {"type": "string"}},
        }}],
    )


def mock_client(monkeypatch, responses, **settings):
    client = GeoOpenAICompatibleClient("test-only", settings=AgentConfig(**settings))
    calls = []

    async def create(**params):
        calls.append(params)
        return responses[len(calls) - 1]

    monkeypatch.setattr(client._sdk_client.chat.completions, "create", create)
    return client, calls


@pytest.mark.asyncio
async def test_verified_data_context_refreshes_without_changing_saved_messages(monkeypatch):
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(text="one", finish="stop")),
        streamed(chunk(text="two", finish="stop")),
    ])
    snapshots = iter(["\nRaster CRS EPSG:32647", "\nActual output /runs/prior/result.geojson"])
    client.data_context_provider = lambda: next(snapshots)
    original = replace(request(), system_prompt="Base contract")
    _ = [event async for event in client.stream_message(original)]
    events = [event async for event in client.stream_message(original)]
    assert events[-1].message.text == "two"
    assert "32647" in calls[0]["messages"][0]["content"]
    assert "prior/result" in calls[1]["messages"][0]["content"]
    assert "32647" not in calls[1]["messages"][0]["content"]
    assert original.system_prompt == "Base contract"
    assert len(original.messages) == 1


@pytest.mark.asyncio
async def test_reasoning_only_length_is_diagnosed_without_implicit_model_switch(monkeypatch):
    client, calls = mock_client(monkeypatch, [streamed(
        chunk(reasoning="hidden reasoning", finish="length", usage=(37, 16384)),
    )])
    events = [event async for event in client.stream_message(request())]
    assert len(calls) == 1
    assert calls[0]["model"] == request().model
    assert not any(isinstance(event, ApiRetryEvent) for event in events)
    assert len(events) == 1
    assert events[0].message.is_effectively_empty()
    assert events[0].usage.output_tokens == 16384
    diagnostic = client.last_empty_response_diagnostic
    assert "finish_reason=length" in diagnostic
    assert "输出预算耗尽" in diagnostic
    assert "reasoning_chars=16" in diagnostic
    assert "hidden reasoning" not in diagnostic


@pytest.mark.asyncio
async def test_configured_recovery_keeps_context_and_totals_completed_requests(monkeypatch):
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(reasoning="hidden", finish="length", usage=(20, 16384))),
        streamed(chunk(text="Recovered", finish="stop", usage=(25, 3))),
    ], empty_response_fallback_model="test-model(none)", empty_response_max_tokens=4096)
    events = [event async for event in client.stream_message(request())]
    finals = [event for event in events if isinstance(event, ApiMessageCompleteEvent)]
    assert len(finals) == 1
    assert finals[0].message.text == "Recovered"
    assert finals[0].usage.input_tokens == 45
    assert finals[0].usage.output_tokens == 16387
    retries = [event for event in events if isinstance(event, ApiRetryEvent)]
    assert len(retries) == 1
    assert "test-model(none)" in retries[0].message
    assert "finish_reason=length" in retries[0].message
    assert calls[1]["model"] == "test-model(none)"
    assert calls[1]["messages"] == calls[0]["messages"]
    assert calls[1]["tools"] == calls[0]["tools"]
    assert calls[1]["max_tokens"] == 4096
    assert request().model == "test-model(max)"
    assert client.last_empty_response_diagnostic is None


@pytest.mark.asyncio
async def test_persistent_empty_stops_after_one_recovery_with_total_usage(monkeypatch):
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(reasoning="one", finish="length", usage=(10, 100))),
        streamed(chunk(finish="stop", usage=(11, 0))),
    ], empty_response_fallback_model="configured-alternative")
    events = [event async for event in client.stream_message(request())]
    final = events[-1]
    assert len(calls) == 2
    assert final.message.is_effectively_empty()
    assert final.usage.total_tokens == 121
    assert "finish_reason=stop" in client.last_empty_response_diagnostic
    assert "request_attempts=2" in client.last_empty_response_diagnostic
    assert "这不表示先前的工具输出为空" in client.last_empty_response_diagnostic


@pytest.mark.asyncio
async def test_streamed_visible_text_never_triggers_empty_recovery(monkeypatch):
    client, calls = mock_client(monkeypatch, [streamed(
        chunk(text="Answer", reasoning="analysis", finish="length", usage=(10, 16384)),
    )], empty_response_fallback_model="configured-alternative")
    events = [event async for event in client.stream_message(request())]
    assert len(calls) == 1
    assert events[-1].message.text == "Answer"


@pytest.mark.asyncio
async def test_real_parser_keeps_split_tool_arguments_and_reasoning(monkeypatch):
    def tool(arguments, *, name=None, tool_id=None):
        return SimpleNamespace(index=0, id=tool_id, function=SimpleNamespace(
            name=name, arguments=arguments,
        ))

    client, calls = mock_client(monkeypatch, [streamed(
        chunk(reasoning="private reasoning", tool_calls=[tool(
            '{"code": "print(', name="geo_python", tool_id="call-1",
        )]),
        chunk(tool_calls=[tool('1)"}')], finish="tool_calls", usage=(20, 40)),
    )], request_extra_body={"reasoning_effort": "low"})
    events = [event async for event in client.stream_message(request())]
    final = events[-1]
    tools = [block for block in final.message.content if isinstance(block, ToolUseBlock)]
    assert tools[0].input == {"code": "print(1)"}
    assert tools[0].id == "call-1"
    assert final.message._reasoning == "private reasoning"
    assert calls[0]["extra_body"] == {"reasoning_effort": "low"}


@pytest.mark.asyncio
async def test_extensions_are_task_local_and_recovery_never_increases_budget(monkeypatch):
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(finish="stop", usage=(1, 0))),
        streamed(chunk(text="ok", finish="stop", usage=(2, 1))),
    ], request_extra_body={"reasoning_effort": "high"},
        empty_response_fallback_extra_body={"reasoning_effort": "none"},
        empty_response_max_tokens=65536)
    partial = client.stream_message(request())
    await anext(partial)  # Retry is the first emitted event.
    await partial.aclose()
    # A fresh request must use its main extras, even after a prematurely closed retry loop.
    events = [event async for event in client.stream_message(request())]
    assert events[-1].message.text == "ok"
    assert calls[-1]["extra_body"] == {"reasoning_effort": "high"}
    assert calls[-1]["max_tokens"] == 16384


@pytest.mark.asyncio
async def test_transport_failure_after_visible_text_does_not_repeat_stream(monkeypatch):
    client, calls = mock_client(monkeypatch, [streamed(chunk(text="Partial"), OSError("broken"))])
    events = [event async for event in client.stream_message(request())]
    assert len(calls) == 1
    assert events[-1].message.text == "Partial"
    assert events[-1].stop_reason == "transport_error"
    assert "回答尚未完成" in client.last_response_diagnostic
    assert client.last_empty_response_diagnostic is None


@pytest.mark.asyncio
async def test_recovery_transport_failure_retains_first_response_usage(monkeypatch):
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(reasoning="hidden", finish="length", usage=(23, 16384))),
        streamed(ValueError("bad request")),
    ], empty_response_fallback_model="configured-alternative")
    events = [event async for event in client.stream_message(request())]
    assert len(calls) == 2
    assert events[-1].message.is_effectively_empty()
    assert events[-1].usage.input_tokens == 23
    assert events[-1].usage.output_tokens == 16384
    assert "恢复请求失败（RequestFailure）" in client.last_empty_response_diagnostic


@pytest.mark.asyncio
async def test_recovery_cap_cannot_enlarge_main_budget(monkeypatch):
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(finish="stop", usage=(1, 0))),
        streamed(chunk(text="ok", finish="stop", usage=(2, 1))),
    ], empty_response_fallback_model="configured-alternative", empty_response_max_tokens=65536)
    _ = [event async for event in client.stream_message(request())]
    assert calls[1]["max_tokens"] == 16384


@pytest.mark.asyncio
async def test_concurrent_requests_keep_extensions_and_diagnostics_separate(monkeypatch):
    client = GeoOpenAICompatibleClient("test-only", settings=AgentConfig(
        request_extra_body={"reasoning_effort": "high"},
        empty_response_fallback_extra_body={"reasoning_effort": "none"},
    ))
    calls = []

    async def create(**params):
        calls.append(params)
        await asyncio.sleep(0)
        if params["messages"][0]["content"] == "A":
            return streamed(chunk(finish="stop", usage=(1, 0)))
        return streamed(chunk(text="B", finish="stop", usage=(2, 1)))

    monkeypatch.setattr(client._sdk_client.chat.completions, "create", create)

    async def run(label):
        message = ConversationMessage(role="user", content=[TextBlock(text=label)])
        events = [event async for event in client.stream_message(replace(
            request(), messages=[message],
        ))]
        return events[-1], client.last_empty_response_diagnostic

    result_a, result_b = await asyncio.gather(run("A"), run("B"))
    assert result_a[0].message.is_effectively_empty()
    assert "request_attempts=2" in result_a[1]
    assert result_b[0].message.text == "B"
    assert result_b[1] is None
    calls_b = [call for call in calls if call["messages"][0]["content"] == "B"]
    assert calls_b[0]["extra_body"] == {"reasoning_effort": "high"}
    assert client.last_empty_response_diagnostic is None


def test_agent_extensions_cannot_override_model_or_tool_context():
    for key in ("model", "messages", "tools", "max_tokens", "stream"):
        with pytest.raises(ValidationError):
            AgentConfig(request_extra_body={key: "silent-override"})
    with pytest.raises(ValidationError):
        AgentConfig(empty_response_retries=2)


def test_runtime_passes_agent_settings_into_local_transport():
    settings = AgentConfig(empty_response_fallback_model="configured-alternative")
    client = _build_api_client(GeoConfig(api_key="test-only", agent=settings))
    assert isinstance(client, GeoOpenAICompatibleClient)
    assert client.settings is settings


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("has_tools", [True, False])
async def test_stream_usage_option_is_explicit_even_with_tools(monkeypatch, enabled, has_tools):
    client, calls = mock_client(monkeypatch, [streamed(chunk(text="ok", finish="stop"))],
                                include_stream_usage=enabled)
    req = request() if has_tools else replace(request(), tools=[])
    _ = [event async for event in client.stream_message(req)]
    if enabled:
        assert calls[0]["stream_options"] == {"include_usage": True}
    else:
        assert "stream_options" not in calls[0]


@pytest.mark.asyncio
async def test_fallback_partial_error_keeps_text_and_drops_incomplete_tool_calls(monkeypatch):
    partial_tool = SimpleNamespace(index=0, id="partial-call", function=SimpleNamespace(
        name="geo_python", arguments='{"code": "print(',
    ))
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(reasoning="analysis", finish="length", usage=(23, 16384))),
        streamed(chunk(text="Partial answer", tool_calls=[partial_tool]), OSError("broken")),
    ], empty_response_fallback_model="configured-alternative")
    events = [event async for event in client.stream_message(request())]
    final = events[-1]
    assert len(calls) == 2
    assert final.message.text == "Partial answer"
    assert not final.message.tool_uses
    assert final.stop_reason == "transport_error"
    assert final.usage.total_tokens == 16407
    assert "回答尚未完成" in client.last_response_diagnostic
    assert client.last_empty_response_diagnostic is None


@pytest.mark.asyncio
async def test_empty_response_usage_survives_closing_at_retry_event(monkeypatch):
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(reasoning="analysis", finish="length", usage=(23, 16384))),
    ], empty_response_fallback_model="configured-alternative")
    stream = client.stream_message(request())
    retry = await anext(stream)
    assert isinstance(retry, ApiRetryEvent)
    await stream.aclose()
    assert len(calls) == 1
    pending = client.consume_unsettled_usage()
    assert pending.input_tokens == 23
    assert pending.output_tokens == 16384
    assert client.consume_unsettled_usage().total_tokens == 0


@pytest.mark.asyncio
async def test_cancellation_during_fallback_keeps_known_usage(monkeypatch):
    started = asyncio.Event()
    pending = []

    async def blocked():
        started.set()
        await asyncio.Event().wait()
        yield chunk(text="unreachable")

    client, calls = mock_client(monkeypatch, [
        streamed(chunk(reasoning="analysis", finish="length", usage=(23, 16384))),
        blocked(),
    ], empty_response_fallback_model="configured-alternative")

    async def run():
        try:
            _ = [event async for event in client.stream_message(request())]
        finally:
            pending.append(client.consume_unsettled_usage())

    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 2
    assert pending[0].input_tokens == 23
    assert pending[0].output_tokens == 16384


@pytest.mark.asyncio
async def test_acknowledged_completion_is_not_charged_twice(monkeypatch):
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(reasoning="analysis", finish="length", usage=(23, 16384))),
        streamed(chunk(text="Answer", finish="stop", usage=(24, 5))),
    ], empty_response_fallback_model="configured-alternative")
    events = [event async for event in client.stream_message(request())]
    client.ack_usage(events[-1].usage)
    assert client.consume_unsettled_usage().total_tokens == 0
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_repeated_cumulative_usage_chunks_are_not_double_counted(monkeypatch):
    client, calls = mock_client(monkeypatch, [streamed(
        chunk(text="A", usage=(12, 3)), chunk(text="B", usage=(12, 5)),
        chunk(finish="stop", usage=(12, 5)),
    )])
    events = [event async for event in client.stream_message(request())]
    assert events[-1].usage.input_tokens == 12
    assert events[-1].usage.output_tokens == 5
    pending = client.consume_unsettled_usage()
    assert pending.input_tokens == 12
    assert pending.output_tokens == 5
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_retry_generator_can_be_closed_from_another_task(monkeypatch):
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(reasoning="analysis", finish="length", usage=(23, 16384))),
    ], empty_response_fallback_model="configured-alternative")
    stream = client.stream_message(request())
    assert isinstance(await anext(stream), ApiRetryEvent)
    # asyncio finalizes abandoned generators in a new task after a UI loop exits.
    await asyncio.create_task(stream.aclose())
    assert len(calls) == 1
    assert client._request_state.get() is None


@pytest.mark.asyncio
async def test_retry_generator_resumes_in_a_new_context_without_losing_recovery(monkeypatch):
    client, calls = mock_client(monkeypatch, [
        streamed(chunk(reasoning="analysis", finish="length", usage=(23, 16384))),
        streamed(chunk(text="Recovered", finish="stop", usage=(24, 5))),
    ], empty_response_fallback_model="configured-alternative")
    stream = client.stream_message(request())
    assert isinstance(await asyncio.create_task(anext(stream)), ApiRetryEvent)
    events = [event async for event in stream]
    assert events[-1].message.text == "Recovered"
    assert len(calls) == 2
    assert client._request_state.get() is None


@pytest.mark.asyncio
async def test_closing_partial_text_in_another_task_closes_sdk_transport_immediately(monkeypatch):
    class Transport:
        close_calls = 0

        def __init__(self):
            self.chunks = iter([chunk(text="Partial answer"), chunk(finish="stop")])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.chunks)
            except StopIteration:
                raise StopAsyncIteration from None

        async def close(self):
            self.close_calls += 1

    transport = Transport()
    client, calls = mock_client(monkeypatch, [transport])
    stream = client.stream_message(request())
    assert (await anext(stream)).text == "Partial answer"
    assert client._request_state.get() is None
    await asyncio.create_task(stream.aclose())
    assert transport.close_calls == 1
    # Nested upstream finalizers must not close a second time after local cleanup.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert transport.close_calls == 1 and len(calls) == 1


def tool_delta(arguments, *, index=0, name="geo_python", tool_id="call-invalid"):
    return SimpleNamespace(index=index, id=tool_id, function=SimpleNamespace(
        name=name, arguments=arguments,
    ))


@pytest.mark.asyncio
async def test_truncated_call_recovers_once_without_repeating_valid_sibling(monkeypatch):
    client, calls = mock_client(monkeypatch, [streamed(chunk(
        text="Checking data", tool_calls=[tool_delta('{"code":"print('),
            tool_delta('{"code":"print(1)"}', index=1, tool_id="call-valid")],
        finish="length", usage=(12, 16384))), streamed(chunk(
        text="Discard repair narration", tool_calls=[tool_delta('{"code":"print(2)"}',
            name="geoharness_repair_0", tool_id="repair-new-id")],
        finish="tool_calls", usage=(20, 60)))],
        empty_response_fallback_model="configured-alternative")
    events = [event async for event in client.stream_message(request())]
    final = events[-1]
    assert len(calls) == 2
    assert calls[1]["model"] == "configured-alternative"
    assert [tool["function"]["name"] for tool in calls[1]["tools"]] == ["geoharness_repair_0"]
    assert [(tool.id, tool.input) for tool in final.message.tool_uses] == [
        ("call-invalid", {"code": "print(2)"}), ("call-valid", {"code": "print(1)"})]
    assert final.message.text == "Checking data"
    assert final.usage.total_tokens == 16476
    assert client.last_tool_argument_diagnostics[0]["recovered"] is True
    assert any(isinstance(event, ApiRetryEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["invalid", "transport", "unexpected", "duplicate", "blank"])
async def test_failed_argument_recovery_stays_bounded_and_retains_original_calls(
    monkeypatch, second,
):
    arguments = ('{"code":"print(' if second == "invalid" else
                 '{"code":" "}' if second == "blank" else '{"code":"print(9)"}')
    name = "unrelated_tool" if second == "unexpected" else "geoharness_repair_0"
    repair_calls = [tool_delta(arguments, name=name)]
    if second == "duplicate":
        repair_calls.append(tool_delta(arguments, name=name, index=1, tool_id="duplicate"))
    recovery = (streamed(RuntimeError("transport broke")) if second == "transport" else
        streamed(chunk(tool_calls=repair_calls,
            finish="length", usage=(5, 10))))
    client, calls = mock_client(monkeypatch, [streamed(chunk(
        tool_calls=[tool_delta('{"code":"print('),
            tool_delta('{"code":"print(1)"}', index=1, tool_id="call-valid")],
        finish="length", usage=(10, 20))), recovery])
    final = [event async for event in client.stream_message(request())][-1]
    assert len(calls) == 2
    assert "_geoharness_argument_error" in final.message.tool_uses[0].input
    assert final.message.tool_uses[1].input == {"code": "print(1)"}
    assert final.usage.total_tokens == (30 if second == "transport" else 45)


@pytest.mark.asyncio
@pytest.mark.parametrize(("arguments", "category", "finish"), [
    ('{"code":"print(', "invalid_json", "length"),
    ("{}", "missing_required_arguments", "tool_calls"),
    ("[]", "non_object_json", "tool_calls"),
    ("null", "non_object_json", "tool_calls"),
])
async def test_invalid_tool_arguments_are_rejected_before_tools_can_execute(
    monkeypatch, arguments, category, finish,
):
    client, calls = mock_client(monkeypatch, [streamed(chunk(
        tool_calls=[tool_delta(arguments)], finish=finish, usage=(12, 16384),
    ))], empty_response_fallback_model="configured-alternative", tool_argument_retries=0)
    events = [event async for event in client.stream_message(request())]
    final = events[-1]
    assert isinstance(final, ApiMessageCompleteEvent)
    assert len(final.message.tool_uses) == 1
    with pytest.raises(ValidationError, match="no script was executed"):
        GeoPythonInput.model_validate(final.message.tool_uses[0].input)
    assert category in client.last_response_diagnostic
    assert f"argument_chars={len(arguments)}" in client.last_response_diagnostic
    assert f"finish_reason={finish}" in client.last_response_diagnostic
    assert "print(" not in client.last_response_diagnostic
    assert final.usage.total_tokens == 16396
    assert client.last_empty_response_diagnostic is None
    assert len(calls) == 1
    assert not any(isinstance(event, ApiRetryEvent) for event in events)


@pytest.mark.asyncio
async def test_rejecting_invalid_parallel_call_keeps_valid_calls_and_streamed_text(monkeypatch):
    client, calls = mock_client(monkeypatch, [streamed(
        chunk(text="Checking ", tool_calls=[tool_delta(
            '{"code":"private incomplete script',
        )]),
        chunk(text="data", tool_calls=[tool_delta(
            json.dumps({"code": "print(1)"}), index=1, tool_id="call-valid",
        )], finish="length", usage=(12, 40)),
    )], tool_argument_retries=0)
    events = [event async for event in client.stream_message(request())]
    assert [event.text for event in events[:-1]] == ["Checking ", "data"]
    assert len(events[-1].message.tool_uses) == 2
    invalid, valid = events[-1].message.tool_uses
    with pytest.raises(ValidationError, match="invalid_json"):
        GeoPythonInput.model_validate(invalid.input)
    assert valid.id == "call-valid"
    assert valid.input == {"code": "print(1)"}
    assert "private incomplete script" not in events[-1].message.text
    assert events[-1].usage.total_tokens == 52
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_zero_argument_tool_keeps_explicit_empty_object(monkeypatch):
    client, calls = mock_client(monkeypatch, [streamed(chunk(
        tool_calls=[tool_delta("{}", name="doctor")], finish="tool_calls",
    ))])
    req = replace(request(), tools=[{
        "name": "doctor", "description": "Check environment", "input_schema": {
            "type": "object", "properties": {},
        },
    }])
    events = [event async for event in client.stream_message(req)]
    assert events[-1].message.tool_uses[0].input == {}
    assert client.last_response_diagnostic is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_complete_arguments_at_budget_limit_remain_executable(monkeypatch):
    client, calls = mock_client(monkeypatch, [streamed(chunk(
        reasoning="private reasoning", tool_calls=[tool_delta('{"code":"print(1)"}')],
        finish="length", usage=(4, 16384),
    ))])
    events = [event async for event in client.stream_message(request())]
    final = events[-1]
    assert final.message.tool_uses[0].input == {"code": "print(1)"}
    assert final.message._reasoning == "private reasoning"
    assert client.last_tool_argument_diagnostics == []
    assert client.last_response_diagnostic is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_non_object_arguments_keep_complete_sibling_and_private_reasoning(monkeypatch):
    client, calls = mock_client(monkeypatch, [streamed(chunk(
        text="Checking", reasoning="private", tool_calls=[
            tool_delta("[]"),
            tool_delta('{"code":"print(1)"}', index=1, tool_id="call-valid"),
        ], finish="tool_calls", usage=(3, 5),
    ))])
    events = [event async for event in client.stream_message(request())]
    invalid, valid = events[-1].message.tool_uses
    assert invalid.input["_geoharness_argument_error"]["category"] == "non_object_json"
    assert valid.input == {"code": "print(1)"}
    assert events[-1].message.text == "Checking"
    assert events[-1].message._reasoning == "private"
    assert "private" not in json.dumps(client.last_tool_argument_diagnostics)
    assert events[-1].usage.total_tokens == 8
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_invalid_tool_rejection_closes_provider_once_and_settles_usage(monkeypatch):
    class Transport:
        close_calls = 0

        def __init__(self):
            self.chunks = iter([chunk(
                tool_calls=[tool_delta("{}")], finish="tool_calls", usage=(4, 2),
            )])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.chunks)
            except StopIteration:
                raise StopAsyncIteration from None

        async def close(self):
            self.close_calls += 1

    transport = Transport()
    client, calls = mock_client(monkeypatch, [transport])
    events = [event async for event in client.stream_message(request())]
    with pytest.raises(ValidationError, match="missing_required_arguments"):
        GeoPythonInput.model_validate(events[-1].message.tool_uses[0].input)
    assert transport.close_calls == 1
    client.ack_usage(events[-1].usage)
    assert client.consume_unsettled_usage().total_tokens == 0
    assert client._request_state.get() is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_invalid_arguments_continue_through_error_result_without_repeating_valid_tools(
    monkeypatch, tmp_path,
):
    from openharness.config.settings import PermissionSettings
    from openharness.engine.messages import ToolResultBlock
    from openharness.permissions.checker import PermissionChecker
    from openharness.tools.base import ToolRegistry, ToolResult

    from geoharness.engine import GeoQueryEngine
    from geoharness.tools.geo_python import GeoPythonTool

    class RecordingPythonTool(GeoPythonTool):
        calls = []

        async def execute(self, arguments, context):
            self.calls.append(arguments.code)
            return ToolResult(output="completed synthetic execution")

        def is_read_only(self, arguments):
            return True

    client, calls = mock_client(monkeypatch, [
        streamed(chunk(tool_calls=[
            tool_delta('{"code":"cut off'),
            tool_delta(json.dumps({"code": "valid once"}), index=1, tool_id="call-valid"),
        ], finish="length", usage=(12, 40))),
        streamed(chunk(tool_calls=[tool_delta(
            json.dumps({"code": "repaired"}), tool_id="call-repaired", name="geoharness_repair_0",
        )], finish="tool_calls", usage=(18, 5))),
        streamed(chunk(text="Done", finish="stop", usage=(20, 2))),
    ], empty_response_fallback_model="configured-alternative")
    registry = ToolRegistry()
    tool = RecordingPythonTool()
    registry.register(tool)
    engine = GeoQueryEngine(
        api_client=client, tool_registry=registry,
        permission_checker=PermissionChecker(settings=PermissionSettings()),
        cwd=tmp_path, model="test-model", system_prompt="Repair only rejected arguments.",
        max_turns=4,
    )
    _ = [event async for event in engine.submit_message("make a map")]
    assert tool.calls == ["repaired", "valid once"]
    assert len(calls) == 3
    results = [block for message in engine.messages for block in message.content
               if isinstance(block, ToolResultBlock)]
    invalid = next(block for block in results if block.tool_use_id == "call-invalid")
    assert not invalid.is_error
    assert invalid.content == "completed synthetic execution"
    assert "cut off" not in invalid.content
    assert engine.messages[-1].text == "Done"
    assert engine.total_usage.total_tokens == 97
    assert client.consume_unsettled_usage().total_tokens == 0
