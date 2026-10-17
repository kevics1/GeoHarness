"""Real native candidates retain declared stable identities and must be re-preflighted."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point, box

from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.qgis.layout_candidates import (
    apply_candidate,
    effective_layout_patches,
    subject_fit_patches,
)
from geoharness.qgis.specification import load_spec
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def test_missing_identities_or_insufficient_source_counts_have_no_layout_fix():
    base = {"frame_id": "detail_map", "valid": False, "rendered": True,
            "selected_features": 2, "minimum_visible_features": 3,
            "selected_bounds": [0, 0, 10, 10]}
    for extra in ({}, {"selected_features": 3, "missing_keys": [9]},
                  {"selected_features": 3, "duplicate_keys": [1]}):
        assert subject_fit_patches({"checks": [{**base, **extra}]}, {},
                                  minimum_span=1, padding=0) == []


def test_fit_patches_keep_all_selected_bounds_and_do_not_mutate_requirements():
    layout = {"detail_frame": {"focus_layers": [1], "requirements": [
        {"layer_index": 2, "minimum_visible_features": 3, "feature_keys": ["a", "b", "c"]}]}}
    checks = [{"frame_id": "detail_map", "valid": False, "rendered": True,
               "selected_features": 3, "minimum_visible_features": 3,
               "selected_bounds": [100, 200, 300, 400]}]
    patches = subject_fit_patches({"checks": checks}, layout, minimum_span=1, padding=0.1)
    result = apply_candidate({"layout": layout}, patches)
    assert result["layout"]["detail_extent"] == {"xmin": 80, "ymin": 180,
                                                 "xmax": 320, "ymax": 420}
    assert "focus_layers" in layout["detail_frame"]
    assert (result["layout"]["detail_frame"]["requirements"]
            == layout["detail_frame"]["requirements"])


def test_candidate_replay_keeps_native_adaptations_before_failure():
    original = {"height_mm": 210, "footer_height_mm": 18,
                "label_table": {"height_mm": 15, "placement": "auto"}}
    effective = {**original, "height_mm": 245, "footer_height_mm": 30,
                 "label_table": {"height_mm": 35, "placement": "pages"}}
    patches = effective_layout_patches(original, effective)
    replay = apply_candidate({"layout": original}, patches)
    assert replay["layout"] == effective
    assert original["height_mm"] == 210


async def test_native_subject_candidates_are_measured_and_bound_to_the_failed_spec(tmp_path):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f"QGIS unavailable: {exc}")
    city, fire, points = (tmp_path / name for name in ("city.gpkg", "fire.gpkg", "points.gpkg"))
    gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 10000, 10000)], crs=3857).to_file(city)
    gpd.GeoDataFrame({"id": [1]}, geometry=[box(1000, 1000, 2000, 2000)], crs=3857).to_file(fire)
    gpd.GeoDataFrame({"id": [1, 2, 3, 4]}, geometry=[Point(1100, 1500), Point(1600, 1500),
        Point(3500, 1500), Point(9500, 9500)], crs=3857).to_file(points)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    tool = GeoQgisProjectTool()
    draft = await tool.execute(GeoQgisProjectInput(action="spec", crs="EPSG:3857", dpi=36,
        layers=[{"file_path": str(city), "layer_key": "city"},
                {"file_path": str(fire), "layer_key": "fire"},
                {"file_path": str(points), "layer_key": "places"}],
        layout={"legend": False, "scale_bar": False,
            "main_frame": {"focus_layers": ["city"]},
            "detail_frame": {"focus_layers": ["fire"], "purpose": "selected subjects",
                "requirements": [{"layer_index": "fire", "coverage": "complete"},
                    {"layer_index": "places", "coverage": "complete", "key_field": "id",
                     "feature_keys": [1, 2, 3], "minimum_visible_features": 3}]}}), context)
    assert not draft.is_error, draft.output
    reference = draft.metadata["spec_ref"]
    failed = await tool.execute(GeoQgisProjectInput(action="preflight", spec_ref=reference),
                                context)
    assert failed.is_error, failed.output
    candidates = failed.metadata["diagnostics"]["candidates"]
    passed = [item for item in candidates if item["validation_status"] == "native_preflight_passed"]
    assert passed and len(candidates) <= 3, failed.output
    assert all(item["subjects_valid"] for item in passed)
    assert not list(tmp_path.rglob("*.png"))
    calls = failed.metadata["candidate_edit_calls"]
    assert calls[0]["base_spec_ref"] == reference
    assert calls[0]["base_input_digest"] == passed[0]["base_input_digest"]
    original = load_spec(reference, cwd=tmp_path, settings=config.qgis)
    assert original["layout"]["detail_frame"]["focus_layers"] == ["fire"]
    edited = await tool.execute(GeoQgisProjectInput(**calls[0]["edit_call"]), context)
    assert not edited.is_error, edited.output
    checked = await tool.execute(GeoQgisProjectInput(action="preflight",
        spec_ref=edited.metadata["spec_ref"]), context)
    assert not checked.is_error, checked.output
    created = await tool.execute(GeoQgisProjectInput(**json.loads(checked.output)["create_call"]),
                                 context)
    assert not created.is_error, created.output
    manifest = json.loads(Path(created.metadata["project_manifest"]).read_text(encoding="utf-8"))
    detail = [item for item in manifest["frame_checks"]["subjects"]["checks"]
              if item["frame_id"] == "detail_map" and item["layer_key"] == "places"][0]
    assert detail["selected_features"] == detail["complete_features"] == 3
    assert detail["feature_keys"] == [1, 2, 3]
