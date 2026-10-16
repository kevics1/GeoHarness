"""Recorded failure patterns at the public draft and native evidence boundaries."""

import hashlib
import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.hooks.events import HookEvent
from openharness.hooks.executor import HookExecutionContext
from openharness.hooks.loader import HookRegistry
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.cartography.facts import infer_analysis_manifests, load_analysis_facts
from geoharness.config.settings import GeoConfig
from geoharness.hooks.executor import GeoHookExecutor
from geoharness.qgis.prepared import diagnostic_files
from geoharness.qgis.specification import apply_edits, canonical_spec, compile_references
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool, QgisStyleEdit


async def test_draft_can_be_inspected_and_edited_without_retyping_layers(tmp_path):
    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    tool = GeoQgisProjectTool()
    result = await tool.execute(GeoQgisProjectInput(action="spec", crs="EPSG:3857",
        layers=[{"file_path": "actual.geojson", "layer_key": "fire"}]), context)
    saved = json.loads(result.output)
    assert not result.is_error, result.output
    assert Path(saved["spec_path"]) in diagnostic_files(cwd=tmp_path, settings=config.qgis)
    inspected = await tool.execute(GeoQgisProjectInput(**saved["inspect_call"]), context)
    assert json.loads(inspected.output)["parameters"]["layers"][0]["layer_key"] == "fire"
    edited = await tool.execute(GeoQgisProjectInput(action="spec", spec_ref=saved["spec_ref"],
        edits=[{"operation": "style", "layer_key": "fire", "style": {"color": "#d73027"}},
               {"operation": "frame", "frame": "detail", "minimum_zoom": 3, "settings": {
                   "purpose": "Fire detail", "focus_layers": ["fire"],
                   "requirements": [{"layer_index": "fire", "coverage": "complete"}]}}]),
        context)
    assert not edited.is_error, edited.output
    current = await tool.execute(GeoQgisProjectInput(action="inspect",
        spec_ref=json.loads(edited.output)["spec_ref"]), context)
    parameters = json.loads(current.output)["parameters"]
    assert parameters["layers"][0]["file_path"] == "actual.geojson"
    assert parameters["layers"][0]["style"]["color"] == "#d73027"
    assert parameters["layout"]["detail_frame"]["focus_layers"] == ["fire"]
    assert parameters["layout"]["detail_zoom_policy"] == "advisory"


def test_stable_subject_references_survive_layer_reordering():
    original = canonical_spec({"layers": [
        {"layer_key": "roads", "file_path": "roads.gpkg"},
        {"layer_key": "fire", "file_path": "fire.gpkg"}],
        "layout": {"detail_frame": {"focus_layers": [1],
            "requirements": [{"layer_index": 1, "coverage": "complete"}]}}})
    original["layers"].reverse()
    compiled = compile_references(original)
    assert compiled["layout"]["detail_frame"]["focus_layers"] == [0]
    assert compiled["layout"]["detail_frame"]["requirements"][0]["layer_index"] == 0
    original["layout"]["detail_frame"]["focus_layers"] = ["guessed-layer-name"]
    with pytest.raises(ValueError, match="available layer_keys"):
        compile_references(original)


def test_small_style_and_frame_edits_preserve_prior_semantics():
    original = {"layers": [{"layer_key": "fire", "file_path": "fire.gpkg", "style": {
        "renderer": "categorized", "field": "time", "categories": [
            {"value": "one", "color": "#ffffff"}]}}],
        "layout": {"detail_extent": {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10},
                   "detail_frame": {"purpose": "Fire detail", "requirements": [
                       {"layer_index": "fire", "coverage": "complete"}]}}}
    updated = apply_edits(original, [
        {"operation": "style", "layer_key": "fire", "style": {"opacity": 0.8}},
        {"operation": "frame", "frame": "detail", "settings": {"focus_layers": ["fire"]}}])
    assert updated["layers"][0]["style"]["field"] == "time"
    assert updated["layout"]["detail_frame"]["requirements"][0]["coverage"] == "complete"
    assert "detail_extent" not in updated["layout"]
    GeoQgisProjectInput.model_validate({**updated, "action": "spec", "crs": "EPSG:3857"})
    partial = QgisStyleEdit(operation="style", layer_key="fire", style={"opacity": 0.5})
    assert partial.model_dump(exclude_unset=True)["style"] == {"opacity": 0.5}
    original["layers"][0]["label_style"] = {"size_pt": 8, "buffer_size_mm": 1}
    changed_labels = apply_edits(original, [{"operation": "labels", "layer_key": "fire",
        "label_field": "time"}])
    assert changed_labels["layers"][0]["label_style"] == {"size_pt": 8, "buffer_size_mm": 1}


