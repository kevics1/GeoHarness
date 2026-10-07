"""Exercise upstream startup consumers without writing OpenHarness workspace state."""

from __future__ import annotations

from pathlib import Path
from typing import Any, AsyncIterator

import pytest

from geoharness.config.settings import GeoConfig


class LocalModelClient:
    """Keep the real query loop, replacing only the external model transport."""

    async def stream_message(self, request: Any) -> AsyncIterator[Any]:
        from openharness.api.client import ApiMessageCompleteEvent, ApiTextDeltaEvent
        from openharness.api.usage import UsageSnapshot
        from openharness.engine.messages import ConversationMessage, TextBlock

        text = (
            '{"ok": true}'
            if "strict JSON" in (request.system_prompt or "")
            else "hello from the isolated runtime"
        )
        yield ApiTextDeltaEvent(text=text)
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(
                role="assistant", content=[TextBlock(text=text)]
            ),
            usage=UsageSnapshot(input_tokens=1, output_tokens=1),
            stop_reason="end_turn",
        )


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    # Inherited upstream overrides must not redirect GeoHarness sessions/logs.
    for kind in ("CONFIG", "DATA", "LOGS"):
        monkeypatch.setenv(f"OPENHARNESS_{kind}_DIR", str(home / "upstream" / kind.lower()))
    monkeypatch.delenv("GEOH_CONFIG_PATH", raising=False)
    return home


@pytest.fixture
def config() -> GeoConfig:
    return GeoConfig(model="test-model", api_key="test-key", base_url="https://example.invalid/v1")


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from geoharness import runtime

    workspace = tmp_path / "project" / "example"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(runtime, "_warm_data_layer", lambda catalog: None)
    monkeypatch.setattr(runtime, "_build_api_client", lambda config: LocalModelClient())
    return workspace


async def _exercise_upstream_consumers(bundle: Any) -> None:
    from openharness.ui.runtime import handle_line

    async def discard(*args: Any) -> None:
        pass

    client = LocalModelClient()
    bundle.engine.set_api_client(client)
    bundle.hook_executor.update_context(api_client=client)
    await handle_line(
        bundle,
        "hello",
        print_system=discard,
        render_event=discard,
        clear_output=discard,
    )


async def test_real_runtime_prompt_and_plugins_do_not_create_openharness(
    isolated_home: Path, workspace: Path, config: GeoConfig
) -> None:
    from openharness.ui.runtime import close_runtime, start_runtime

    from geoharness.runtime import build_geo_runtime

    bundle = await build_geo_runtime(cwd=workspace, config_override=config)
    try:
        await start_runtime(bundle)
        await _exercise_upstream_consumers(bundle)
        assert bundle.cwd == str(workspace)
        assert bundle.engine._cwd == workspace.resolve()
        assert not (workspace / ".openharness").exists()
    finally:
        await close_runtime(bundle)


async def test_print_startup_keeps_working_directory_clean(
    isolated_home: Path, workspace: Path, config: GeoConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from geoharness import launcher, runtime

    monkeypatch.setattr(launcher, "load_geo_config", lambda path: config)
    monkeypatch.setattr(runtime, "_build_api_client", lambda config: LocalModelClient())
    monkeypatch.setattr("builtins.input", lambda prompt: "exit")
    assert await launcher.launch_geo_tui(cwd=workspace, prompt="hello", print_mode=True) == 0
    assert not (workspace / ".openharness").exists()


async def test_react_factory_and_first_prompt_keep_working_directory_clean(
    isolated_home: Path, workspace: Path, config: GeoConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openharness.ui.runtime import close_runtime

    from geoharness import runtime
    from geoharness.tui_backend import geo_backend_runtime_factory

    monkeypatch.setattr(runtime, "load_geo_config", lambda path: config)
    bundle = await geo_backend_runtime_factory(cwd=str(workspace))
    try:
        await _exercise_upstream_consumers(bundle)
        assert not (workspace / ".openharness").exists()
    finally:
        await close_runtime(bundle)


async def test_default_plugin_and_autopilot_paths_are_isolated_per_project(
    isolated_home: Path, workspace: Path, config: GeoConfig
) -> None:
    from openharness.autopilot import RepoAutopilotStore
    from openharness.config.settings import Settings
    from openharness.plugins import loader
    from openharness.ui.runtime import close_runtime

    from geoharness.runtime import build_geo_runtime

    bundle = await build_geo_runtime(cwd=workspace, config_override=config)
    try:
        other = workspace.parent / "other" / workspace.name
        other.mkdir(parents=True)
        roots: list[Path] = []
        for cwd in (workspace, other):
            assert loader.load_plugins(Settings(), cwd) == []
            store = RepoAutopilotStore(cwd)
            root = store.registry_path.parent.parent
            assert root.is_relative_to(isolated_home / ".geoharness")
            assert loader.get_project_plugins_dir(cwd).parent == root
            assert not (cwd / ".openharness").exists()
            roots.append(root)
        assert roots[0] != roots[1]
        first = RepoAutopilotStore(workspace)
        first.enqueue_card(source_kind="manual_idea", title="project one task")
        assert len(first.list_cards()) == 1
        assert RepoAutopilotStore(other).list_cards() == []
    finally:
        await close_runtime(bundle)


async def test_sessions_and_logs_ignore_upstream_overrides_and_preserve_project_isolation(
    isolated_home: Path, workspace: Path, config: GeoConfig
) -> None:
    from openharness.config.paths import get_logs_dir
    from openharness.engine.messages import ConversationMessage
    from openharness.ui.runtime import close_runtime

    from geoharness.runtime import build_geo_runtime

    bundle = await build_geo_runtime(cwd=workspace, config_override=config)
    try:
        log_path = get_logs_dir() / "startup.log"
        log_path.write_text("started", encoding="utf-8")
        assert log_path.is_relative_to(isolated_home / ".geoharness")
        snapshot = bundle.session_backend.save_snapshot(
            cwd=workspace,
            model=config.model,
            system_prompt=bundle.engine.system_prompt,
            messages=[ConversationMessage.from_user_text("workspace one")],
            usage=bundle.engine.total_usage,
            session_id="project-one",
        )
        assert snapshot.is_relative_to(isolated_home / ".geoharness")
        assert bundle.session_backend.load_latest(workspace)["summary"] == "workspace one"
        other = workspace.parent / "other" / workspace.name
        other.mkdir(parents=True)
        assert bundle.session_backend.get_session_dir(other) != snapshot.parent
        assert bundle.session_backend.load_latest(other) is None
        assert not (isolated_home / "upstream").exists()
        assert not (workspace / ".openharness").exists()
    finally:
        await close_runtime(bundle)
