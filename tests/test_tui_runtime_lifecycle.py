"""Exercise the real React backend/handle_line consumer with synthetic SDK streams."""

from __future__ import annotations

import asyncio
import gc
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from openharness.config.settings import PermissionSettings, Settings
from openharness.mcp.client import McpClientManager
from openharness.permissions.checker import PermissionChecker
from openharness.state.app_state import AppState
from openharness.state.store import AppStateStore
from openharness.tools.base import ToolRegistry
from openharness.ui.backend_host import BackendHostConfig
from openharness.ui.runtime import RuntimeBundle

from geoharness.api_client import GeoOpenAICompatibleClient
from geoharness.config.settings import AgentConfig
from geoharness.engine import GeoQueryEngine
from geoharness.runtime import _build_commands
from geoharness.tui_backend import _geo_backend_host_class


def _chunk(*, text=None, reasoning=None, finish=None, usage=None):
    return SimpleNamespace(
        choices=[SimpleNamespace(
            delta=SimpleNamespace(content=text, reasoning_content=reasoning, tool_calls=None),
            finish_reason=finish,
        )],
        usage=(
            SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]) if usage else None
        ),
    )


async def _stream(*chunks):
    for chunk in chunks:
        if isinstance(chunk, Exception):
            raise chunk
        yield chunk


@pytest.fixture
def lifecycle_host(tmp_path, monkeypatch):
    from openharness.ui import runtime

    # Keep global configuration, credentials and session files outside this seam.
    settings = Settings(model="test-model(max)", api_key="synthetic", api_format="openai")
    monkeypatch.setattr(RuntimeBundle, "current_settings", lambda self: settings)
    monkeypatch.setattr(RuntimeBundle, "current_plugins", lambda self: [])
    monkeypatch.setattr(RuntimeBundle, "hook_summary", lambda self: "No hooks")
    monkeypatch.setattr(runtime, "build_runtime_system_prompt", lambda *args, **kwargs: "test")
    monkeypatch.setattr(runtime, "sync_app_state", lambda bundle: None)
    client = GeoOpenAICompatibleClient("synthetic", settings=AgentConfig(
        empty_response_fallback_model="test-model(none)",
    ))
    registry = ToolRegistry()
    engine = GeoQueryEngine(
        api_client=client, tool_registry=registry,
        permission_checker=PermissionChecker(settings=PermissionSettings()),
        cwd=tmp_path, model=settings.model, system_prompt="test",
    )
    bundle = RuntimeBundle(
        api_client=client, cwd=str(tmp_path), mcp_manager=McpClientManager({}),
        tool_registry=registry, app_state=AppStateStore(initial_state=AppState(
            cwd=str(tmp_path), model=settings.model, permission_mode="default", theme="dark",
        )),
        hook_executor=MagicMock(), engine=engine, commands=_build_commands(),
        external_api_client=True, enforce_max_turns=False,
        session_backend=MagicMock(), include_project_memory=False,
    )
    host = _geo_backend_host_class()(BackendHostConfig(cwd=str(tmp_path)))
    host._bundle = bundle
    return host, client, engine


def _responses(monkeypatch, client, *responses):
    calls = []

    async def create(**params):
        calls.append(params)
        return responses[len(calls) - 1]

    monkeypatch.setattr(client._sdk_client.chat.completions, "create", create)
    return calls


def _events(capsys):
    return [json.loads(line.removeprefix("OHJSON:"))
            for line in capsys.readouterr().out.splitlines() if line.startswith("OHJSON:")]


async def _flush_finalizers():
    gc.collect()
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_react_handle_line_renders_empty_recovery_then_completes(
    lifecycle_host, monkeypatch, capsys,
):
    host, client, engine = lifecycle_host
    calls = _responses(monkeypatch, client,
        _stream(_chunk(reasoning="hidden", finish="length", usage=(12, 100))),
        _stream(_chunk(text="Recovered answer", finish="stop", usage=(13, 3))),
    )
    assert await host._run_active_request(host._process_line("test request"))
    events = _events(capsys)
    assert len(calls) == 2
    assert [event["message"] for event in events if event["type"] == "assistant_complete"] == [
        "Recovered answer",
    ]
    assert events[-1]["type"] == "line_complete"
    assert not any(event["type"] == "error" for event in events)
    assert engine.total_usage.total_tokens == 128
    await client._sdk_client.close()


async def test_react_handle_line_recovery_failure_has_one_error_and_completes(
    lifecycle_host, monkeypatch, capsys,
):
    host, client, engine = lifecycle_host
    _responses(monkeypatch, client,
        _stream(_chunk(reasoning="hidden", finish="length", usage=(12, 100))),
        _stream(ValueError("synthetic recovery rejection")),
    )
    assert await host._run_active_request(host._process_line("test request"))
    events = _events(capsys)
    errors = [event for event in events if event["type"] == "error"]
    assert len(errors) == 1 and "恢复请求失败" in errors[0]["message"]
    assert not any(event["type"] == "transcript_item"
                   and event["item"]["text"] == errors[0]["message"] for event in events)
    assert events[-1]["type"] == "line_complete"
    assert engine.total_usage.total_tokens == 112
    await client._sdk_client.close()