def test_encoded_layers_and_bad_layer_values_keep_public_validation():
    value = GeoQgisProjectInput(action="spec", layers=json.dumps([
        {"file_path": "real.gpkg", "layer_key": "fire"}]),
        layout={"main_frame": {"focus_layers": ["fire"]}})
    assert value.layout.main_frame.focus_layers == [0]
    with pytest.raises(ValueError):
        GeoQgisProjectInput(action="spec", layers=[42])
    with pytest.raises(ValueError):
        GeoQgisProjectInput(action="spec", layers=[{"file_path": 42}])
    with pytest.raises(ValueError):
        GeoQgisProjectInput(action="spec", layers=[{"file_path": "a", "layer_key": []}])
    for value in (None, 42):
        with pytest.raises(ValueError):
            GeoQgisProjectInput(action="spec", layers=[{"file_path": "a"}],
                layout={"main_frame": {"requirements": value}})


async def test_help_returns_the_runtime_style_schema_without_processing_search(tmp_path):
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="help", section="style"), context)
    properties = json.loads(result.output)["schema"]["properties"]
    assert {"renderer", "field", "categories"} <= set(properties)
    assert "column" not in properties and "render_method" not in properties


async def test_bad_schema_gets_actionable_feedback_before_execution(tmp_path):
    executor = GeoHookExecutor(HookRegistry(), HookExecutionContext(
        cwd=tmp_path, api_client=None, default_model="test"))
    result = await executor.execute(HookEvent.PRE_TOOL_USE, {
        "tool_name": "geo_qgis_project", "tool_input": {"action": "preflight", "layers": [
            {"file_path": "real.gpkg", "color": "#ff0000"}]}})
    assert result.blocked
    feedback = json.loads(result.reason)
    assert feedback["executed"] is False
    assert feedback["errors"][0]["field"] == "layers.0.color"
    assert feedback["help_call"] == {"action": "help", "section": "style"}


def test_accessibility_evidence_is_bound_to_actual_mapped_native_outputs(tmp_path):
    frame = gpd.GeoDataFrame({"gh_resident_id": ["one", "two"],
                             "gh_status": ["reachable", "snap_too_far"]},
                            geometry=[Point(102, 28), Point(102.01, 28)], crs=4326)
    geojson, csv = tmp_path / "result.geojson", tmp_path / "result.csv"
    frame.to_file(geojson, driver="GeoJSON")
    frame.drop(columns="geometry").to_csv(csv, index=False)
    manifest = {"producer": "geo_accessibility", "action": "accessibility",
        "status": "succeeded", "metric_crs": "EPSG:32647", "export_crs": "EPSG:4326",
        "parameters": {}, "statistics": {"unverified_business_claim": 99}, "paths": {
            "result": {"geojson": str(geojson), "csv": str(csv), "feature_count": 2,
                "geojson_sha256": hashlib.sha256(geojson.read_bytes()).hexdigest(),
                "csv_sha256": hashlib.sha256(csv.read_bytes()).hexdigest()}}}
    shelters = frame.rename(columns={"gh_resident_id": "gh_shelter_id"}).copy()
    shelters["gh_status"] = "connected"
    shelter_json, shelter_csv = tmp_path / "shelters.geojson", tmp_path / "shelters.csv"
    shelter_json.write_text(shelters.to_json(), encoding="utf-8")
    shelters.drop(columns="geometry").to_csv(shelter_csv, index=False)
    manifest["paths"]["shelters"] = {"geojson": str(shelter_json), "csv": str(shelter_csv),
        "feature_count": 2, "geojson_sha256": hashlib.sha256(shelter_json.read_bytes()).hexdigest(),
        "csv_sha256": hashlib.sha256(shelter_csv.read_bytes()).hexdigest()}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    sources = [{"file_path": str(geojson), "gdf": frame}]
    assert infer_analysis_manifests(sources, cwd=tmp_path, read_roots=[tmp_path]) == [str(path)]
    facts = load_analysis_facts([str(path)], cwd=tmp_path, read_roots=[tmp_path],
                                mapped_sources=sources, display_crs="EPSG:4326")
    analysis = facts["analyses"][0]
    assert analysis["statistics"]["output_status_counts"]["result"]["status_counts"] == {
        "reachable": 1, "snap_too_far": 1}
    assert analysis["analysis_correctness"] == "unverified"
    assert "unverified_business_claim" not in analysis["statistics"]
    geojson.write_text("{}")
    with pytest.raises(ValueError, match="SHA256"):
        load_analysis_facts([str(path)], cwd=tmp_path, read_roots=[tmp_path],
                            mapped_sources=sources, display_crs="EPSG:4326")
