"""User-facing contracts must describe the actual model and exported tables."""

from __future__ import annotations

import json
from types import SimpleNamespace

import geopandas as gpd
import pandas as pd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import LineString, Point, box

from geoharness.analysis.accessibility import AccessibilityContractError, _ids
from geoharness.tools.geo_accessibility import GeoAccessibilityInput, GeoAccessibilityTool
from geoharness.tools.geo_analysis import SpatialSource

CRS = "EPSG:32650"
X, Y = 500000, 3400000


def source(geometries, *, filters=None, **columns):
    frame = gpd.GeoDataFrame(columns, geometry=geometries, crs=CRS)
    return SpatialSource(geojson=frame.to_json(), crs=CRS, filters=filters or {})


async def run_comparison(tmp_path, hazard_filters):
    args = GeoAccessibilityInput(
        action="compare",
        objective="time",
        roads=source([LineString([(X, Y), (X + 200, Y)])], id=["road"]),
        residents=source(
            [Point(X + offset, Y) for offset in (35, 55, 155)],
            id=["direct", "buffer", "other"],
            name=["Direct", "Buffer", "Other"],
        ),
        shelters=source([Point(X + 190, Y)], id=["shelter"]),
        hazards=source(
            [box(X + 30, Y - 5, X + 40, Y + 5), box(X + 150, Y - 5, X + 160, Y + 5)],
            forecast=[1, 2],
            filters=hazard_filters,
        ),
        road_id_field="id",
        resident_id_field="id",
        shelter_id_field="id",
        direction_mode="bidirectional",
        speed_kmh=36,
        connector_speed_kmh=3,
        max_snap_distance_m=0,
        hazard_buffer_m=20,
    )
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={
            "geoharness_config": SimpleNamespace(
                analysis=SimpleNamespace(
                    outputs_dir="analysis",
                    metric_crs=CRS,
                    timeout_seconds=20,
                )
            )
        },
    )
    result = await GeoAccessibilityTool().execute(args, context)
    assert not result.is_error, result.output
    return result.metadata, json.loads(result.output)


async def test_selected_hazards_buffer_and_status_labels_describe_actual_model(tmp_path):
    manifest, receipt = await run_comparison(tmp_path, {"forecast": 1})
    selection = receipt["input_selection"]["hazards"]
    assert selection["selection_mode"] == "explicit_filters"
    assert selection["requested_filters"] == selection["applied_filters"] == {"forecast": 1}
    assert selection["feature_count"] == 1
    scope = receipt["hazard_scope"]
    assert scope["selected_feature_count"] == 1
    assert scope["hazard_buffer_m"] == 20
    assert scope["resident_intersects_selected_count"] == 1
    assert scope["resident_intersects_buffered_count"] == 2
    assert manifest["status_definitions"] == receipt["status_definitions"]
    assert "20 m" in receipt["status_definitions"]["inside_hazard"]["label_zh"]
    assert receipt["status_definitions"]["reachable"]["label_zh"] == "当前模型条件下可达"
    hazards = gpd.read_file(manifest["paths"]["hazard_result"]["geojson"])
    assert hazards.name.tolist() == ["Direct", "Buffer", "Other"]
    assert "gh_status_label_zh" in hazards
    assert hazards.gh_status_label_zh.str.contains("逃生|陷入火场").sum() == 0
    parameters = receipt["method_parameters"]
    assert parameters["speed_kmh"] == 36
    assert parameters["connector_speed_kmh"] == 3
    assert parameters["max_snap_distance_m"] == 0
    assert len(json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))) <= 12000


async def test_unfiltered_input_is_reported_as_union_not_a_selected_forecast(tmp_path):
    _, receipt = await run_comparison(tmp_path, {})
    selection = receipt["input_selection"]["hazards"]
    assert selection["selection_mode"] == "all_supplied_features"
    assert selection["feature_count"] == 2
    assert selection["applied_filters"] == {}
    assert receipt["hazard_scope"]["geometry_operation"] == "union_then_buffer"
    assert receipt["hazard_scope"]["resident_intersects_selected_count"] == 2
    assert receipt["hazard_scope"]["resident_intersects_buffered_count"] == 3


