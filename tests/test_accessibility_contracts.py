"""Small independent examples for routing, identity and publication contracts."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from pydantic import ValidationError
from shapely.geometry import LineString, Point, box

from geoharness.analysis.accessibility import AccessibilityContractError, compute_accessibility
from geoharness.tools.geo_accessibility import GeoAccessibilityInput, GeoAccessibilityTool
from geoharness.tools.geo_analysis import SpatialSource

CRS = "EPSG:32650"
X, Y = 500000, 3400000


def point(x, y=0):
    return Point(X + x, Y + y)


def line(coordinates):
    return LineString([(X + x, Y + y) for x, y in coordinates])


def frame(geometries, ids=None, **attrs):
    return gpd.GeoDataFrame(
        {"id": ids or [str(i) for i in range(len(geometries))], **attrs},
        geometry=geometries,
        crs=CRS,
    )


def compute(roads, residents, shelters, **kwargs):
    options = {
        "road_id_field": "id",
        "resident_id_field": "id",
        "shelter_id_field": "id",
        "direction_mode": "bidirectional",
        "speed_kmh": 36,
        "connector_speed_kmh": 3.6,
        "max_snap_distance_m": 30,
    }
    options.update(kwargs)
    return compute_accessibility(roads, residents, shelters, **options)


def test_segment_projection_route_includes_both_connectors_and_preserves_ids():
    roads = frame([line([(0, 0), (100, 0), (200, 0)])])
    residents = frame([point(80, 10), point(20, 10)], ["b", "a"], name=["Beta", "Alpha"])
    shelters = frame([point(180, 20)], ["s"])
    result = compute(roads, residents, shelters)
    assets = result["artifacts"]["result"]
    routes = result["artifacts"]["routes"].set_index("gh_resident_id")
    assert assets.id.tolist() == ["b", "a"]
    assert assets.name.tolist() == ["Beta", "Alpha"]
    assert assets.geometry.to_wkb().tolist() == residents.geometry.to_wkb().tolist()
    route = routes.loc["a"]
    assert route.gh_distance_m == pytest.approx(190)
    assert route.gh_road_distance_m == pytest.approx(160)
    assert route.gh_connector_distance_m == pytest.approx(30)
    assert route.gh_time_min == pytest.approx(160 / 600 + 30 / 60)
    assert route.geometry.length == pytest.approx(route.gh_distance_m)
    assert tuple(route.geometry.coords[0]) == tuple(residents.loc[1].geometry.coords[0])
    assert tuple(route.geometry.coords[-1]) == tuple(shelters.loc[0].geometry.coords[0])
    assert result["assumptions"]["capacity_allocation"] is False


def test_direction_mapping_respects_source_coordinate_order_and_rejects_unknown():
    roads = frame([line([(0, 0), (200, 0)])], direction=["F"])
    residents, shelters = frame([point(180)]), frame([point(20)])
    result = compute(
        roads,
        residents,
        shelters,
        direction_mode="field",
        direction_field="direction",
        direction_values={"F": "forward"},
        max_snap_distance_m=0,
    )
    assert result["artifacts"]["result"].gh_status.tolist() == ["network_unreachable"]
    assert result["artifacts"]["routes"].empty
    reverse = compute(
        roads,
        residents,
        shelters,
        direction_mode="field",
        direction_field="direction",
        direction_values={"F": "reverse"},
        max_snap_distance_m=0,
    )
    assert reverse["artifacts"]["result"].gh_distance_m.tolist() == pytest.approx([160])
    with pytest.raises(AccessibilityContractError, match="every road direction"):
        compute(
            roads,
            residents,
            shelters,
            direction_mode="field",
            direction_field="direction",
            direction_values={"B": "both"},
        )


def test_z_network_requires_explicit_2d_source():
    roads = frame([LineString([(X, Y, 0), (X + 100, Y, 50)])])
    residents = frame([Point(X, Y + 10, 3)])
    shelters = frame([Point(X + 100, Y + 10, 7)])
    with pytest.raises(AccessibilityContractError, match='explicit 2D'):
        compute(roads, residents, shelters)


def test_hazard_removes_road_and_safe_detour_includes_endpoints():
    roads = frame([line([(0, 0), (100, 0)]), line([(0, 0), (0, 30), (100, 30), (100, 0)])])
    hazard = frame([box(X + 40, Y - 5, X + 60, Y + 5)])
    result = compute(roads, frame([point(0, -10)]), frame([point(100, -10)]), hazards=hazard)
    route = result["artifacts"]["routes"].iloc[0]
    assert result["statistics"]["hazard_blocked_segments"] == 1
    assert route.gh_road_distance_m == pytest.approx(160)
    assert route.gh_distance_m == pytest.approx(180)
    assert not route.geometry.intersects(hazard.geometry.iloc[0])


def test_every_failed_snap_has_a_reason_and_disconnected_network_is_reported():
    roads = frame([line([(0, 0), (100, 0)])])
    hazards = frame([box(X + 45, Y + 5, X + 55, Y + 15)])
    residents = frame([point(50, 10), point(50, 20), point(0, 100)], ["in", "blocked", "far"])
    result = compute(roads, residents, frame([point(100)]), hazards=hazards)
    states = result["artifacts"]["result"].set_index("id")
    assert states.gh_status.to_dict() == {
        "in": "inside_hazard",
        "blocked": "connector_blocked",
        "far": "snap_too_far",
    }
    assert states.gh_reason.str.len().min() > 0
    isolated = compute(
        frame([line([(0, 0), (20, 0)]), line([(100, 0), (120, 0)])]),
        frame([point(0)]),
        frame([point(120)]),
        max_snap_distance_m=0,
    )
    assert isolated["statistics"]["safe_weak_components"] == 2
    assert isolated["artifacts"]["result"].gh_status.tolist() == ["network_unreachable"]


def test_identity_geometry_and_metric_crs_contracts_fail_before_routing():
    roads = frame([line([(0, 0), (100, 0)])])
    with pytest.raises(AccessibilityContractError, match="unique"):
        compute(
            roads, frame([point(0), point(10)], ["duplicate", "duplicate"]), frame([point(100)])
        )
    with pytest.raises(AccessibilityContractError, match="projected metre"):
        compute(
            roads.to_crs(4326), frame([point(0)]).to_crs(4326), frame([point(100)]).to_crs(4326)
        )
    with pytest.raises(AccessibilityContractError, match="point geometry"):
        compute(roads, frame([box(X, Y, X + 1, Y + 1)]), frame([point(100)]))


async def test_tool_publishes_crs_safe_complete_manifest_and_returns_every_resident(tmp_path: Path):
    roads = frame([line([(0, 0), (200, 0)])])
    residents = frame([point(20, 10), point(20, 100)], ["near", "far"], name=["Near", "Far"])
    shelters = frame([point(180, 20)], ["s"], capacity=[20])
    sources = {}
    for label, data in (("roads", roads), ("residents", residents), ("shelters", shelters)):
        path = tmp_path / f"{label}.gpkg"
        data.to_file(path)
        sources[label] = SpatialSource(file_path=str(path))
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
    args = GeoAccessibilityInput(
        **sources,
        road_id_field="id",
        resident_id_field="id",
        shelter_id_field="id",
        direction_mode="bidirectional",
        speed_kmh=30,
        max_snap_distance_m=30,
    )
    result = await GeoAccessibilityTool().execute(args, context)
    assert not result.is_error, result.output
    manifest = json.loads(Path(result.metadata["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["status"] == "succeeded"
    assert manifest["producer"] == "geo_accessibility"
    assert manifest["assumptions"]["analysis_correctness"] == "unverified"
    assets = gpd.read_file(manifest["paths"]["result"]["geojson"])
    assert assets.crs.to_epsg() == 4326
    assert assets.id.tolist() == ["near", "far"]
    assert assets.gh_status.tolist() == ["reachable", "snap_too_far"]
    assert assets.name.tolist() == ["Near", "Far"]
    for entry in manifest["paths"].values():
        assert entry["state"] == "succeeded" and entry["validation_state"] == "passed"
        assert Path(entry["geojson"]).is_relative_to(tmp_path / "analysis")
    assert not list((tmp_path / "analysis").glob(".staging-*"))
    with pytest.raises(ValidationError, match="direction_field"):
        args.model_validate({**args.model_dump(), "direction_mode": "field"})
