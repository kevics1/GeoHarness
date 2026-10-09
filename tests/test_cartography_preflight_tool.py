"""Tool-boundary preflight: reject invalid maps before versions or rendering."""

import json

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig, MapReviewConfig, QgisConfig
from geoharness.tools.geo_cartography import GeoCartographyInput, GeoCartographyTool
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def setup(tmp_path):
    source = tmp_path / "points.geojson"
    gpd.GeoDataFrame({"value": ["1.5", "2.5"], "name": ["A", "B"]},
                     geometry=[Point(114, 30), Point(114.1, 30.1)], crs=4326).to_file(source)
    config = GeoConfig(qgis=QgisConfig(enabled=False), map_review=MapReviewConfig(enabled=False))
    return source, ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})


@pytest.mark.parametrize("layer,extent", [
    ({"column": "missing"}, None),
    ({"expected_feature_count": 9}, None),
    ({}, [1, 1, 2, 2]),
])
async def test_invalid_map_rejected_without_version_or_files(tmp_path, monkeypatch, layer, extent):
    source, context = setup(tmp_path)

    def cannot_render(*args, **kwargs):
        pytest.fail("Invalid map reached renderer")

    monkeypatch.setattr("geoharness.cartography.render_layers", cannot_render)
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        layers=json.dumps([{"file_path": str(source), **layer}]),
        renderer="matplotlib", map_crs="EPSG:4326", map_extent=extent,
    ), context)
    assert result.is_error and "preflight" in result.output
    assert not context.metadata.get("map_workflow_state", {}).get("maps")
    assert not list(tmp_path.glob("**/*.png"))
    assert not (tmp_path / "exports").exists()


async def test_readonly_preflight_coerces_copy_without_backend_or_artifacts(tmp_path):
    source, context = setup(tmp_path)
    original = source.read_bytes()
    arguments = GeoCartographyInput(action="preflight", file_path=str(source),
                                    field_name="value", analysis_type="graduated")
    # Explicit style selects numeric rendering independently of configured bridge rules.
    arguments = arguments.model_copy(update={"layers": json.dumps([
        {"file_path": str(source), "column": "value", "render_method": "graduated"},
    ])})
    tool = GeoCartographyTool()
    result = await tool.execute(arguments, context)
    assert not result.is_error, result.output
    report = result.metadata["preflight"]
    assert report["layers"][0]["converted_fields"] == ["value"]
    assert report["layers"][0]["visible_main_feature_count"] == 2
    assert source.read_bytes() == original
    assert tool.is_read_only(arguments)
    assert not (tmp_path / ".geoharness").exists()
    assert not (tmp_path / "exports").exists()


async def test_native_readonly_preflight_without_qgis(tmp_path):
    source, context = setup(tmp_path)
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="preflight", layers=[{"file_path": str(source)}],
    ), context)
    assert not result.is_error, result.output
    assert result.metadata["render_executed"] is False
    assert not context.metadata.get("map_workflow_state")
    assert not (tmp_path / ".geoharness").exists()


async def test_same_deterministic_preflight_error_is_not_executed_again(tmp_path, monkeypatch):
    source, context = setup(tmp_path)
    tool = GeoCartographyTool()
    count = 0
    original = tool._preflight_blocking

    def counted(*args):
        nonlocal count
        count += 1
        return original(*args)

    monkeypatch.setattr(tool, "_preflight_blocking", counted)
    arguments = GeoCartographyInput(action="preflight", layers=json.dumps([
        {"file_path": str(source), "column": "missing", "render_method": "categorized"},
    ]))
    first = await tool.execute(arguments, context)
    second = await tool.execute(arguments, context)
    assert first.is_error and second.is_error
    assert count == 1
    assert second.metadata["duplicate_error_blocked"] is True
    frame = gpd.read_file(source)
    frame["missing"] = "new"
    frame.to_file(source, driver="GeoJSON")
    third = await tool.execute(arguments, context)
    assert not third.is_error, third.output
    assert count == 2


async def test_native_numeric_string_rejected_before_qgis_or_budget(tmp_path, monkeypatch):
    source, context = setup(tmp_path)
    context.metadata["geoharness_config"] = GeoConfig()

    async def cannot_run(*args, **kwargs):
        pytest.fail("Invalid native data reached QGIS")

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", cannot_run)
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="create", layers=[{"file_path": str(source), "style": {
            "renderer": "graduated", "field": "value",
            "ranges": [{"lower": 0, "upper": 3, "color": "#ff0000"}],
        }}],
    ), context)
    assert result.is_error and "numeric" in result.output
    assert not context.metadata.get("map_workflow_state")


async def test_native_layer_index_selector_is_preserved(tmp_path):
    source, context = setup(tmp_path)
    package = tmp_path / "multiple.gpkg"
    gpd.read_file(source).iloc[:1].to_file(package, layer="first", driver="GPKG")
    gpd.read_file(source).to_file(package, layer="second", driver="GPKG")
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="preflight", layers=[{"file_path": str(package) + "|layerid=1",
                                    "expected_feature_count": 2}],
    ), context)
    assert not result.is_error, result.output
    assert result.metadata["preflight"]["layers"][0]["feature_count"] == 2


@pytest.mark.parametrize("output_format", ["html", "svg"])
async def test_non_native_output_cannot_claim_a_detail_window(tmp_path, output_format):
    source, context = setup(tmp_path)
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        file_path=str(source), output_format=output_format, map_crs="EPSG:4326",
        map_extent=[1, 1, 2, 2], qgis_layout={"detail_extent": {
            "xmin": 113, "ymin": 29, "xmax": 115, "ymax": 31,
        }},
    ), context)
    assert result.is_error and "QGIS PNG/PDF" in result.output
    assert not context.metadata.get("map_workflow_state", {}).get("maps")
