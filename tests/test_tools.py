"""Tests for geoharness.tools — geo_data, geo_cartography, registry.

Tests verify:
- Native tools have correct name, description, input_model
- Tools execute correctly with mocked context
- Tool registry builds with correct tool count (2 native + 5 whitelist)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.config.settings import (
    BridgeRule,
    CartographyConfig,
    GeoConfig,
)
from geoharness.tools.geo_cartography import (
    GeoCartographyInput,
    GeoCartographyTool,
)
from geoharness.tools.geo_data import GeoDataInput, GeoDataTool

# ─── Mock DataCatalog ─────────────────────────────────────────────────────


class MockDataCatalog:
    """Mock DataCatalog for testing."""

    def list_sources(self, source_type: str = "all"):
        from geoharness.data.catalog import DataSource

        return [
            DataSource(
                name="wuhan_air",
                source_type="postgis",
                metadata={"format": "PostGIS table", "crs": "EPSG:4326"},
            ),
            DataSource(
                name="test_data",
                source_type="file",
                metadata={"format": "geojson", "crs": "EPSG:4326"},
            ),
        ]

    def get_source_detail(self, name: str):
        if name == "wuhan_air":
            return {
                "name": "wuhan_air",
                "source_type": "postgis",
                "format": "PostGIS table",
                "path": "wuhan_air",
                "crs": "EPSG:4326",
                "bbox": [114.0, 30.0, 115.0, 31.0],
                "schema": {"id": "integer", "pm25": "double", "geom": "geometry"},
            }
        return None


# ─── GeoDataTool Tests ────────────────────────────────────────────────────


class TestGeoDataTool:
    """Test the geo_data tool."""

    @pytest.fixture
    def tool(self) -> GeoDataTool:
        return GeoDataTool()

    @pytest.fixture
    def context(self) -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=Path("/tmp"),
            metadata={"data_catalog": MockDataCatalog()},
            hook_executor=None,
        )

    def test_tool_name(self, tool: GeoDataTool):
        assert tool.name == "geo_data"

    def test_tool_description(self, tool: GeoDataTool):
        assert "geographic data" in tool.description.lower()

    def test_tool_input_model(self, tool: GeoDataTool):
        assert tool.input_model is GeoDataInput

    def test_tool_is_read_only(self, tool: GeoDataTool):
        assert tool.is_read_only(GeoDataInput()) is True

    @pytest.mark.asyncio
    async def test_list_action(self, tool: GeoDataTool, context: ToolExecutionContext):
        result = await tool.execute(
            GeoDataInput(action="list", source="all"), context
        )
        assert not result.is_error
        assert "wuhan_air" in result.output
        assert "test_data" in result.output
        assert "2" in result.output

    @pytest.mark.asyncio
    async def test_inspect_action(self, tool: GeoDataTool, context: ToolExecutionContext):
        result = await tool.execute(
            GeoDataInput(action="inspect", name="wuhan_air"), context
        )
        assert not result.is_error
        assert "wuhan_air" in result.output
        assert "EPSG:4326" in result.output
        assert "pm25" in result.output

    @pytest.mark.asyncio
    async def test_inspect_not_found(
        self, tool: GeoDataTool, context: ToolExecutionContext
    ):
        result = await tool.execute(
            GeoDataInput(action="inspect", name="nonexistent"), context
        )
        assert result.is_error
        assert "not found" in result.output.lower()

    @pytest.mark.asyncio
    async def test_load_action(self, tool: GeoDataTool, context: ToolExecutionContext):
        result = await tool.execute(
            GeoDataInput(action="load", name="wuhan_air"), context
        )
        assert not result.is_error
        assert "PostGIS" in result.output or "postgres" in result.output.lower()

    @pytest.mark.asyncio
    async def test_no_catalog_error(
        self, tool: GeoDataTool
    ):
        context = ToolExecutionContext(
            cwd=Path("/tmp"),
            metadata={},  # No data_catalog
            hook_executor=None,
        )
        result = await tool.execute(GeoDataInput(action="list"), context)
        assert result.is_error
        assert "DataCatalog" in result.output


# ─── GeoCartographyTool Tests ─────────────────────────────────────────────


class TestGeoCartographyTool:
    """Test the geo_cartography tool."""

    @pytest.fixture
    def tool(self) -> GeoCartographyTool:
        return GeoCartographyTool()

    @pytest.fixture
    def config_with_rules(self) -> GeoConfig:
        return GeoConfig(
            cartography=CartographyConfig(
                default_template="F:/test/template.qpt",
                bridge_rules={
                    "moran_local": BridgeRule(
                        render_method="categorized",
                        color_scheme="RdBu",
                        n_classes=5,
                    ),
                    "getis_ord": BridgeRule(
                        render_method="graduated",
                        color_scheme="YlOrRd",
                        n_classes=6,
                    ),
                },
            )
        )

    @pytest.fixture
    def context(self, config_with_rules: GeoConfig) -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=Path("/tmp"),
            metadata={"geoharness_config": config_with_rules},
            hook_executor=None,
        )

    def test_tool_name(self, tool: GeoCartographyTool):
        assert tool.name == "geo_cartography"

    def test_tool_description(self, tool: GeoCartographyTool):
        assert "cartography" in tool.description.lower()

    def test_tool_input_model(self, tool: GeoCartographyTool):
        assert tool.input_model is GeoCartographyInput

    @pytest.mark.asyncio
    async def test_symbolize_action(
        self, tool: GeoCartographyTool, context: ToolExecutionContext
    ):
        result = await tool.execute(
            GeoCartographyInput(
                action="symbolize",
                layer_name="analysis_result",
                analysis_type="moran_local",
                field_name="lisa_class",
            ),
            context,
        )
        assert not result.is_error
        assert "categorized" in result.output
        assert "RdBu" in result.output
        assert "moran_local" in result.output

    @pytest.mark.asyncio
    async def test_symbolize_graduated(
        self, tool: GeoCartographyTool, context: ToolExecutionContext
    ):
        result = await tool.execute(
            GeoCartographyInput(
                action="symbolize",
                layer_name="hotspots",
                analysis_type="getis_ord",
                field_name="z_score",
            ),
            context,
        )
        assert not result.is_error
        assert "graduated" in result.output
        assert "YlOrRd" in result.output

    @pytest.mark.asyncio
    async def test_symbolize_unknown_analysis_type(
        self, tool: GeoCartographyTool, context: ToolExecutionContext
    ):
        result = await tool.execute(
            GeoCartographyInput(
                action="symbolize",
                layer_name="test",
                analysis_type="unknown_type",
            ),
            context,
        )
        assert not result.is_error
        # Should fall back to default
        assert "categorized" in result.output or "Set1" in result.output

    @pytest.mark.asyncio
    async def test_compose_action(
        self, tool: GeoCartographyTool, context: ToolExecutionContext
    ):
        result = await tool.execute(
            GeoCartographyInput(action="compose"), context
        )
        assert not result.is_error
        assert "template" in result.output.lower()
        assert "F:/test/template.qpt" in result.output

    @pytest.mark.asyncio
    async def test_export_action(
        self, tool: GeoCartographyTool, context: ToolExecutionContext
    ):
        result = await tool.execute(
            GeoCartographyInput(
                action="export", output_format="pdf"
            ),
            context,
        )
        assert not result.is_error
        assert "pdf" in result.output.lower()
        assert "Native renderer" in result.output

    @pytest.mark.asyncio
    async def test_full_pipeline(
        self, tool: GeoCartographyTool, context: ToolExecutionContext
    ):
        result = await tool.execute(
            GeoCartographyInput(
                action="full",
                layer_name="analysis_result",
                analysis_type="moran_local",
                field_name="lisa_class",
                output_format="pdf",
            ),
            context,
        )
        assert not result.is_error
        assert "Full cartography pipeline" in result.output
        assert "categorized" in result.output
        assert "template" in result.output.lower()

    @pytest.mark.asyncio
    async def test_no_config_error(self, tool: GeoCartographyTool):
        context = ToolExecutionContext(
            cwd=Path("/tmp"),
            metadata={},  # No config
            hook_executor=None,
        )
        result = await tool.execute(
            GeoCartographyInput(action="symbolize"), context
        )
        assert result.is_error
        assert "config" in result.output.lower()

    @pytest.mark.asyncio
    async def test_symbolize_missing_layer(
        self, tool: GeoCartographyTool, context: ToolExecutionContext
    ):
        result = await tool.execute(
            GeoCartographyInput(
                action="symbolize", analysis_type="moran_local"
            ),
            context,
        )
        assert result.is_error
        assert "layer_name" in result.output

    def test_is_read_only_symbolize(self, tool: GeoCartographyTool):
        # symbolize action is read-only
        args = GeoCartographyInput(action="symbolize")
        assert tool.is_read_only(args) is True

    def test_is_not_read_only_export(self, tool: GeoCartographyTool):
        # export action is not read-only
        args = GeoCartographyInput(action="export")
        assert tool.is_read_only(args) is False


# ─── Tool Registry Tests ──────────────────────────────────────────────────


class TestBuildGeoToolRegistry:
    """Test build_geo_tool_registry function."""

    def test_registry_has_native_tools(self):
        """Registry should contain geo_data and geo_cartography."""
        from geoharness.tools import build_geo_tool_registry

        registry = build_geo_tool_registry()
        tools = registry.list_tools()
        tool_names = {t.name for t in tools}

        assert "geo_data" in tool_names
        assert "geo_cartography" in tool_names

    def test_registry_has_whitelist_tools(self):
        """Registry should contain whitelisted OpenHarness tools."""
        from geoharness.tools import build_geo_tool_registry

        registry = build_geo_tool_registry()
        tools = registry.list_tools()
        tool_names = {t.name for t in tools}

        assert "ask_user_question" in tool_names
        assert "skill" in tool_names
        assert "todo_write" in tool_names
        assert "tool_search" in tool_names
        assert "brief" in tool_names

    def test_registry_excludes_non_whitelist_tools(self):
        """Registry should NOT contain non-whitelisted OpenHarness tools."""
        from geoharness.tools import build_geo_tool_registry

        registry = build_geo_tool_registry()
        tools = registry.list_tools()
        tool_names = {t.name for t in tools}

        # These should NOT be present
        assert "bash" not in tool_names
        assert "read_file" not in tool_names
        assert "write_file" not in tool_names
        assert "edit_file" not in tool_names
        assert "glob" not in tool_names
        assert "grep" not in tool_names
        assert "web_fetch" not in tool_names
        assert "web_search" not in tool_names

    def test_registry_tool_count(self):
        """Registry should have 2 native + 5 whitelist = 7 tools
        (without MCP).
        """
        from geoharness.tools import build_geo_tool_registry

        registry = build_geo_tool_registry()
        tools = registry.list_tools()

        # 2 native + 5 whitelist = 7 (no MCP connected)
        assert len(tools) == 7

    def test_registry_with_mock_mcp(self):
        """Registry with mock MCP manager should register MCP tools."""
        from geoharness.tools import build_geo_tool_registry

        mock_mcp = MagicMock()
        # Mock list_tools to return some fake MCP tools
        mock_mcp.list_tools = MagicMock(return_value=[])

        registry = build_geo_tool_registry(mcp_manager=mock_mcp)
        tools = registry.list_tools()

        # Should still have at least the 7 base tools
        assert len(tools) >= 7
