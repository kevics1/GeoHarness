"""Short create references preserve real preflight and exact readable diagnostics."""

from dataclasses import replace
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def test_real_preflight_short_create_and_readable_report(tmp_path):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f"QGIS unavailable: {exc}")
    source = tmp_path / "points.geojson"
    gpd.GeoDataFrame({"name": ["A", "B"]}, geometry=[Point(0, 0), Point(1000, 1000)],
                     crs=3857).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    tool = GeoQgisProjectTool()
    checked = await tool.execute(GeoQgisProjectInput(action="preflight",
        layers=[{"file_path": str(source)}], layout={"legend": False}), context)
    assert not checked.is_error, checked.output
    reference = checked.metadata["plan_ref"]
    report_path = Path(checked.metadata["report_path"])
    assert report_path.is_relative_to(tmp_path)
    read = await GeoPythonTool().execute(GeoPythonInput(
        code=f"import json\np=json.load(open({str(report_path)!r}, encoding='utf-8'))\n"
             "print(p['preflight']['display_crs'])"), context)
    assert not read.is_error, read.output
    created = await tool.execute(GeoQgisProjectInput(action="create", plan_ref=reference), context)
    assert not created.is_error, created.output
    assert Path(created.metadata["preview_path"]).is_file()
    assert not created.metadata.get('project_file')
    assert not list(tmp_path.rglob('*.qgz'))
    assert len(created.output) < 16000
    assert "project_manifest" in created.output
    saved = report_path.read_bytes()
    report_path.write_text("{}", encoding="utf-8")
    rejected = await tool.execute(GeoQgisProjectInput(action="create", plan_ref=reference), context)
    assert rejected.is_error and "changed" in rejected.output
    assert not rejected.metadata["render_executed"]
    report_path.write_bytes(saved)
    recovered = await tool.execute(
        GeoQgisProjectInput(action="create", plan_ref=reference), context)
    assert recovered.is_error and recovered.metadata["mutation_conflict"]
    assert not recovered.metadata.get("duplicate_error_blocked")
    checked_again = await tool.execute(GeoQgisProjectInput(
        **recovered.metadata["preflight_call"]), context)
    assert not checked_again.is_error, checked_again.output
    recreated = await tool.execute(GeoQgisProjectInput(
        action="create", plan_ref=checked_again.metadata["plan_ref"]), context)
    assert not recreated.is_error, recreated.output


async def test_preflight_save_error_is_a_tool_result(tmp_path):
    config = GeoConfig()
    config = replace(config, qgis=config.qgis.model_copy(
        update={"enabled": False, "plans_dir": "."}))
    source = tmp_path / "points.geojson"
    gpd.GeoDataFrame({"name": ["A"]}, geometry=[Point(0, 0)], crs=3857).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action="preflight",
        layers=[{"file_path": str(source)}], layout={"legend": False}), context)
    assert result.is_error and "could not be saved" in result.output
    assert result.metadata["render_executed"] is False


def test_prepared_call_cannot_silently_override_the_checked_layers_or_layout():
    with pytest.raises(ValueError, match="only action and plan_ref"):
        GeoQgisProjectInput(action="create", plan_ref="qgis-plan:" + "a" * 64, title="different")


def test_native_registered_reference_rejects_changed_or_unknown_files(tmp_path):
    from geoharness.data.profiles import file_signature
    from geoharness.data.task_context import resolve_registered_path

    source = tmp_path / "data.geojson"
    source.write_text("{}", encoding="utf-8")
    metadata = {"_data_refs_snapshot": {"data:known": {
        "path": str(source), "signature": file_signature(source)}}}
    assert resolve_registered_path("data:known", metadata, cwd=tmp_path,
                                   config=GeoConfig()) == str(source)
    source.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        resolve_registered_path("data:known", metadata, cwd=tmp_path, config=GeoConfig())
    with pytest.raises(ValueError, match="Unknown"):
        resolve_registered_path("data:unknown", metadata, cwd=tmp_path, config=GeoConfig())


def test_large_execution_keeps_paths_without_repeating_the_frozen_plan():
    import json

    from geoharness.qgis.prepared import execution_summary

    result = {"status": "succeeded", "result": {"project_manifest": "workspace/project.json",
        "project_file": "workspace/map.qgz", "preview_path": "workspace/preview.png",
        "layout_plan": {"frozen": "x" * 100000}, "layers": [{"name": "points",
            "feature_count": 84, "native_facts": ["x"] * 1000}]}}
    compact = execution_summary(result)
    assert len(json.dumps(compact)) < 1000
    assert compact["result"]["project_manifest"] == "workspace/project.json"
    assert "layout_plan" not in compact["result"]
    failed = {**result, "status": "failed", "error_summary": "specific failure"}
    compact_failure = execution_summary(failed)
    assert compact_failure["status"] == "failed"
    assert compact_failure["error_summary"] == "specific failure"
    assert len(json.dumps(compact_failure)) < 1000
