"""Native spatial results and provenance from synthetic metric data."""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import geopandas as gpd
import pandas as pd
import pytest
from openharness.tools.base import ToolExecutionContext
from pydantic import ValidationError
from shapely.geometry import Point, Polygon, box

from geoharness.analysis import analyze_vectors
from geoharness.tools.geo_analysis import GeoAnalysisInput, GeoAnalysisTool, SpatialSource


@pytest.fixture
def context(tmp_path: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        cwd=tmp_path,
        metadata={"geoharness_config": SimpleNamespace(analysis=SimpleNamespace(
            outputs_dir="analysis", metric_crs="EPSG:32650", timeout_seconds=20,
        ))},
    )


def _source(geometries: list, properties: dict | None = None, crs: str = "EPSG:32650"):
    frame = gpd.GeoDataFrame(properties or {}, geometry=geometries, crs=crs)
    return SpatialSource(geojson=frame.to_json(), crs=crs)


def _result_frame(result):
    assert not result.is_error, result.output
    return gpd.read_file(result.metadata["paths"]["result"]["geojson"])


async def test_area_reports_time_groups_and_union_separately(context):
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(action="area", group_field="time",
        source=_source([box(500000,3400000,500100,3400100),
                        box(500000,3400000,500200,3400200)], {"time": ["t1", "t2"]})), context)
    frame = _result_frame(result)
    assert frame.area_m2.tolist() == pytest.approx([10000, 40000])
    areas = result.metadata["statistics"]["area_statistics"]
    assert areas["feature_area_sum_m2"] == pytest.approx(50000)
    assert areas["union_area_m2"] == pytest.approx(40000)
    assert areas["overlap_area_m2"] == pytest.approx(10000)
    assert [group["value"] for group in areas["groups"]] == ["t1", "t2"]
    assert [group["union_area_m2"] for group in areas["groups"]] == pytest.approx([10000, 40000])


async def test_area_rejects_nonpolygon_and_unknown_group(context):
    bad = await GeoAnalysisTool().execute(GeoAnalysisInput(action="area",
        source=_source([Point(500000,3400000)])), context)
    assert bad.is_error and "polygon" in bad.output
    missing = await GeoAnalysisTool().execute(GeoAnalysisInput(action="area", group_field="missing",
        source=_source([box(500000,3400000,500100,3400100)])), context)
    assert missing.is_error and "group_field" in missing.output


async def test_area_groups_deduplicate_overlaps_and_reject_reserved_field(context):
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(action="area", group_field="time",
        source=_source([box(500000,3400000,500100,3400100),
                        box(500050,3400000,500150,3400100),
                        box(500000,3400000,500100,3400100)],
                       {"time": ["t1", "t1", None]})), context)
    frame = _result_frame(result)
    assert frame.area_m2.tolist() == pytest.approx([10000, 10000, 10000])
    areas = result.metadata["statistics"]["area_statistics"]
    assert areas["feature_area_sum_m2"] == pytest.approx(30000)
    assert areas["union_area_m2"] == pytest.approx(15000)
    assert [group["value"] for group in areas["groups"]] == ["t1", None]
    assert [group["union_area_m2"] for group in areas["groups"]] == pytest.approx([15000,10000])
    conflict = await GeoAnalysisTool().execute(GeoAnalysisInput(action="area",
        source=_source([box(500000,3400000,500100,3400100)], {"area_m2": [99]})), context)
    assert conflict.is_error and "conflict" in conflict.output


async def test_area_preserves_datetime_groups_and_handles_empty_result(context):
    path = Path(context.cwd) / "dated.gpkg"
    gpd.GeoDataFrame({"time": [pd.Timestamp("2020-03-30T19:00:00+08:00")]},
        geometry=[box(500000,3400000,500100,3400100)], crs="EPSG:32650").to_file(path)
    source = SpatialSource(file_path=str(path))
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="area", source=source, group_field="time"), context)
    _result_frame(result)
    value = result.metadata["statistics"]["area_statistics"]["groups"][0]["value"]
    output = json.loads(Path(result.metadata["output_path"]).read_text(encoding="utf-8"))
    assert output["features"][0]["properties"]["time"] == value
    assert "+08:00" in value
    empty = await GeoAnalysisTool().execute(GeoAnalysisInput(action="area",
        source=SpatialSource(geojson={"type": "FeatureCollection", "features": []})), context)
    assert _result_frame(empty).empty
    assert empty.metadata["statistics"]["area_statistics"]["union_area_m2"] == 0


