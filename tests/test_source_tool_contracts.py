"""Focused checks for visible data locations/schema and one JSON object encoding."""

import json
from types import SimpleNamespace

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from pydantic import ValidationError
from shapely.geometry import Point, box

from geoharness.data.file_loader import FileLoader
from geoharness.tools.geo_analysis import GeoAnalysisInput, GeoAnalysisTool, SpatialSource
from geoharness.tools.geo_db_data import GeoDBDataTool
from geoharness.tools.geo_raster_data import GeoRasterDataTool
from geoharness.tools.geo_vector_data import GeoVectorDataTool


def test_one_json_encoding_is_accepted_without_weakening_source_validation():
    source = {"file_path": "returned.geojson", "crs": "EPSG:4326"}
    parsed = GeoAnalysisInput.model_validate({"action": "impact",
        "source": json.dumps(source), "reference": json.dumps(source), "zones": json.dumps(source)})
    assert parsed.source.file_path == parsed.reference.file_path == parsed.zones.file_path
    assert GeoAnalysisInput.model_json_schema()["properties"]["source"].get("$ref")
    for invalid in ("[]", "null", '"another string"', "{'file_path':'x'}",
                    json.dumps(json.dumps(source)), '{"file_path":"x", "unexpected":1}',
                    '{"file_path":"x","source_name":"also_x"}'):
        with pytest.raises(ValidationError):
            SpatialSource.model_validate(invalid)


def test_vector_load_and_inspect_expose_path_and_every_field(tmp_path):
    directory = tmp_path / "data"
    directory.mkdir()
    path = directory / "points.geojson"
    gpd.GeoDataFrame({f"field_{i}": [i] for i in range(45)}, geometry=[Point(1, 2)],
                     crs=4326).to_file(path)
    loader = FileLoader(str(tmp_path))
    tool = GeoVectorDataTool()
    for result in (tool._load(loader, "points"), tool._inspect(loader, "points")):
        assert not result.is_error
        assert str(path.resolve()) in result.output and "Format: geojson" in result.output
        dtype = result.metadata["source_detail"]["schema"]["field_44"]
        assert f"field_44: {dtype}" in result.output and "nulls=0" in result.output
        assert "EPSG:4326" in result.output and "[1.0, 2.0, 1.0, 2.0]" in result.output
        assert len(result.metadata["source_detail"]["schema"]) == 46


def test_database_loaded_rows_expose_schema_and_table_locator():
    frame = gpd.GeoDataFrame({"time": ["t1"]}, geometry=[Point(1, 2)], crs=3857)
    connector = SimpleNamespace(query_geodataframe=lambda name, limit: frame)
    result = GeoDBDataTool()._load(connector, "events", 10)
    assert not result.is_error and "Table locator: events" in result.output
    assert "time:" in result.output and "EPSG:3857" in result.output
    assert "loaded rows, not the full table" in result.output


def test_csv_inspect_reports_columns_without_inventing_spatial_metadata(tmp_path):
    path = tmp_path / "attributes.csv"
    path.write_text("name,value\nA,12\nB,\n", encoding="utf-8")
    result = GeoVectorDataTool()._inspect(FileLoader(str(tmp_path)), "attributes")
    assert not result.is_error, result.output
    assert str(path) in result.output and "value: float64; nulls=1" in result.output
    assert "CRS: unknown" in result.output
    assert "Extent [xmin, ymin, xmax, ymax]: unknown" in result.output


def test_database_inspect_does_not_infer_crs_from_geometry_presence():
    from geoharness.data.postgis import PostGISConnector

    connector = PostGISConnector.__new__(PostGISConnector)
    connector.list_tables = lambda: ["metric"]
    connector._geometry_columns = lambda tables: {"metric": "geom"}
    connector.get_table_schema = lambda name: {"geom": "geometry"}
    connector.get_table_extent = lambda name: [500000, 3000000, 500100, 3000100]
    assert not connector.list_sources()[0].metadata["crs"]
    result = GeoDBDataTool()._inspect(connector, "metric")
    assert "CRS: unknown" in result.output and "EPSG:4326" not in result.output


def test_raster_stats_include_the_source_contract():
    header = {"path": "returned/dem.tif", "format": "GTiff", "crs": "EPSG:32647",
        "bbox": [0, 0, 30, 30], "width": 1, "height": 1, "band_count": 1,
        "dtype": "int16", "resolution": [30, 30], "nodata": -9999}
    loader = SimpleNamespace(get_source_detail=lambda name: header,
        band_stats=lambda name, band: {"band": band, "count": 1, "min": 1, "max": 1,
            "mean": 1, "std": 0, "p5": 1, "p50": 1, "p95": 1})
    result = GeoRasterDataTool()._stats(loader, "dem", 1)
    for text in ("returned/dem.tif", "GTiff", "EPSG:32647", "int16", "NoData: -9999"):
        assert text in result.output


async def test_json_encoded_source_runs_real_area_analysis(tmp_path):
    path = tmp_path / "polygon.geojson"
    gpd.GeoDataFrame(geometry=[box(500000, 3000000, 500100, 3000100)],
                     crs=32647).to_file(path)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": SimpleNamespace(
        analysis=SimpleNamespace(outputs_dir="analysis", metric_crs="EPSG:32647",
                                 timeout_seconds=20))})
    result = await GeoAnalysisTool().execute(GeoAnalysisInput.model_validate({"action": "area",
        "source": json.dumps({"source_type": "file", "file_path": str(path)})}), context)
    assert not result.is_error, result.output
    assert result.metadata["statistics"]["area_statistics"]["union_area_m2"] == pytest.approx(10000)


async def test_missing_vector_path_is_not_reported_as_a_permission_failure(tmp_path):
    from geoharness.execution.runner import execute_python

    result = await execute_python(code="import geopandas as gpd\ngpd.read_file(None)",
        name="missing-source", cwd=tmp_path, data_dir=None, outputs_dir=tmp_path / "runs",
        timeout_seconds=30, max_output_chars=4000)
    assert result["exit_code"] != 0
    assert "No vector source selected" in result["stderr"]
    assert "PermissionError" not in result["stderr"]
