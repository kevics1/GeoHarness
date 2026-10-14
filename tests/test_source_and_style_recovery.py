"""Small checks for directory discovery failure and unambiguous style normalization."""

import json

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig, QgisConfig
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def test_none_discovery_path_reports_actual_cause_before_traceback(tmp_path):
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoPythonTool().execute(GeoPythonInput(
        code="import os\nroot = None\nos.path.join(root, 'missing.shp')"), context)
    assert result.is_error
    assert "NoneType" in result.metadata["error_summary"]
    assert "DATA_DIRS are allowed read roots" in result.output
    assert "read_vector" in result.output and "resolve_data" in result.output
    assert list(json.loads(result.output))[:3] == ["status", "error_summary", "recovery_hints"]


async def test_classification_is_nested_without_changing_values_and_records_the_move(tmp_path):
    source = tmp_path / "points.geojson"
    gpd.GeoDataFrame({"kind": ["a"]}, geometry=[Point(0, 0)], crs=3857).to_file(source)
    original = {"file_path": str(source), "renderer": "categorized", "field": "kind",
                "categories": [{"value": "a", "label": "A", "color": "#ff0000"}],
                "style": {"size_mm": 3}}
    args = GeoQgisProjectInput(action="preflight", layers=[original])
    style = args.layers[0].style
    assert style.renderer == "categorized" and style.field == "kind"
    assert style.categories[0].value == "a" and style.size_mm == 3
    assert "categories" in original and "categories" not in original["style"]
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig(
        qgis=QgisConfig(enabled=False))})
    result = await GeoQgisProjectTool().execute(args, context)
    assert not result.is_error, result.output
    assert result.metadata["preflight"]["parameter_normalizations"] == [
        {"layer_index": 0, "moved_to_style": ["renderer", "field", "categories"]}]
    assert "moved_to_style" in result.output


def test_conflicting_style_is_rejected_but_identical_duplicate_is_accepted():
    for outer, nested in (("single", "categorized"), ("categorized", "single")):
        with pytest.raises(ValueError, match="Conflicting 'renderer'"):
            GeoQgisProjectInput(action="preflight", layers=[{
                "file_path": "source.geojson", "renderer": outer,
                "style": {"renderer": nested}}])
    same = GeoQgisProjectInput(action="preflight", layers=[{
        "file_path": "source.geojson", "renderer": "single", "style": {"renderer": "single"}}])
    assert same.layers[0].style.renderer == "single"
    with pytest.raises(ValueError, match="Extra inputs"):
        GeoQgisProjectInput(action="preflight", layers=[{
            "file_path": "source.geojson", "invented_symbol_setting": "no"}])
