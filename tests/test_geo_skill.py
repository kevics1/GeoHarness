"""Regression tests for discoverable geography skills and legacy names."""

from pathlib import Path
from unittest.mock import patch

import pytest
from openharness.skills.loader import load_skills_from_dirs
from openharness.skills.registry import SkillRegistry
from openharness.skills.types import SkillDefinition
from openharness.tools.base import ToolExecutionContext
from openharness.tools.skill_tool import SkillToolInput

from geoharness.tools.geo_skill import GeoSkillTool


def _registry(*skills: SkillDefinition) -> SkillRegistry:
    registry = SkillRegistry()
    for skill in skills:
        registry.register(skill)
    return registry


def _skill(name: str, **kwargs) -> SkillDefinition:
    return SkillDefinition(
        name=name, description=f"Description of {name}", content=f"Content of {name}",
        source="test", **kwargs,
    )


@pytest.mark.parametrize("name", ["map-making", "MAP-MAKING", "map_making", "mapmaking"])
async def test_map_making_loads_actual_cartography(name, tmp_path):
    registry = _registry(_skill("cartography"))
    with patch("geoharness.tools.geo_skill.load_skill_registry", return_value=registry):
        result = await GeoSkillTool().execute(
            SkillToolInput(name=name), ToolExecutionContext(cwd=tmp_path),
        )
    assert not result.is_error
    assert result.output == "Content of cartography"
    assert result.metadata["skill_name"] == "cartography"


async def test_explicit_user_skill_takes_precedence_over_compatibility_alias(tmp_path):
    registry = _registry(_skill("cartography"), _skill("map-making"))
    with patch("geoharness.tools.geo_skill.load_skill_registry", return_value=registry):
        result = await GeoSkillTool().execute(
            SkillToolInput(name="map-making"), ToolExecutionContext(cwd=tmp_path),
        )
    assert result.output == "Content of map-making"
    assert result.metadata["skill_name"] == "map-making"


@pytest.mark.parametrize("name", ["map-making", "cartography"])
async def test_alias_cannot_bypass_user_only_invocation(name, tmp_path):
    registry = _registry(_skill("cartography", disable_model_invocation=True))
    with patch("geoharness.tools.geo_skill.load_skill_registry", return_value=registry):
        result = await GeoSkillTool().execute(
            SkillToolInput(name=name), ToolExecutionContext(cwd=tmp_path),
        )
    assert result.is_error
    assert "only be invoked by the user as /cartography" in result.output


async def test_unknown_skill_lists_only_actual_loaded_names(tmp_path):
    registry = _registry(_skill("cartography"), _skill("Personal Analysis", command_name="local"))
    with patch("geoharness.tools.geo_skill.load_skill_registry", return_value=registry):
        result = await GeoSkillTool().execute(
            SkillToolInput(name="missing"), ToolExecutionContext(cwd=tmp_path),
        )
    assert result.is_error
    assert result.metadata["available_skills"] == ["cartography", "local"]
    assert "- cartography\n- local" in result.output
    assert "spatial-analysis" not in result.output


async def test_extra_skill_directories_and_plugin_roots_are_preserved(tmp_path):
    metadata = {"extra_skill_dirs": ("package/skills",), "extra_plugin_roots": ("plugins",)}
    with patch(
        "geoharness.tools.geo_skill.load_skill_registry",
        return_value=_registry(_skill("plugin-name", aliases=("Plugin Alias",))),
    ) as load:
        result = await GeoSkillTool().execute(
            SkillToolInput(name="plugin alias"),
            ToolExecutionContext(cwd=tmp_path, metadata=metadata),
        )
    assert not result.is_error
    load.assert_called_once_with(
        tmp_path, extra_skill_dirs=("package/skills",), extra_plugin_roots=("plugins",),
    )


async def test_map_making_uses_real_bundled_skill_content(tmp_path):
    skills_path = Path(__file__).parent.parent / "skills"
    registry = _registry(*load_skills_from_dirs([skills_path]))
    with patch("geoharness.tools.geo_skill.load_skill_registry", return_value=registry):
        result = await GeoSkillTool().execute(
            SkillToolInput(name="map-making"), ToolExecutionContext(cwd=tmp_path),
        )
    assert not result.is_error
    assert result.output == (skills_path / "cartography" / "SKILL.md").read_text(encoding="utf-8")


def test_geo_registry_uses_skill_adapter_without_changing_tool_count():
    from geoharness.tools import build_geo_tool_registry

    registry = build_geo_tool_registry()
    tool = registry.get("skill")
    assert isinstance(tool, GeoSkillTool)
    assert tool.is_read_only(SkillToolInput(name="cartography"))
    assert len(registry.list_tools()) == 15

