"""Export-time PAL facts and packaged rebuild inputs stay tied to the delivered PNG."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from geoharness.config.settings import QgisConfig
from geoharness.qgis.discovery import child_environment, discover_qgis
from geoharness.qgis.project_worker import _layout_input_digest, _packaged_creation_spec
from geoharness.qgis.runner import execute_qgis


def test_creation_contract_does_not_bind_temporary_layout_inputs():
    payload = {"layers": [{"file_path": "source.geojson"}], "title": "draft"}
    assert _layout_input_digest(payload) == _layout_input_digest({**payload,
        "_creation_spec": {"parameters": {"layers": [{"file_path": "random/snapshot.gpkg"}]}}})


def test_packaged_creation_contract_keeps_notes_and_source_bindings_without_mutating_input():
    supplied = {"parameters": {"layers": [{"file_path": "random/snapshot.gpkg",
        "derivation": {"source": "original.geojson"}, "label_style": {"font_file": "font.ttc"}}],
        "layout": {"footer_text": "original notes", "layout_plan": {"runtime": "old"},
                   "font_file": "font.ttc"}},
        "source_bindings": [{"source": "original.geojson", "derivation": {"year": 2024}}],
        "analysis_manifests": ["analysis/report.json"]}
    unchanged = copy.deepcopy(supplied)
    result = _packaged_creation_spec(supplied,
        [{"kind": "vector", "file": "data/layer_0.gpkg",
          "label_style": {"font": {"packaged": True, "file": "assets/fonts/selected.ttc"}}}],
        {"font": {"packaged": True, "file": "assets/fonts/selected.ttc"}})
    assert supplied == unchanged
    layer = result["parameters"]["layers"][0]
    assert layer["file_path"] == "data/layer_0.gpkg" and layer["layer_name"] == "layer_0"
    assert "derivation" not in layer
    assert layer["label_style"]["font_file"] == "assets/fonts/selected.ttc"
    assert result["parameters"]["layout"] == {
        "footer_text": "original notes", "font_file": "assets/fonts/selected.ttc"}
    assert result["source_bindings"] == supplied["source_bindings"]
    assert result["analysis_manifests"] == supplied["analysis_manifests"]
    assert _packaged_creation_spec(None, [], {}) is None


def _case(tmp_path):
    import geopandas as gpd
    from shapely.geometry import Point, box

    settings = QgisConfig(outputs_dir=str(tmp_path / "native-output"))
    try:
        discover_qgis(settings, cwd=tmp_path)
    except (ValueError, RuntimeError, OSError) as exc:
        pytest.skip(f"An independent QGIS installation is required: {exc}")
    points = tmp_path / "points.geojson"
    body = tmp_path / "body.geojson"
    gpd.GeoDataFrame({"name": [f"目标{index}的长标签" for index in range(24)],
        "kind": ["red", "blue"] * 12}, geometry=[Point(1000, 1000)] * 24,
        crs=3857).to_file(points)
    gpd.GeoDataFrame({"name": ["narrow subject"]},
        geometry=[box(1050, 1050, 1052, 1090)], crs=3857).to_file(body)
    parameters = {"title": "真实导出标签与符号", "crs": "EPSG:3857", "dpi": 72,
        "map_extent": [500, 500, 1500, 1500], "layout": {"legend": False, "scale_bar": False},
        "layers": [
            {"file_path": str(points), "name": "classified", "label_field": "name", "style": {
                "renderer": "categorized", "field": "kind", "categories": [
                    {"value": "red", "color": "#d73027", "size_mm": 3},
                    {"value": "blue", "color": "#4575b4", "size_mm": 4}]}},
            {"file_path": str(points), "name": "hollow overlay", "style": {
                "renderer": "single", "color": "#ffff00", "fill_opacity": 0,
                "outline_color": "#222222", "size_mm": 6, "width_mm": 0.8}},
            {"file_path": str(body), "name": "narrow body", "style": {
                "renderer": "single", "width_mm": 1, "outline_color": "#222222"}},
        ]}
    return settings, parameters


def test_native_symbol_units_and_compound_marker_shapes_are_not_guessed(tmp_path):
    settings, _ = _case(tmp_path)
    runtime = discover_qgis(settings, cwd=tmp_path)
    worker = Path(__file__).parents[1] / "src/geoharness/qgis/worker.py"
    script = """
