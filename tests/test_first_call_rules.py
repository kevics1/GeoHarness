"""Small input-contract checks, without a model or map rendering."""

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point, box

from geoharness.config.settings import GeoConfig, QgisConfig
from geoharness.tools.geo_qgis_project import (
    GeoQgisProjectInput,
    GeoQgisProjectTool,
    QgisLabelStyle,
    QgisVectorStyle,
)


def test_rgba_is_normalized_and_bad_values_are_rejected():
    assert QgisVectorStyle(color="rgba(255,0,0,0.5)").color == "#80ff0000"
    assert QgisLabelStyle(buffer_color="rgba(0,0,0,0)").buffer_color == "#00000000"
    for value in ("rgba(999,0,0,1)", "rgba(0,0,0,nan)", "rgba(0,0,0,2)"):
        with pytest.raises(ValueError):
            QgisVectorStyle(color=value)


async def test_wrong_polygon_placement_is_diagnosed_without_native_launch(tmp_path):
    path = tmp_path / "polygon.geojson"
    gpd.GeoDataFrame({"name": ["area"]}, geometry=[box(0, 0, 1, 1)], crs=4326).to_file(path)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig(
        qgis=QgisConfig(enabled=False))})
    tool = GeoQgisProjectTool()
    result = await tool.execute(GeoQgisProjectInput(action="preflight", layers=[{
        "file_path": str(path), "label_field": "name",
        "label_style": {"placement": "over_point"}}]), context)
    assert result.is_error and "layers[0].label_style.placement" in result.output
    assert "Polygon" in result.output and "auto" in result.output
    assert not result.metadata["render_executed"]


async def test_placement_checks_only_the_selected_derivation(tmp_path):
    path = tmp_path / "mixed.geojson"
    gpd.GeoDataFrame({"name": ["point", "area"]}, geometry=[
        Point(0, 0), box(0, 0, 1, 1)], crs=4326).to_file(path)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig(
        qgis=QgisConfig(enabled=False))})
    checked = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="preflight", layers=[{"file_path": str(path), "label_field": "name",
            "label_style": {"placement": "around_point"},
            "derivation": {"feature_indices": [0]}}]), context)
    assert not checked.is_error, checked.output


def test_prepared_call_names_the_redundant_field():
    with pytest.raises(ValueError, match=r"extra fields \['title'\]"):
        GeoQgisProjectInput(action="create", plan_ref="qgis-plan:" + "a" * 64, title="redundant")


async def test_native_analysis_map_inputs_are_accepted_without_rewriting(tmp_path):
    from geoharness.config.settings import AnalysisConfig
    from geoharness.tools.geo_analysis import GeoAnalysisInput, GeoAnalysisTool

    source = gpd.GeoDataFrame({"time": ["t1"]}, geometry=[box(0, 0, 100, 100)], crs=3857)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig(
        qgis=QgisConfig(enabled=False), analysis=AnalysisConfig(metric_crs="EPSG:3857"))})
    analyzed = await GeoAnalysisTool().execute(GeoAnalysisInput(action="area", source={
        "geojson": source.to_json(), "crs": "EPSG:3857"}), context)
    assert not analyzed.is_error, analyzed.output
    inputs = analyzed.metadata["qgis_inputs"]
    checked = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="preflight", **inputs), context)
    assert not checked.is_error, checked.output
    assert checked.metadata["preflight"]["analysis_evidence_status"] == "verified"
