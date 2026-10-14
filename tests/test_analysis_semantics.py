"""Reference selection, containment and external distance bands are separate facts."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point, box

from geoharness.analysis.vector import analyze_vectors
from geoharness.cartography.facts import load_analysis_facts
from geoharness.tools.geo_analysis import GeoAnalysisInput, SpatialSource


def _case(tmp_path, when):
    assets = gpd.GeoDataFrame({"name": ["a", "b", "c", "d"]},
        geometry=[Point(50, 50), Point(80, 80), Point(120, 50), Point(160, 50)], crs=3857)
    reference = gpd.GeoDataFrame({"time": ["current", "forecast"]},
        geometry=[box(0, 0, 10, 10), box(0, 0, 100, 100)], crs=3857)
    return analyze_vectors(GeoAnalysisInput(action="distance", distance_breaks_m=[50],
        source=SpatialSource(geojson=assets.to_json(), crs="EPSG:3857"),
        reference=SpatialSource(geojson=reference.to_json(), crs="EPSG:3857",
                                filters={"time": when})),
        cwd=tmp_path, outputs_dir="analysis", metric_crs="EPSG:3857")


def test_selected_forecast_has_two_inside_one_external_near_and_one_far(tmp_path):
    manifest = _case(tmp_path, "forecast")
    frame = gpd.read_file(manifest["paths"]["result"]["geojson"])
    assert frame.reference_relation.tolist() == ["covered_by", "covered_by", "outside", "outside"]
    assert frame.distance_class.tolist() == [
        "covered_by", "covered_by", "outside_0_50m", "outside_gt_50m"]
    assert manifest["reference_scope"]["filters"] == {"time": "forecast"}
    assert manifest["reference_scope"]["feature_count"] == 1
    assert manifest["statistics"]["relation_counts"]["covered_by"] == 2
    facts = load_analysis_facts([manifest["manifest_path"]], cwd=tmp_path, read_roots=[tmp_path],
        mapped_sources=[{"file_path": manifest["paths"]["result"]["geojson"]}],
        display_crs="EPSG:3857")
    assert facts["analyses"][0]["statistics"]["relation_counts"]["covered_by"] == 2


def test_current_reference_cannot_inherit_forecast_containment(tmp_path):
    assert _case(tmp_path, "current")["statistics"]["relation_counts"]["covered_by"] == 0


def test_unknown_filter_value_fails_without_silently_using_all_times(tmp_path):
    with pytest.raises(ValueError, match="reference.*no features"):
        _case(tmp_path, "missing")


def test_classification_manifest_is_not_allowed_to_lie_about_containment(tmp_path):
    manifest = _case(tmp_path, "forecast")
    manifest["statistics"]["relation_counts"]["covered_by"] = 4
    Path(manifest["manifest_path"]).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="relation|classification"):
        load_analysis_facts([manifest["manifest_path"]], cwd=tmp_path, read_roots=[tmp_path],
            mapped_sources=[{"file_path": manifest["paths"]["result"]["geojson"]}],
            display_crs="EPSG:3857")


def test_vector_inspection_reports_actual_string_dtype_and_nulls(tmp_path):
    from geoharness.data.file_loader import FileLoader

    path = tmp_path / "assets.geojson"
    gpd.GeoDataFrame({"longitude": ["101.2", None], "value": [2, 3]},
        geometry=[Point(101.2, 28), Point(101.3, 28)], crs=4326).to_file(path)
    loader = FileLoader(str(tmp_path))
    detail = loader._read_file_info(path)
    assert detail["schema"]["longitude"] in {"object", "str", "string"}
    assert detail["null_counts"]["longitude"] == 1
    assert detail["schema"]["value"].startswith("int")