import json,os,runpy,sys
runpy.run_path(sys.argv[1],run_name='symbol_facts_fixture')
from qgis.core import Qgis,QgsApplication,QgsMarkerSymbol,QgsSimpleMarkerSymbolLayer
from geoharness.qgis.project_worker import _symbol_metadata
QgsApplication.setPrefixPath(os.environ['QGIS_PREFIX_PATH'],True)
app=QgsApplication([],False);app.initQgis()
symbol=QgsMarkerSymbol.createSimple({'name':'circle'})
symbol.setSize(7);symbol.setSizeUnit(Qgis.RenderUnit.MapUnits)
first=symbol.symbolLayer(0)
first.setStrokeWidth(.4);first.setStrokeWidthUnit(Qgis.RenderUnit.MapUnits)
single=_symbol_metadata(symbol)
symbol.appendSymbolLayer(QgsSimpleMarkerSymbolLayer())
compound=_symbol_metadata(symbol)
print(json.dumps({'single':single,'compound':compound}))
app.exitQgis()
"""
    response = subprocess.run([str(runtime.python_executable), "-c", script, str(worker)],
        env=child_environment(runtime, tmp_path / "symbol-runtime"),
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert response.returncode == 0, response.stderr
    values = json.loads(response.stdout)
    assert values["single"]["size"] == 7 and values["single"]["size_unit"] == "MapUnits"
    assert "size_mm" not in values["single"] and "width_mm" not in values["single"]
    assert values["compound"]["symbol_layer_count"] == 2
    assert "marker_shape" not in values["compound"]


async def test_actual_png_export_reports_pal_and_classified_hollow_symbols(tmp_path):
    settings, payload = _case(tmp_path)
    payload["layout"].update(legend=True, height_mm=297)
    creation = {"parameters": copy.deepcopy(payload),
        "source_bindings": [{"file_path": layer["file_path"], "temporal": {"year": 2024}}
                            for layer in payload["layers"]], "analysis_manifests": []}
    payload.update(_creation_spec=creation, output_format="pdf")
    created = await execute_qgis("project_create", payload, cwd=tmp_path,
                                settings=settings, read_roots=[tmp_path])
    assert created["status"] == "succeeded", created.get("error_summary")
    result = created["result"]
    facts = result["native_facts"]
    digest = hashlib.sha256(Path(result["preview_path"]).read_bytes()).hexdigest()
    assert facts["image_sha256"] == result["image_sha256"] == digest
    frame = facts["frames"]["main_map"]
    assert frame["source_indices"] == [2, 1, 0]
    assert frame["crs"] == "EPSG:3857"
    assert frame["focus_scope"] == "validated_current_layout_plan"
    assert frame["focus_bbox"] == result["layout_plan"]["frames"]["main_map"]["focus_bbox"]
    assert len(frame["points"]) == 48
    classified = [point["symbol"] for point in frame["points"] if point["layer_index"] == 0]
    assert {symbol["color"] for symbol in classified} == {"#d73027", "#4575b4"}
    assert {symbol["size_mm"] for symbol in classified} == {3, 4}
    overlay = next(point["symbol"] for point in frame["points"] if point["layer_index"] == 1)
    assert overlay["color_alpha"] == overlay["fill_opacity"] == 0
    assert overlay["outline_alpha"] == overlay["layer_opacity"] == 1
    assert overlay["marker_shape"] == "circle"
    assert overlay["width_mm"] == pytest.approx(0.8)
    labels = frame["labels"]
    assert labels and all(label["layer_index"] == 0 for label in labels)
    assert any(label["is_unplaced"] for label in labels)
    assert any(not label["is_unplaced"] for label in labels)
    assert {label["feature_id"] for label in labels} <= {
        point["feature_id"] for point in frame["points"] if point["layer_index"] == 0}
    assert all(len(label["bbox"]) == 4 and label["text"].startswith("目标") for label in labels)
    body = frame["layer_bboxes"][0]
    assert body["layer_index"] == 2 and body["minimum_span_mm"] < 1
    manifest = json.loads(Path(result["project_manifest"]).read_text(encoding="utf-8"))
    assert manifest["native_facts"] == facts
    assert manifest["creation_spec"]["source_bindings"] == creation["source_bindings"]
    assert all(layer["file_path"].startswith("data/")
               for layer in manifest["creation_spec"]["parameters"]["layers"])
    assert manifest["automatic_repair_eligible"] is True
    legend = result["layout"]["legends"]["legend"]
    assert legend["scope"] == "actual_legend_node_model; not_pixel_legibility"
    assert legend["truncated"] is False and legend["row_count"] == 4
    assert len(legend["rect_mm"]) == 4 and legend["layout_unit"] == "Millimeters"
    classes = [row for row in legend["rows"] if row["layer_index"] == 0]
    assert {row["text"] for row in classes} == {"red", "blue"}
    assert {row["symbol"]["color"] for row in classes} == {"#d73027", "#4575b4"}
    assert next(layer["symbolnode_count"] for layer in legend["layers"]
                if layer["layer_index"] == 0) == 2


async def test_explicit_font_paths_are_packaged_and_old_export_has_no_rebuild_contract(tmp_path):
    settings, payload = _case(tmp_path)
    installed = Path(os.environ.get("WINDIR", "")) / "Fonts/msyh.ttc"
    if not installed.is_file():
        pytest.skip("An installed static CJK font is required for this fixture")
    configured = tmp_path / "selected.ttc"
    shutil.copyfile(installed, configured)
    payload["layout"]["font_file"] = str(configured)
    payload["layers"][0]["label_style"] = {"font_file": str(configured)}
    payload["analysis_facts"] = {"status": "verified", "summary": {"feature_count": 24}}
    payload["_creation_spec"] = {"parameters": copy.deepcopy(payload),
        "source_bindings": [{"file_path": layer["file_path"]} for layer in payload["layers"]],
        "analysis_manifests": []}
    created = await execute_qgis("project_create", payload, cwd=tmp_path,
                                settings=settings, read_roots=[tmp_path])
    assert created["status"] == "succeeded", created.get("error_summary")
    result = created["result"]
    parameters = result["creation_spec"]["parameters"]
    assert parameters["layout"]["font_file"].startswith("assets/fonts/")
    assert parameters["layers"][0]["label_style"]["font_file"].startswith("assets/fonts/")
    exported = await execute_qgis("project_export", {"project_path": result["project_file"]},
        cwd=tmp_path, settings=settings, read_roots=[tmp_path])
    assert exported["status"] == "succeeded", exported.get("error_summary")
    assert exported["result"]["creation_spec"] is None
    assert exported["result"]["automatic_repair_eligible"] is False
    assert "focus_scope" not in exported["result"]["native_facts"]["frames"]["main_map"]
    assert exported["result"]["analysis"] == {"status": "historical_unverified",
        "original_status": "verified", "current_export_verified": False,
        "summary": {"feature_count": 24}}
    assert exported["result"]["native_facts"]["image_sha256"] == exported["result"]["image_sha256"]


async def test_saved_non_mm_layout_export_does_not_claim_mm_frame_measurements(tmp_path):
    settings, payload = _case(tmp_path)
    created = await execute_qgis("project_create", payload, cwd=tmp_path,
                                settings=settings, read_roots=[tmp_path])
    assert created["status"] == "succeeded", created.get("error_summary")
    runtime = discover_qgis(settings, cwd=tmp_path)
    script = """