async def test_output_roles_schemas_and_time_fields_are_available_without_guessing(tmp_path):
    from geoharness.data.task_context import data_references, recent_outputs

    manifest, receipt = await run_comparison(tmp_path, {"forecast": 1})
    artifacts = receipt["artifacts"]
    assert artifacts["result"]["role"] == "resident_scenario_comparison"
    assert artifacts["baseline_result"]["scenario"] == "baseline"
    assert artifacts["hazard_result"]["scenario"] == "hazard"
    assert "gh_time_min" not in artifacts["result"]["schema"]
    assert "gh_baseline_time_min" in artifacts["result"]["schema"]
    assert "gh_hazard_time_min" in artifacts["result"]["schema"]
    assert artifacts["result"]["field_roles"]["join_key"] == "gh_resident_id"
    assert artifacts["result"]["field_roles"]["baseline_status_label_zh"] == (
        "gh_baseline_status_label_zh")
    assert artifacts["hazard_result"]["field_roles"]["time_minutes"] == "gh_time_min"
    assert "name" in artifacts["result"]["source_attribute_fields"]
    assert "gh_baseline_time_min" in manifest["paths"]["result"]["native_result_fields"]
    assert "access_tier" not in artifacts["hazard_result"]["schema"]
    assert "not shelter activation" in manifest["paths"]["hazard_shelters"]["meaning"]
    points = gpd.read_file(manifest["paths"]["result"]["geojson"])
    assert points.loc[0, "gh_baseline_time_min"] > 0
    assert pd.isna(points.loc[0, "gh_hazard_time_min"])
    config = SimpleNamespace(analysis=SimpleNamespace(outputs_dir="analysis"))
    records = recent_outputs(tmp_path, config, limit=32)
    refs = data_references({}, records)
    comparison = next(
        record
        for record in refs.values()
        if record.get("output_label") == "result" and record["path"].endswith(".geojson")
    )
    assert comparison["role"] == "resident_scenario_comparison"
    assert comparison["field_roles"]["baseline_time_minutes"] == "gh_baseline_time_min"
    assert "status_definitions" not in comparison


def test_float_identity_failure_reports_a_safe_explicit_conversion_path():
    frame = gpd.GeoDataFrame({"index": [1.0, 2.0]}, geometry=[Point(0, 0), Point(1, 0)])
    with pytest.raises(AccessibilityContractError) as error:
        _ids(frame, "index", "shelters")
    diagnostic = error.value.diagnostics
    assert diagnostic["id_field"] == "index"
    assert diagnostic["dtype"] == "float64"
    assert diagnostic["recovery"]["integral_float_values"] is True
    assert "collision" in diagnostic["recovery"]["instruction"]
    assert frame["index"].dtype == "float64"


def test_huge_analysis_contract_keeps_a_real_read_call_and_bounded_receipt():
    from geoharness.tools.geo_accessibility import _bounded_receipt

    payload = {"status": "succeeded", "manifest_path": "analysis/manifest.json",
        "inspect_contract_call": {"tool": "geo_python", "arguments": {
            "input_refs": {"manifest": "analysis/manifest.json"}, "code": "print('schema')"}},
        "quality_scope": "unverified", "artifacts": {"result": {
            "role": "resident_status", "schema": {"long_attribute_" + str(i): "str"
                                                    for i in range(4000)}}},
        "method_parameters": {"long_filter": "x" * 20000}, "assumptions": {"capacity": False}}
    result = _bounded_receipt(payload, 6000, "result")
    assert len(result) <= 6000
    assert json.loads(result)["inspect_contract_call"]["tool"] == "geo_python"
