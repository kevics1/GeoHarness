"""Tests for geoharness.tools — db/vector/raster loaders, cartography, registry.

Tests verify:
- Native tools have correct name, description, input_model
- Tools execute correctly with mocked context
- Tool registry builds with correct tool count (4 native + 5 whitelist)
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
from geoharness.data.catalog import DataSource
from geoharness.tools.geo_cartography import (
    GeoCartographyInput,
    GeoCartographyTool,
)
from geoharness.tools.geo_db_data import GeoDBDataInput, GeoDBDataTool
from geoharness.tools.geo_raster_data import GeoRasterDataInput, GeoRasterDataTool
from geoharness.tools.geo_vector_data import GeoVectorDataInput, GeoVectorDataTool

# ─── Mock connectors / catalog ────────────────────────────────────────────


class _MockPostGIS:
    def list_sources(self):
        return [
            DataSource(
                name="wuhan_air",
                source_type="postgis",
                metadata={"format": "PostGIS table", "crs": "EPSG:4326"},
            )
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
        return {}

    def query_geodataframe(self, name: str, limit: int = 5000):
        import geopandas as gpd
        from shapely.geometry import Point

        return gpd.GeoDataFrame(
            {"id": [1, 2], "pm25": [35.0, 42.0]},
            geometry=[Point(0, 0), Point(1, 1)],
            crs="EPSG:4326",
        )


class _MockFileLoader:
    def list_sources(self):
        return [
            DataSource(
                name="test_data",
                source_type="file",
                metadata={"format": "geojson", "crs": "EPSG:4326"},
            )
        ]

    def get_source_detail(self, name: str):
        if name == "test_data":
            return {
                "name": "test_data",
                "source_type": "file",
                "format": "geojson",
                "path": "/tmp/test_data.geojson",
                "crs": "EPSG:4326",
                "geometry_type": "Point",
                "bbox": [0.0, 0.0, 1.0, 1.0],
                "schema": {"id": "int64", "name": "object"},
            }
        return {}

    def load_geodataframe(self, name: str):
        import geopandas as gpd
        from shapely.geometry import Point

        return gpd.GeoDataFrame(
            {"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326"
        )


class _MockRasterLoader:
    def list_sources(self):
        return [
            DataSource(
                name="dem",
                source_type="raster",
                metadata={"format": "tif"},
            )
        ]

    def get_source_detail(self, name: str):
        if name == "dem":
            return {
                "name": "dem",
                "source_type": "raster",
                "format": "gtiff",
                "path": "/tmp/dem.tif",
                "crs": "EPSG:32647",
                "width": 100,
                "height": 80,
                "band_count": 1,
                "dtype": "int16",
                "nodata": -32768.0,
                "bbox": [0.0, 0.0, 100.0, 80.0],
                "resolution": [30.0, 30.0],
            }
        return {}

    def band_stats(self, name: str, band: int = 1):
        return {
            "band": band,
            "count": 100,
            "min": 1.0,
            "max": 4139.0,
            "mean": 2000.0,
            "std": 500.0,
            "p5": 800.0,
            "p50": 2000.0,
            "p95": 3500.0,
        }


class MockDataCatalog:
    """Mock DataCatalog exposing the three type-specific connectors."""

    def __init__(self) -> None:
        self.postgis = _MockPostGIS()
        self.file = _MockFileLoader()
        self.raster = _MockRasterLoader()

    def get_connector(self, name: str):
        return {"postgis": self.postgis, "file": self.file}.get(name)

    def get_raster_connector(self):
        return self.raster


# ─── GeoDBDataTool Tests ──────────────────────────────────────────────────


class TestGeoDBDataTool:
    @pytest.fixture
    def tool(self) -> GeoDBDataTool:
        return GeoDBDataTool()

    @pytest.fixture
    def context(self) -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=Path("/tmp"),
            metadata={"data_catalog": MockDataCatalog()},
            hook_executor=None,
        )

    def test_tool_metadata(self, tool: GeoDBDataTool):
        assert tool.name == "geo_db_data"
        assert "postgis" in tool.description.lower()
        assert tool.input_model is GeoDBDataInput
        assert tool.is_read_only(GeoDBDataInput()) is True

    @pytest.mark.asyncio
    async def test_list(self, tool: GeoDBDataTool, context: ToolExecutionContext):
        result = await tool.execute(GeoDBDataInput(action="list"), context)
        assert not result.is_error
        assert "wuhan_air" in result.output

    @pytest.mark.asyncio
    async def test_inspect(self, tool: GeoDBDataTool, context: ToolExecutionContext):
        result = await tool.execute(
            GeoDBDataInput(action="inspect", name="wuhan_air"), context
        )
        assert not result.is_error
        assert "EPSG:4326" in result.output
        assert "pm25" in result.output

    @pytest.mark.asyncio
    async def test_inspect_not_found(
        self, tool: GeoDBDataTool, context: ToolExecutionContext
    ):
        result = await tool.execute(
            GeoDBDataInput(action="inspect", name="ghost"), context
        )
        assert result.is_error
        assert "not found" in result.output.lower()

    @pytest.mark.asyncio
    async def test_load(self, tool: GeoDBDataTool, context: ToolExecutionContext):
        result = await tool.execute(
            GeoDBDataInput(action="load", name="wuhan_air", limit=2), context
        )
        assert not result.is_error
        assert "Features: 2" in result.output

    @pytest.mark.asyncio
    async def test_missing_catalog(self, tool: GeoDBDataTool):
        ctx = ToolExecutionContext(cwd=Path("/tmp"), metadata={}, hook_executor=None)
        result = await tool.execute(GeoDBDataInput(action="list"), ctx)
        assert result.is_error
        assert "DataCatalog" in result.output

    @pytest.mark.asyncio
    async def test_no_postgis_configured(self, tool: GeoDBDataTool):
        class Empty:
            def get_connector(self, name: str):
                return None

        ctx = ToolExecutionContext(
            cwd=Path("/tmp"),
            metadata={"data_catalog": Empty()},
            hook_executor=None,
        )
        result = await tool.execute(GeoDBDataInput(action="list"), ctx)
        assert result.is_error
        assert "PostGIS is not configured" in result.output


# ─── GeoVectorDataTool Tests ──────────────────────────────────────────────


class TestGeoVectorDataTool:
    @pytest.fixture
    def tool(self) -> GeoVectorDataTool:
        return GeoVectorDataTool()

    @pytest.fixture
    def context(self) -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=Path("/tmp"),
            metadata={"data_catalog": MockDataCatalog()},
            hook_executor=None,
        )

    def test_tool_metadata(self, tool: GeoVectorDataTool):
        assert tool.name == "geo_vector_data"
        assert "vector" in tool.description.lower()
        assert tool.input_model is GeoVectorDataInput
        assert tool.is_read_only(GeoVectorDataInput()) is True

    @pytest.mark.asyncio
    async def test_list(self, tool: GeoVectorDataTool, context: ToolExecutionContext):
        result = await tool.execute(GeoVectorDataInput(action="list"), context)
        assert not result.is_error
        assert "test_data" in result.output

    @pytest.mark.asyncio
    async def test_inspect(self, tool: GeoVectorDataTool, context: ToolExecutionContext):
        result = await tool.execute(
            GeoVectorDataInput(action="inspect", name="test_data"), context
        )
        assert not result.is_error
        assert "EPSG:4326" in result.output
        assert "Point" in result.output

    @pytest.mark.asyncio
    async def test_inspect_not_found(
        self, tool: GeoVectorDataTool, context: ToolExecutionContext
    ):
        result = await tool.execute(
            GeoVectorDataInput(action="inspect", name="ghost"), context
        )
        assert result.is_error
        assert "not found" in result.output.lower()

    @pytest.mark.asyncio
    async def test_load(self, tool: GeoVectorDataTool, context: ToolExecutionContext):
        result = await tool.execute(
            GeoVectorDataInput(action="load", name="test_data"), context
        )
        assert not result.is_error
        assert "Features: 1" in result.output

    @pytest.mark.asyncio
    async def test_missing_name_errors(
        self, tool: GeoVectorDataTool, context: ToolExecutionContext
    ):
        result = await tool.execute(GeoVectorDataInput(action="inspect"), context)
        assert result.is_error


# ─── GeoRasterDataTool Tests ──────────────────────────────────────────────


class TestGeoRasterDataTool:
    @pytest.fixture
    def tool(self) -> GeoRasterDataTool:
        return GeoRasterDataTool()

    @pytest.fixture
    def context(self) -> ToolExecutionContext:
        return ToolExecutionContext(
            cwd=Path("/tmp"),
            metadata={"data_catalog": MockDataCatalog()},
            hook_executor=None,
        )

    def test_tool_metadata(self, tool: GeoRasterDataTool):
        assert tool.name == "geo_raster_data"
        assert "raster" in tool.description.lower()
        assert tool.input_model is GeoRasterDataInput
        assert tool.is_read_only(GeoRasterDataInput()) is True

    @pytest.mark.asyncio
    async def test_list(self, tool: GeoRasterDataTool, context: ToolExecutionContext):
        result = await tool.execute(GeoRasterDataInput(action="list"), context)
        assert not result.is_error
        assert "dem" in result.output

    @pytest.mark.asyncio
    async def test_inspect(self, tool: GeoRasterDataTool, context: ToolExecutionContext):
        result = await tool.execute(
            GeoRasterDataInput(action="inspect", name="dem"), context
        )
        assert not result.is_error
        assert "100 x 80" in result.output
        assert "EPSG:32647" in result.output

    @pytest.mark.asyncio
    async def test_stats(self, tool: GeoRasterDataTool, context: ToolExecutionContext):
        result = await tool.execute(
            GeoRasterDataInput(action="stats", name="dem", band=1), context
        )
        assert not result.is_error
        assert "4139" in result.output

    @pytest.mark.asyncio
    async def test_missing_raster_connector(self, tool: GeoRasterDataTool):
        class NoRaster:
            def get_raster_connector(self):
                return None

        ctx = ToolExecutionContext(
            cwd=Path("/tmp"),
            metadata={"data_catalog": NoRaster()},
            hook_executor=None,
        )
        result = await tool.execute(GeoRasterDataInput(action="list"), ctx)
        assert result.is_error
        assert "raster" in result.output.lower()


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
        """Registry should contain the four native tools."""
        from geoharness.tools import build_geo_tool_registry

        registry = build_geo_tool_registry()
        tools = registry.list_tools()
        tool_names = {t.name for t in tools}

        assert {"geo_db_data", "geo_vector_data", "geo_raster_data",
                "geo_cartography"} <= tool_names

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
        """Registry should have 4 native + 5 whitelist = 9 tools
        (without MCP).
        """
        from geoharness.tools import build_geo_tool_registry

        registry = build_geo_tool_registry()
        tools = registry.list_tools()

        # 4 native + 5 whitelist = 9 (no MCP connected)
        assert len(tools) == 9

    def test_registry_with_mock_mcp(self):
        """Registry with mock MCP manager should register MCP tools."""
        from geoharness.tools import build_geo_tool_registry

        mock_mcp = MagicMock()
        # Mock list_tools to return some fake MCP tools
        mock_mcp.list_tools = MagicMock(return_value=[])

        registry = build_geo_tool_registry(mcp_manager=mock_mcp)
        tools = registry.list_tools()

        # Should still have at least the 9 base tools
        assert len(tools) >= 9