import os,sys
from qgis.core import Qgis,QgsApplication,QgsProject
QgsApplication.setPrefixPath(os.environ['QGIS_PREFIX_PATH'],True)
app=QgsApplication([],False);app.initQgis()
project=QgsProject.instance();assert project.read(sys.argv[1])
layout=project.layoutManager().printLayouts()[0]
layout.setUnits(Qgis.LayoutUnit.Centimeters)
assert project.write()
project.clear();app.exitQgis()
"""
    response = subprocess.run([str(runtime.python_executable), "-c", script,
        created["result"]["project_file"]],
        env=child_environment(runtime, tmp_path / "layout-units"),
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert response.returncode == 0, response.stderr
    exported = await execute_qgis("project_export",
        {"project_path": created["result"]["project_file"], "dpi": 72},
        cwd=tmp_path, settings=settings, read_roots=[tmp_path])
    assert exported["status"] == "succeeded", exported.get("error_summary")
    frame = exported["result"]["native_facts"]["frames"]["main_map"]
    assert frame["layout_unit"] == "Centimeters"
    assert frame["rect_mm"] is None and len(frame["rect_scene"]) == 4
    assert all(item["minimum_span_mm"] is None for item in frame["layer_bboxes"])
    assert exported["result"]["automatic_repair_eligible"] is False