async def test_impact_multilayer_mixed_crs_and_zone_counts(context):
    assets = gpd.GeoDataFrame(
        {"asset": ["inside", "near", "outside"]},
        geometry=[Point(500000, 3400000), Point(500025, 3400000), Point(500100, 3400000)],
        crs="EPSG:32650",
    ).to_crs("EPSG:4326")
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="impact",
        source=SpatialSource(geojson=assets.to_json()),
        reference=_source([box(499990, 3399990, 500010, 3400010)]),
        zones=_source([
            box(499980, 3399980, 500040, 3400020),
            box(500020, 3399980, 500120, 3400020),
        ], {"zone": ["west", "east"]}),
        distance_m=20,
    ), context)
    frame = _result_frame(result)
    assert frame.distance_m.tolist() == pytest.approx([0, 15, 90], abs=1e-6)
    assert frame.affected.tolist() == [True, True, False]
    zones = gpd.read_file(result.metadata["paths"]["zones"]["geojson"])
    assert zones.total_count.tolist() == [2, 2]
    assert zones.affected_count.tolist() == [2, 1]
    assert result.metadata["statistics"] == {
        "source_count": 3, "feature_count": 3, "affected_count": 2, "unaffected_count": 1,
    }
    manifest = json.loads(Path(result.metadata["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["metric_crs"] == "EPSG:32650"
    assert manifest["export_crs"] == "EPSG:4326"
    assert "boundary" in manifest["count_semantics"] or "boundaries" in manifest["count_semantics"]
    assert "overlapping" in manifest["count_semantics"]
    assert manifest["sources"]["source"]["crs"] == "EPSG:4326"
    assert manifest["layers"][-1]["label"] == "result"
    assert all(Path(layer["file_path"]).exists() for layer in manifest["layers"])
    assert pd.read_csv(manifest["paths"]["result"]["csv"]).shape[0] == 3
    assert manifest["table_previews"]["zones"][0]["affected_count"] == 2
    assert manifest["table_previews"]["zones"][1]["affected_count"] == 1
    paths_text = next(line.removeprefix("Artifacts: ") for line in result.output.splitlines()
                      if line.startswith("Artifacts: "))
    assert json.loads(paths_text)["zones"]["csv"] == manifest["paths"]["zones"]["csv"]
    assert '"affected_count": 1' in result.output
    assert "Suggested layers:" in result.output


async def test_buffer_and_auto_utm_use_real_metres(context):
    context.metadata["geoharness_config"].analysis.metric_crs = ""
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="buffer", source=_source([Point(500000, 3400000)]), distance_m=100,
    ), context)
    frame = _result_frame(result).to_crs(result.metadata["metric_crs"])
    assert result.metadata["metric_crs"] == "EPSG:32650"
    assert frame.area.iloc[0] == pytest.approx(31415.9265, rel=0.002)
    assert frame.total_bounds[2] - frame.total_bounds[0] == pytest.approx(200, abs=1e-6)


async def test_count_boundary_and_overlapping_zones_are_explicit(context):
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="count",
        source=_source([Point(500000, 3400000), Point(500020, 3400000)]),
        zones=_source([
            box(499980, 3399980, 500020, 3400020),
            box(500020, 3399980, 500060, 3400020),
        ]),
    ), context)
    assert _result_frame(result).total_count.tolist() == [2, 1]
    assert "every matching zone" in result.metadata["count_semantics"]


async def test_distance_uses_geometry_edges_not_centroids(context):
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="distance",
        source=_source([box(500000, 3400000, 500020, 3400020)]),
        reference=_source([box(500030, 3400000, 500050, 3400020)]),
    ), context)
    assert _result_frame(result).distance_m.tolist() == pytest.approx([10])


async def test_intersection_preserves_properties_and_produces_known_area(context):
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="intersection",
        source=_source([box(500000, 3400000, 500020, 3400020)], {"asset": ["a"]}),
        reference=_source([box(500010, 3400000, 500030, 3400020)], {"hazard": ["h"]}),
    ), context)
    frame = _result_frame(result)
    assert frame.to_crs("EPSG:32650").area.tolist() == pytest.approx([200], abs=1e-6)
    assert frame.asset.tolist() == ["a"]
    assert frame.hazard.tolist() == ["h"]
    assert frame._gh_source_id.tolist() == [0]
    assert frame._gh_reference_id.tolist() == [0]


async def test_empty_clip_persists_valid_geojson_and_csv(context):
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="clip", source=_source([Point(500000, 3400000)], {"id": [1]}),
        reference=_source([box(500100, 3400100, 500200, 3400200)]),
    ), context)
    assert _result_frame(result).empty
    assert "Empty result persisted successfully" in result.output
    assert pd.read_csv(result.metadata["paths"]["result"]["csv"]).empty
    assert json.loads(Path(result.metadata["output_path"]).read_text())["features"] == []