async def test_react_interrupt_during_recovery_accepts_next_submission_without_background_error(
    lifecycle_host, monkeypatch, capsys,
):
    host, client, engine = lifecycle_host
    loop = asyncio.get_running_loop()
    errors = []
    original_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    started = asyncio.Event()

    async def blocked_stream():
        started.set()
        await asyncio.Event().wait()
        yield _chunk(text="unreachable")

    calls = _responses(monkeypatch, client,
        _stream(_chunk(reasoning="hidden", finish="length", usage=(12, 100))),
        blocked_stream(),
        _stream(_chunk(text="Next answer", finish="stop", usage=(10, 2))),
    )
    try:
        task = asyncio.create_task(host._run_active_request(host._process_line("first request")))
        await asyncio.wait_for(started.wait(), timeout=5)
        await host._interrupt_active_request()
        assert await task
        interrupted = _events(capsys)
        assert interrupted[-1]["type"] == "line_complete"
        assert host._active_request_task is None
        assert await host._run_active_request(host._process_line("second request"))
        events = _events(capsys)
        assert any(event["type"] == "assistant_complete" and event["message"] == "Next answer"
                   for event in events)
        assert events[-1]["type"] == "line_complete"
        assert len(calls) == 3 and engine.total_usage.total_tokens == 124
        await _flush_finalizers()
        assert not errors, [str(context.get("exception") or context) for context in errors]
    finally:
        loop.set_exception_handler(original_handler)
        await client._sdk_client.close()


async def test_unexpected_runtime_exception_is_reported_and_next_input_is_accepted(
    lifecycle_host, monkeypatch, capsys,
):
    host, client, engine = lifecycle_host
    _responses(monkeypatch, client,
        _stream(_chunk(text="First answer", finish="stop", usage=(10, 2))),
        _stream(_chunk(text="Next answer", finish="stop", usage=(11, 3))),
    )
    # A persistence failure occurs outside the API/engine exception boundary.
    # Keep the actual backend request driver, status snapshot and handle_line.
    host._bundle.session_backend.save_snapshot.side_effect = [
        RuntimeError("synthetic session persistence failure"), None,
    ]
    try:
        assert await host._run_active_request(host._process_line("first request"))
        first_events = _events(capsys)
        errors = [event for event in first_events if event["type"] == "error"]
        assert len(errors) == 1 and "synthetic session persistence failure" in errors[0]["message"]
        assert first_events[-1]["type"] == "line_complete"
        assert host._active_request_task is None
        assert await host._run_active_request(host._process_line("second request"))
        events = _events(capsys)
        assert any(event["type"] == "assistant_complete" and event["message"] == "Next answer"
                   for event in events)
        assert events[-1]["type"] == "line_complete"
        assert not any(event["type"] == "error" for event in events)
    finally:
        await client._sdk_client.close()


async def test_abandoned_retry_stream_finalizes_in_another_context_without_unhandled_error(
    lifecycle_host, monkeypatch,
):
    from openharness.api.client import ApiMessageRequest, ApiRetryEvent
    from openharness.engine.messages import ConversationMessage

    host, client, engine = lifecycle_host
    loop = asyncio.get_running_loop()
    errors = []
    original_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    _responses(monkeypatch, client,
        _stream(_chunk(reasoning="hidden", finish="length", usage=(12, 100))),
    )
    try:
        stream = client.stream_message(ApiMessageRequest(
            model="test-model(max)", messages=[ConversationMessage.from_user_text("test")],
        ))
        assert isinstance(await anext(stream), ApiRetryEvent)
        # asyncio performs abandoned async-generator cleanup in a finalizer task.
        del stream
        await _flush_finalizers()
        assert not errors, [str(context.get("exception") or context) for context in errors]
    finally:
        loop.set_exception_handler(original_handler)
        await client._sdk_client.close()


