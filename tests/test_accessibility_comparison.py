"""Focused regression examples for common candidates and routing objectives."""

from __future__ import annotations

import json
from types import SimpleNamespace

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import LineString, Point, box

from geoharness.analysis.accessibility import (
    compute_accessibility,
    compute_accessibility_comparison,
)
from geoharness.tools.geo_accessibility import GeoAccessibilityInput, GeoAccessibilityTool
from geoharness.tools.geo_analysis import SpatialSource

CRS = "EPSG:32650"
X, Y = 500000, 3400000


def frame(geometries, ids=None):
    return gpd.GeoDataFrame(
        {"id": ids or [str(i) for i in range(len(geometries))]}, geometry=geometries, crs=CRS
    )


def line(points):
    return LineString([(X + x, Y + y) for x, y in points])


def inputs():
    roads = frame(
        [line([(0, 0), (100, 0), (200, 0)]), line([(0, 0), (0, 100), (200, 100), (200, 0)])]
    )
    residents, shelters = frame([Point(X, Y)]), frame([Point(X + 200, Y)])
    hazards = frame([box(X + 90, Y - 5, X + 110, Y + 5)])
    return roads, residents, shelters, hazards


def options(**kwargs):
    return {
        "road_id_field": "id",
        "resident_id_field": "id",
        "shelter_id_field": "id",
        "direction_mode": "bidirectional",
        "speed_kmh": 60,
        "connector_speed_kmh": 3.6,
        "max_snap_distance_m": 10,
        **kwargs,
    }


@pytest.mark.parametrize("objective", ["distance", "time"])
def test_compare_freezes_candidates_and_checks_deletion_only_objective(objective):
    roads, residents, shelters, hazards = inputs()
    result = compute_accessibility_comparison(
        roads, residents, shelters, hazards=hazards, **options(objective=objective)
    )
    comparison = result["comparison"]
    assert all(comparison["preconditions"].values())
    assert comparison["monotonicity"] == {"status": "passed", "violation_count": 0}
    points = result["artifacts"]["result"]
    assert points.gh_objective_delta.iloc[0] == pytest.approx(
        200 if objective == "distance" else 0.2
    )
    assert points.gh_baseline_status.iloc[0] == points.gh_hazard_status.iloc[0] == "reachable"
    candidates = result["artifacts"]["access_candidates"]
    assert candidates.gh_access_id.is_unique
    assert candidates.gh_baseline_allowed.all()
    assert not candidates.gh_hazard_allowed.all()
    # Separately invoked scenarios still prepare the same original candidate set.
    alone = compute_accessibility(
        roads, residents, shelters, hazards=hazards, **options(objective=objective)
    )
    assert alone["assumptions"]["candidate_set_sha256"] == comparison["candidate_set_sha256"]


def test_time_objective_changes_shelter_choice_without_claiming_shortest_distance():
    roads = frame([line([(0, 0), (1000, 0)])])
    residents = frame([Point(X, Y)])
    shelters = frame([Point(X + 100, Y + 100), Point(X + 500, Y)], ["walk", "road"])
    distance = compute_accessibility(roads, residents, shelters, **options(max_snap_distance_m=150))
    fastest = compute_accessibility(
        roads, residents, shelters, **options(max_snap_distance_m=150, objective="time")
    )
    route_d = distance["artifacts"]["routes"].iloc[0]
    route_t = fastest["artifacts"]["routes"].iloc[0]
    assert route_d.gh_shelter_id == "walk" and route_d.gh_distance_m == pytest.approx(200)
    assert route_t.gh_shelter_id == "road" and route_t.gh_time_min == pytest.approx(0.5)
    assert route_t.gh_time_min < route_d.gh_time_min
    assert route_t.gh_distance_m > route_d.gh_distance_m


def test_alternative_original_access_candidate_avoids_false_nearest_component_failure():
    roads = frame([line([(0, 0), (100, 0)]), line([(150, 0), (300, 0)])])
    residents, shelters = frame([Point(X, Y)]), frame([Point(X + 300, Y)])
    result = compute_accessibility(roads, residents, shelters, **options(max_snap_distance_m=160))
    assert result["artifacts"]["result"].gh_status.tolist() == ["reachable"]
    assert result["artifacts"]["result"].gh_access_candidate_count.iloc[0] == 2
    assert result["artifacts"]["routes"].gh_distance_m.iloc[0] == pytest.approx(300)


def test_inserted_access_points_do_not_invent_crossing_junctions_or_terminal_shortcuts():
    roads = frame([line([(-100, 0), (100, 0)]), line([(0, -100), (0, 100)])])
    residents = frame([Point(X - 90, Y), Point(X, Y)], ["horizontal", "crossing"])
    shelters = frame([Point(X, Y + 90)])
    result = compute_accessibility(roads, residents, shelters, **options())
    states = result["artifacts"]["result"].set_index("id").gh_status.to_dict()
    assert states == {"horizontal": "network_unreachable", "crossing": "reachable"}
    assert result["statistics"]["safe_weak_components"] == 2


async def test_compare_tool_publishes_transition_candidates_and_direct_qgis_receipt(tmp_path):
    roads, residents, shelters, hazards = inputs()
    specs = {
        key: SpatialSource(geojson=value.to_json(), crs=CRS)
        for key, value in zip(
            ("roads", "residents", "shelters", "hazards"), (roads, residents, shelters, hazards)
        )
    }
    args = GeoAccessibilityInput(action="compare", **specs, **options())
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={
            "geoharness_config": SimpleNamespace(
                analysis=SimpleNamespace(
                    outputs_dir="analysis", metric_crs=CRS, timeout_seconds=20
                ),
            )
        },
    )
    output = await GeoAccessibilityTool().execute(args, context)
    assert not output.is_error, output.output
    receipt = json.loads(output.output)
    assert receipt["qgis_inputs"]["analysis_manifests"] == [output.metadata["manifest_path"]]
    assert {layer["name"] for layer in receipt["qgis_inputs"]["layers"]} == {
        "hazard_result",
        "hazard_routes",
        "hazard_shelters",
    }
    assert "access_candidates" in {layer["name"] for layer in output.metadata["layers"]}
    assert output.metadata["parameters"]["action"] == "compare"
    assert output.metadata["comparison"]["monotonicity"]["status"] == "passed"
    assert {"result", "baseline_result", "hazard_result", "access_candidates"} <= set(
        output.metadata["paths"]
    )
    points = gpd.read_file(output.metadata["paths"]["result"]["geojson"])
    assert points.gh_transition.tolist() == ["reachable -> reachable"]
    assert points.crs.to_epsg() == 4326