async def test_relative_file_source_resolves_cwd_and_remains_unchanged(context, tmp_path):
    path = tmp_path / "assets.geojson"
    frame = gpd.GeoDataFrame(geometry=[Point(500000, 3400000)], crs="EPSG:32650")
    frame.to_file(path, driver="GeoJSON")
    original = path.read_bytes()
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="buffer", source=SpatialSource(file_path="assets.geojson"), distance_m=5,
        output_name="assets",
    ), context)
    assert not result.is_error, result.output
    assert path.read_bytes() == original
    assert result.metadata["sources"]["source"]["file_path"] == str(path.resolve())
    assert Path(result.metadata["output_path"]).is_relative_to(tmp_path / "analysis")


async def test_unknown_file_crs_requires_explicit_declaration(context, tmp_path):
    frame = gpd.GeoDataFrame(geometry=[Point(500000, 3400000)])
    path = tmp_path / "unknown.gpkg"
    frame.to_file(path, driver="GPKG")
    args = GeoAnalysisInput(
        action="buffer", source=SpatialSource(file_path=str(path)), distance_m=5
    )
    result = await GeoAnalysisTool().execute(args, context)
    assert result.is_error
    assert "unknown CRS" in result.output
    args.source.crs = "EPSG:32650"
    assert not (await GeoAnalysisTool().execute(args, context)).is_error


async def test_invalid_geometry_and_unknown_coordinate_system_are_rejected(context):
    invalid = Polygon([(500000, 3400000), (500020, 3400020),
                       (500000, 3400020), (500020, 3400000), (500000, 3400000)])
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="buffer", source=_source([invalid]), distance_m=5,
    ), context)
    assert result.is_error and "invalid geometry" in result.output
    # RFC 7946 cannot silently treat unknown projected coordinates as longitude/latitude.
    spec = _source([Point(500000, 3400000)])
    spec.crs = ""
    document = json.loads(spec.geojson)
    document.pop("crs", None)
    spec.geojson = document
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="buffer", source=spec, distance_m=5,
    ), context)
    assert result.is_error and "CRS domain" in result.output


@pytest.mark.parametrize("metric_crs", ["EPSG:4326", "EPSG:2263"])
async def test_non_metric_crs_rejected(context, metric_crs):
    context.metadata["geoharness_config"].analysis.metric_crs = metric_crs
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="buffer", source=_source([Point(500000, 3400000)]), distance_m=5,
    ), context)
    assert result.is_error and "metre units" in result.output


@pytest.mark.parametrize("distance", [-1, float("inf"), float("nan")])
def test_illegal_distance_rejected(distance):
    with pytest.raises(ValidationError):
        GeoAnalysisInput(
            action="buffer", source=_source([Point(500000, 3400000)]), distance_m=distance,
        )


@pytest.mark.parametrize("name", ["../escape", "..\\escape", "/outside", "C:\\outside", "NUL"])
async def test_output_name_cannot_escape_root(context, name):
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="buffer", source=_source([Point(500000, 3400000)]), distance_m=5,
        output_name=name,
    ), context)
    assert result.is_error and "plain file stem" in result.output
    assert not (Path(context.cwd) / "analysis").exists()


async def test_all_empty_inputs_can_use_explicit_metric_crs(context):
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="buffer",
        source=SpatialSource(geojson={"type": "FeatureCollection", "features": []}),
        distance_m=5,
    ), context)
    assert _result_frame(result).empty


async def test_timeout_prevents_worker_from_publishing(context, monkeypatch):
    from geoharness.analysis import vector

    original = vector._run_operation

    def slow(*args, **kwargs):
        time.sleep(0.08)
        return original(*args, **kwargs)

    monkeypatch.setattr(vector, "_run_operation", slow)
    context.metadata["geoharness_config"].analysis.timeout_seconds = 0.03
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(
        action="buffer", source=_source([Point(500000, 3400000)]), distance_m=5,
    ), context)
    assert result.is_error and "timed out" in result.output
    import asyncio

    await asyncio.sleep(0.12)
    assert not (Path(context.cwd) / "analysis").exists()


def test_cancelled_analysis_does_not_write(tmp_path):
    cancelled = Event()
    cancelled.set()
    with pytest.raises(TimeoutError):
        analyze_vectors(
            GeoAnalysisInput(action="buffer", source=_source([Point(0, 0)]), distance_m=5),
            cwd=tmp_path, outputs_dir="analysis", metric_crs="EPSG:32650", cancelled=cancelled,
        )
    assert not (tmp_path / "analysis").exists()