async def test_failure_before_model_reply_finishes_line_and_accepts_next_input(
    lifecycle_host, monkeypatch, capsys,
):
    from openharness.ui import runtime

    host, client, engine = lifecycle_host
    calls = _responses(monkeypatch, client,
        _stream(_chunk(text="Next answer", finish="stop", usage=(11, 3))),
    )
    prompts = iter([RuntimeError("synthetic prompt failure"), "test"])

    def build_prompt(*args, **kwargs):
        result = next(prompts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(runtime, "build_runtime_system_prompt", build_prompt)
    try:
        assert await host._run_active_request(host._process_line("first request"))
        first = _events(capsys)
        assert len([event for event in first if event["type"] == "error"]) == 1
        assert first[-1]["type"] == "line_complete" and not calls
        assert await host._run_active_request(host._process_line("second request"))
        second = _events(capsys)
        assert any(event["type"] == "assistant_complete" and event["message"] == "Next answer"
                   for event in second)
        assert second[-1]["type"] == "line_complete" and len(calls) == 1
    finally:
        await client._sdk_client.close()


def test_unexpected_runtime_error_redacts_configured_credentials(lifecycle_host):
    from geoharness.config.settings import DataConfig, GeoConfig, MapReviewConfig, McpServerConfig
    from geoharness.tui_backend import _runtime_error_detail

    host, client, engine = lifecycle_host
    secrets = ["synthetic-primary-key", "synthetic-review-key", "synthetic-dsn", "synthetic-token"]
    engine.tool_metadata["geoharness_config"] = GeoConfig(
        api_key=secrets[0], map_review=MapReviewConfig(api_key=secrets[1]),
        data=DataConfig(postgis_dsn=secrets[2]),
        mcp_servers={"test": McpServerConfig(command="synthetic", env={"TOKEN": secrets[3]})},
    )
    detail = _runtime_error_detail(
        RuntimeError("connection failed " + " ".join(secrets)), host._bundle,
    )
    assert "connection failed" in detail
    assert all(secret not in detail for secret in secrets)
    assert detail.count("[redacted]") == 4


async def test_real_cascade_hook_handles_api_retry_and_allows_first_model_reply(
    lifecycle_host, monkeypatch, capsys,
):
    from geoharness.config.settings import GeoConfig
    from geoharness.runtime import _build_hook_executor

    host, client, engine = lifecycle_host
    hook_executor = _build_hook_executor(
        GeoConfig(model=engine.model), client, Path(host._bundle.cwd),
    )
    host._bundle.hook_executor = hook_executor
    engine._hook_executor = hook_executor
    calls = _responses(monkeypatch, client,
        _stream(_chunk(reasoning="hidden", finish="length", usage=(12, 512))),
        _stream(_chunk(text='{"ok": true}', finish="stop", usage=(13, 4))),
        _stream(_chunk(text="Map request received", finish="stop", usage=(14, 5))),
    )
    try:
        assert await host._run_active_request(host._process_line("根据数据文件夹制图"))
        events = _events(capsys)
        assert not any(event["type"] == "error" for event in events)
        assert any(event["type"] == "assistant_complete"
                   and event["message"] == "Map request received" for event in events)
        assert events[-1]["type"] == "line_complete"
        assert len(calls) == 3
        assert engine.total_usage.total_tokens == 560
    finally:
        await client._sdk_client.close()


async def test_advisory_hook_transport_failure_allows_main_reply(
    lifecycle_host, monkeypatch, capsys,
):
    from geoharness.config.settings import GeoConfig
    from geoharness.runtime import _build_hook_executor

    host, client, engine = lifecycle_host
    engine._hook_executor = _build_hook_executor(
        GeoConfig(model=engine.model), client, Path(host._bundle.cwd),
    )
    calls = _responses(monkeypatch, client,
        _stream(RuntimeError("synthetic hook transport failure")),
        _stream(_chunk(text="Main answer", finish="stop", usage=(14, 5))),
    )
    try:
        assert await host._run_active_request(host._process_line("test request"))
        events = _events(capsys)
        assert not any(event["type"] == "error" for event in events)
        assert any(event["type"] == "assistant_complete" and event["message"] == "Main answer"
                   for event in events)
        assert events[-1]["type"] == "line_complete" and len(calls) == 2
    finally:
        await client._sdk_client.close()


async def test_interrupt_during_real_hook_recovery_settles_known_usage(
    lifecycle_host, monkeypatch, capsys,
):
    from geoharness.config.settings import GeoConfig
    from geoharness.runtime import _build_hook_executor

    host, client, engine = lifecycle_host
    engine._hook_executor = _build_hook_executor(
        GeoConfig(model=engine.model), client, Path(host._bundle.cwd),
    )
    started = asyncio.Event()
    closed = asyncio.Event()

    async def blocked():
        started.set()
        try:
            await asyncio.Event().wait()
            yield _chunk(text="unreachable")
        finally:
            closed.set()

    calls = _responses(monkeypatch, client,
        _stream(_chunk(reasoning="hidden", finish="length", usage=(12, 512))),
        blocked(),
        _stream(_chunk(text='{"ok": true}', finish="stop", usage=(13, 4))),
        _stream(_chunk(text="Next answer", finish="stop", usage=(14, 5))),
    )
    try:
        task = asyncio.create_task(host._run_active_request(host._process_line("first request")))
        await asyncio.wait_for(started.wait(), timeout=5)
        await host._interrupt_active_request()
        assert await task and closed.is_set()
        events = _events(capsys)
        assert events[-1]["type"] == "line_complete"
        assert engine.total_usage.total_tokens == 524
        assert await host._run_active_request(host._process_line("second request"))
        events = _events(capsys)
        assert any(event["type"] == "assistant_complete" and event["message"] == "Next answer"
                   for event in events)
        assert events[-1]["type"] == "line_complete" and len(calls) == 4
        assert engine.total_usage.total_tokens == 560
    finally:
        await client._sdk_client.close()
