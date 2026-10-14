"""Agent checklists stay internal without weakening configured path restrictions."""

from pathlib import Path

import pytest
from openharness.tools.base import ToolExecutionContext
from openharness.tools.todo_write_tool import TodoWriteToolInput

from geoharness.config.settings import AgentConfig, GeoConfig, PermissionsConfig
from geoharness.runtime import _build_permission_checker
from geoharness.tools import build_geo_tool_registry


async def test_default_todo_preserves_project_file(tmp_path):
    project = tmp_path / "TODO.md"
    project.write_text("# Human project tasks\n", encoding="utf-8")
    config = GeoConfig()
    registry = build_geo_tool_registry(config=config)
    tool = next(t for t in registry.list_tools() if t.name == "todo_write")
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    result = await tool.execute(TodoWriteToolInput(item="检查地图"), context)
    assert not result.is_error, result.output
    assert project.read_text(encoding="utf-8") == "# Human project tasks\n"
    internal = tmp_path / config.agent.internal_todo_path
    assert "- [ ] 检查地图" in internal.read_text(encoding="utf-8")
    await tool.execute(TodoWriteToolInput(item="检查地图", checked=True), context)
    assert "- [x] 检查地图" in internal.read_text(encoding="utf-8")
    assert _build_permission_checker(config).evaluate("todo_write", is_read_only=False).allowed


@pytest.mark.parametrize("path", ["other.md", "../TODO.md", ".env", "data/tasks.md"])
async def test_todo_rejects_arbitrary_destinations(tmp_path, path):
    config = GeoConfig()
    tool = next(t for t in build_geo_tool_registry(config=config).list_tools()
                if t.name == "todo_write")
    result = await tool.execute(TodoWriteToolInput(item="task", path=path),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config}))
    assert result.is_error
    assert not (tmp_path / config.agent.internal_todo_path).exists()


@pytest.mark.parametrize("location", ["TODO.md", "../tasks.md", ".geoharness/.env",
                                    ".ssh/tasks.md"])
async def test_invalid_configured_todo_destination_is_rejected(tmp_path, location):
    config = GeoConfig(agent=AgentConfig(internal_todo_path=location))
    tool = next(t for t in build_geo_tool_registry(config=config).list_tools()
                if t.name == "todo_write")
    result = await tool.execute(TodoWriteToolInput(item="task"),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config}))
    assert result.is_error


@pytest.mark.parametrize("pattern", ["absolute", ".geoharness/tasks/*",
                                    ".geoharness/tasks", ".geoharness\\tasks\\*"])
async def test_allowed_todo_still_enforces_custom_deny_paths(tmp_path, pattern):
    pattern = str(tmp_path / ".geoharness/tasks") + "/*" if pattern == "absolute" else pattern
    config = GeoConfig(permissions=PermissionsConfig(
        deny_paths=[pattern]))
    assert _build_permission_checker(config).evaluate("todo_write", is_read_only=False).allowed
    tool = next(t for t in build_geo_tool_registry(config=config).list_tools()
                if t.name == "todo_write")
    result = await tool.execute(TodoWriteToolInput(item="task"),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config}))
    assert result.is_error and "denied" in result.output.lower()
    assert not (tmp_path / config.agent.internal_todo_path).exists()


async def test_todo_refuses_redirected_directory(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    internal = tmp_path / ".geoharness/tasks"
    internal.parent.mkdir()
    try:
        internal.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    config = GeoConfig()
    tool = next(t for t in build_geo_tool_registry(config=config).list_tools()
                if t.name == "todo_write")
    result = await tool.execute(TodoWriteToolInput(item="task"),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config}))
    assert result.is_error
    assert not (external / "TODO.md").exists()


async def test_todo_refuses_hardlink_to_project_file(tmp_path):
    project = tmp_path / "TODO.md"
    project.write_text("# Human project tasks\n", encoding="utf-8")
    config = GeoConfig()
    internal = tmp_path / config.agent.internal_todo_path
    internal.parent.mkdir(parents=True)
    try:
        internal.hardlink_to(project)
    except OSError:
        pytest.skip("Hardlink creation is unavailable")
    tool = next(t for t in build_geo_tool_registry(config=config).list_tools()
                if t.name == "todo_write")
    result = await tool.execute(TodoWriteToolInput(item="task"),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config}))
    assert result.is_error
    assert project.read_text(encoding="utf-8") == "# Human project tasks\n"


def test_complete_qgis_layers_json_is_normalized_before_validation():
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput

    value = GeoQgisProjectInput(action="create", layers='[{"file_path":"data.geojson"}]')
    assert value.layers[0].file_path == "data.geojson"
    for invalid in ('[{"file_path":', '{}', 'null', '[1]', '[]'):
        with pytest.raises(ValueError):
            GeoQgisProjectInput(action="create", layers=invalid)
    assert Path(value.layers[0].file_path).name == "data.geojson"
