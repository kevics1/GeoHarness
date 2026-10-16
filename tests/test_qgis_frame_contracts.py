"""Per-frame business subjects, bounded zoom preferences and real detail pages."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point, box

from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.qgis.frame_contracts import evaluate_requirements, joint_paper_proposal
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def test_subject_contracts_do_not_accept_union_or_partial_coverage():
    measured = {"frame_id": "detail_map", "layer_index": 1, "selected_features": 1,
                "visible_features": 1, "complete_features": 0, "coverage": "complete"}
    assert not evaluate_requirements([measured])["valid"]
    assert evaluate_requirements([{**measured, "coverage": "intersects"}])["valid"]
    assert not evaluate_requirements([{**measured, "complete_features": 1,
                                      "missing_keys": ["fire"]}])["valid"]
    regions = {"detail_map": [0, 0, 60, 80], "main_map": [0, 0, 208, 150]}
    assert joint_paper_proposal({"legend": False}, regions, 0.33, 3) is None
    assert joint_paper_proposal({"legend": False, "auto_expand_max_ratio": 1.1},
                                regions, 1, 3) is None


def _case(tmp_path):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f"QGIS unavailable: {exc}")
    city, fire, points = (tmp_path / name for name in ("city.gpkg", "fire.gpkg", "points.gpkg"))
    gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 10000, 10000)], crs=3857).to_file(city)
    gpd.GeoDataFrame({"id": [1]}, geometry=[box(1000, 1000, 2000, 2000)], crs=3857).to_file(fire)
    gpd.GeoDataFrame({"id": range(1, 7), "name": [f"居民点{i}" for i in range(1, 7)]},
        geometry=[Point(1100 + i * 100, 1500) for i in range(6)], crs=3857).to_file(points)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    return context, [{"file_path": str(city), "layer_key": "city", "name": "全域"},
                     {"file_path": str(fire), "layer_key": "fire", "name": "火场"},
                     {"file_path": str(points), "layer_key": "places", "name": "居民点"}]


async def test_native_detail_must_show_its_fire_even_when_main_shows_it(tmp_path):
    context, layers = _case(tmp_path)
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action="preflight",
        crs="EPSG:3857", layers=layers,
        layout={"legend": False, "scale_bar": False,
            "detail_extent": {"xmin": 7000, "ymin": 7000, "xmax": 9000, "ymax": 9000},
            "detail_frame": {"purpose": "火场详图", "requirements": [
                {"layer_index": "fire", "coverage": "complete"}]}}), context)
    assert result.is_error, result.output
    evidence = result.metadata["diagnostics"]
    assert evidence["code"] == "frame_subject_contract_failed"
    fire = evidence["subjects"]["checks"][0]
    assert fire["frame_id"] == "detail_map"
    assert fire["visible_features"] == 0 and fire["selected_features"] == 1


async def test_native_advisory_zoom_does_not_grow_paper_or_fail(tmp_path):
    context, layers = _case(tmp_path)
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action="preflight",
        crs="EPSG:3857", layers=layers,
        layout={"legend": False, "scale_bar": False, "width_mm": 297, "height_mm": 210,
            "detail_extent": {"xmin": 0, "ymin": 0, "xmax": 10000, "ymax": 10000},
            "detail_minimum_zoom": 10,
            "detail_zoom_policy": "advisory", "detail_frame": {"requirements": [
                {"layer_index": "fire", "coverage": "complete"}]}}), context)
    assert not result.is_error, result.output
    layout = result.metadata["preflight"]["layout"]
    assert layout["effective_parameters"]["width_mm"] == 297
    assert layout["effective_parameters"]["height_mm"] == 210
    assert layout["warnings"][0]["policy"] == "advisory"
    assert not layout["warnings"][0]["suggestions"][0]["feasible_with_public_dimensions"]
    assert result.metadata["preflight"]["frame_checks"]["subjects"]["valid"]
    detail = result.metadata["preflight"]["layout_plan"]["frames"]["detail_map"]
    assert detail["focus_scope"] == "declared_frame_requirements"
    assert detail["focus_bbox"] == [1000, 1000, 2000, 2000]


async def test_native_independent_detail_and_roster_pages_preserve_subjects(tmp_path):
    context, layers = _case(tmp_path)
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action="create",
        crs="EPSG:3857", layers=layers, dpi=36, output_format="pdf",
        layout={"legend": False, "scale_bar": False, "detail_placement": "page",
            "detail_minimum_zoom": 2,
            "main_frame": {"focus_layers": ["city"]},
            "detail_frame": {"purpose": "火场与选定居民点详图", "focus_layers": ["fire"],
                "requirements": [{"layer_index": "fire", "coverage": "complete"},
                                 {"layer_index": "places", "coverage": "complete",
                                  "key_field": "id", "feature_keys": [1, 2]}]},
            "label_table": {"layer_index": 2, "fields": ["id", "name"],
                            "placement": "pages", "columns": 2}}), context)
    assert not result.is_error, result.output
    manifest_path = Path(result.metadata["project_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["layout"]["pages"] == 3
    assert manifest["layout"]["bounds_check"]["valid"]
    assert manifest["frame_checks"]["by_frame"]["detail_map"]["valid"]
    assert manifest["native_facts"]["frames"]["detail_map"]["page_index"] == 1
    tables = manifest["layout"]["label_tables"]
    assert {table["page"] for table in tables} == {2}
    assert sum(table["visible_rows"] for table in tables) == 6
    assert [item["page_index"] for item in manifest["native_facts"]["page_previews"]] == [0, 1, 2]
    assert all((manifest_path.parent / item["path"]).is_file()
               for item in manifest["native_facts"]["page_previews"])
    assert "detail_page_2.png" in manifest["files"]
    from geoharness.cartography.qgis_bridge import relocate_package

    moved = relocate_package({**result.metadata,
        "primary_map": result.metadata["preview_path"]}, tmp_path / "moved-project",
        context.metadata["geoharness_config"])
    moved_root = Path(moved["project_dir"])
    copied = json.loads((moved_root / "project.json").read_text(encoding="utf-8"))
    assert all((moved_root / item["path"]).is_file()
               for item in copied["native_facts"]["page_previews"])
    assert all(Path(item["path"]).is_relative_to(moved_root)
               for item in moved["native_facts"]["page_previews"])
