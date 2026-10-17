"""Render real Ink tool rows while preserving backend errors and upstream assets."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

from geoharness.frontend import frontend_overlay, tool_result_summary


def test_overlay_leaves_upstream_and_dependency_target_unchanged(tmp_path):
    source = tmp_path / "upstream"
    (source / "src/components").mkdir(parents=True)
    component = source / "src/components/ToolCallDisplay.tsx"
    component.write_text("original", encoding="utf-8")
    (source / "package.json").write_text('{}', encoding="utf-8")
    (source / "node_modules").mkdir()
    sentinel = source / "node_modules/sentinel"
    sentinel.write_text("dependency", encoding="utf-8")
    with frontend_overlay(source) as overlay:
        assert (overlay / "node_modules/sentinel").read_text() == "dependency"
        assert "resultLineCount" in (overlay / "src/components/ToolCallDisplay.tsx").read_text()
        assert component.read_text() == "original"
    assert not overlay.exists()
    assert sentinel.read_text() == "dependency"
    assert component.read_text() == "original"


async def test_print_error_only_displays_count_but_preserves_event(capsys):
    from openharness.engine.stream_events import ToolExecutionCompleted

    from geoharness.launcher import _render_event

    event = ToolExecutionCompleted(tool_name="geo_python", output="cause\n\ntrace\n",
                                   is_error=True)
    await _render_event(event)
    assert capsys.readouterr().out.strip() == "× → 2L"
    assert event.output == "cause\n\ntrace\n" and event.is_error
    assert tool_result_summary("", is_error=True) == "× → 0L"


async def test_frontend_launcher_restores_adapters_after_failure(tmp_path, monkeypatch):
    from openharness.ui import app, react_launcher

    from geoharness.config.settings import GeoConfig
    from geoharness.launcher import _run_react_tui

    source = tmp_path / "frontend"
    (source / "src/components").mkdir(parents=True)
    (source / "package.json").write_text('{}')
    getter = lambda: source  # noqa: E731
    monkeypatch.setattr(react_launcher, "get_frontend_dir", getter)
    command = react_launcher.build_backend_command
    overlays = []

    async def fail(**kwargs):
        overlay = react_launcher.get_frontend_dir()
        overlays.append(overlay)
        assert overlay != source and overlay.is_dir()
        assert "geoharness.tui_backend" in react_launcher.build_backend_command()
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(app, "run_repl", fail)
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        await _run_react_tui(tmp_path, GeoConfig(), "", None)
    assert react_launcher.get_frontend_dir is getter
    assert react_launcher.build_backend_command is command
    assert not overlays[0].exists()


@pytest.mark.parametrize("style", ["default", "codex"])
def test_real_react_render_folds_paired_and_restored_orphan_errors(style):
    from openharness.ui.react_launcher import _resolve_tsx, get_frontend_dir

    source = get_frontend_dir()
    if not (source / "node_modules/.bin/tsx.cmd").exists() and not (
            source / "node_modules/.bin/tsx").exists():
        pytest.skip("Installed React frontend dependencies unavailable")
    component = source / "src/components/ToolCallDisplay.tsx"
    before = hashlib.sha256(component.read_bytes()).hexdigest()
    with frontend_overlay(source) as overlay:
        probe = overlay / "probe.tsx"
        probe.write_text("""
import React from 'react';
import {PassThrough} from 'node:stream';
import {render} from 'ink';
import {ConversationView} from './src/components/ConversationView.js';
const output = new PassThrough();
let text = '';
output.on('data', (chunk) => {text += chunk.toString();});
const cause = 'SECRET_ERROR_BODY\\n\\nTRACE_BODY';
const result = {role:'tool_result', tool_name:'geo_python', text:cause, is_error:true};
const items = [{role:'tool', tool_name:'geo_python', text:'', tool_input:{name:'probe'}},
               result, {...result, tool_name:'restored_orphan'},
               {role:'tool', tool_name:'geo_analysis', text:''},
               {role:'tool_result', text:'first\\nsecond', is_error:false}];
const app = render(<ConversationView items={items as any} assistantBuffer=""
  showWelcome={false} outputStyle=STYLE />, {stdout:output as any, stderr:output as any,
  stdin: new PassThrough() as any, debug:true, patchConsole:false, exitOnCtrlC:false});
await new Promise((resolve) => setTimeout(resolve, 150));
app.unmount();
console.log(JSON.stringify({text, cause:result.text, failed:result.is_error}));
""".replace("STYLE", json.dumps(style)), encoding="utf-8")
        result = subprocess.run([*_resolve_tsx(overlay), str(probe)], cwd=overlay,
                                capture_output=True, text=True, encoding="utf-8", timeout=30,
                                env={**os.environ, "NO_COLOR": "1"})
        assert result.returncode == 0, result.stderr
        packet = json.loads(result.stdout.strip().splitlines()[-1])
        assert "×" in packet["text"] and "2L" in packet["text"]
        assert "restored_orphan" in packet["text"]
        assert "SECRET_ERROR_BODY" not in packet["text"]
        assert "TRACE_BODY" not in packet["text"]
        assert packet["cause"] == "SECRET_ERROR_BODY\n\nTRACE_BODY" and packet["failed"]
    assert hashlib.sha256(component.read_bytes()).hexdigest() == before
