"""Regression tests for the spatial-analysis workflow frictions:

1. ``skill(name="spatial-analysis")`` failed — the skill did not exist, so
   the model burned turns guessing (spatial-analysis / map / cartography).
2. ``geo_db_data action=load`` on a pure-attribute table (admin_attributes)
   raised "no geometry column" instead of describing the table's analytic use.
3. The system prompt did not point the model at the spatial-analysis entry
   skill, nor warn that point data cannot directly yield Moran's I.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.config.settings import GeoConfig
from geoharness.data.catalog import DataCatalog
from geoharness.prompts.system_prompt import build_geo_system_prompt
from geoharness.tools.geo_db_data import GeoDBDataInput, GeoDBDataTool


class _MockPostGIS:
    def get_connector(self, name: str):  # noqa: D102
        return self

    connector = None

    def get_source_detail(self, name: str) -> dict:  # noqa: D102
        return {
            "name": name,
            "source_type": "postgis",
            "schema": {
                "region_id": "character varying",
                "population": "bigint",
                "gdp": "double precision",
            },
        }

    def query_geodataframe(self, name: str, limit: int = 5000):
        raise ValueError(f"Table '{name}' has no geometry column")


def _context(tmp_path: Path, catalog) -> ToolExecutionContext:
    return ToolExecutionContext(
        cwd=tmp_path, metadata={"data_catalog": catalog}, hook_executor=None
    )


class TestAttributeTableLoad:
    @pytest.mark.asyncio
    async def test_attribute_table_load_is_not_an_error(self, tmp_path: Path) -> None:
        """admin_attributes-style tables load as 'usable for joins', not fail."""
        catalog = DataCatalog(GeoConfig(), workspace_dir=str(tmp_path))
        catalog.register_connector("postgis", _MockPostGIS())  # type: ignore[arg-type]

        res = await GeoDBDataTool().execute(
            GeoDBDataInput(action="load", name="admin_attributes"),
            _context(tmp_path, catalog),
        )
        assert not res.is_error, res.output
        assert "attribute table" in res.output
        assert "population" in res.output  # schema surfaced
        assert res.metadata.get("attribute_only") is True

    @pytest.mark.asyncio
    async def test_other_value_errors_still_error(self, tmp_path: Path) -> None:
        class Boom(_MockPostGIS):
            def query_geodataframe(self, name: str, limit: int = 5000):
                raise ValueError("contains no rows")

        catalog = DataCatalog(GeoConfig(), workspace_dir=str(tmp_path))
        catalog.register_connector("postgis", Boom())  # type: ignore[arg-type]
        res = await GeoDBDataTool().execute(
            GeoDBDataInput(action="load", name="empty_table"),
            _context(tmp_path, catalog),
        )
        assert res.is_error
        assert "contains no rows" in res.output


class TestSpatialAnalysisSkill:
    def test_skill_file_exists_and_registers(self) -> None:
        from openharness.skills.loader import load_skill_registry

        skill_path = Path(__file__).resolve().parent.parent / "skills"
        reg = load_skill_registry(skill_path.parent, extra_skill_dirs=("skills",))
        names = {s.name for s in reg.list_skills()}
        assert "spatial-analysis" in names

    def test_skill_references_real_tools_and_skills(self) -> None:
        """The entry skill must only reference tools/skills that exist."""
        skill_path = (
            Path(__file__).resolve().parent.parent
            / "skills" / "spatial-analysis" / "SKILL.md"
        )
        text = skill_path.read_text(encoding="utf-8")
        for token in (
            "geo_vector_data", "geo_db_data", "geo_cartography",
            "geo_moran_global", "geo_getis_ord", "geo_cluster_detect",
            "proximity-analysis", "overlay-analysis", "zonal-statistics",
            "cartography",
        ):
            assert token in text, f"skill must reference {token}"


class TestSystemPromptGuidance:
    def _prompt(self, tmp_path: Path) -> str:
        return build_geo_system_prompt(tmp_path, GeoConfig())

    def test_prompt_names_the_entry_skill(self, tmp_path: Path) -> None:
        assert "spatial-analysis" in self._prompt(tmp_path)

    def test_prompt_warns_point_data_needs_aggregation(self, tmp_path: Path) -> None:
        prompt = self._prompt(tmp_path)
        assert "点数据不能直接算" in prompt
        assert "MCP" in prompt  # statistics must go through MCP tools

    def test_prompt_uses_real_skill_tool_name(self, tmp_path: Path) -> None:
        """The skill tool is 'skill', not 'skill_view' (old prompt typo)."""
        prompt = self._prompt(tmp_path)
        assert "skill_view" not in prompt
        assert 'skill(name="geo-perception")' in prompt


class TestSkillVisibleAtRuntime:
    @pytest.mark.asyncio
    async def test_skill_tool_loads_bundled_skill_via_runtime(
        self, tmp_path: Path
    ) -> None:
        """The runtime must expose extra_skill_dirs to the `skill` tool.

        Regression: tool_metadata lacked extra_skill_dirs, so bundled skills
        (cartography, spatial-analysis, …) were invisible in the TUI even
        though the system prompt told the model to load them.
        """
        from openharness.tools.skill_tool import SkillTool

        from geoharness.runtime import build_geo_runtime

        rt = await build_geo_runtime(cwd=tmp_path)
        ctx = ToolExecutionContext(
            cwd=tmp_path, metadata=rt.engine.tool_metadata, hook_executor=None
        )
        res = await SkillTool().execute(
            type("_Args", (), {"name": "spatial-analysis"})(), ctx
        )
        assert not res.is_error, res.output
        assert "空间分析" in res.output
