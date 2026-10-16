"""Real output-boundary regressions: next actions survive and details remain readable."""

import json
from dataclasses import replace

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.config.settings import GeoConfig
from geoharness.qgis.prepared import load_preflight, save_preflight, summary
from geoharness.qgis.specification import save_spec
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def test_large_plan_keeps_create_call_visible_and_inspect_reads_exact_details(tmp_path):
    config = GeoConfig()
    parameters = {"title": "immutable", "layers": [{"file_path": "unused.geojson"}]}
    report = {"status": "passed", "frame_checks": {"main": {"objects": list(range(600))}},
              "warnings": ["large diagnostic " * 200] * 50}
    ref, path = save_preflight(parameters, report, cwd=tmp_path, settings=config.qgis)
    visible = summary(report, ref, path)
    assert json.loads(json.dumps(visible, ensure_ascii=False)[:3000])["plan_ref"] == ref
    assert len(json.dumps(visible, ensure_ascii=False)) <= config.qgis.inspection_max_chars
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    read = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="inspect", plan_ref=ref,
        detail_path=["preflight", "frame_checks", "main", "objects"],
        offset=590, limit=8), context)
    assert not read.is_error, read.output
    value = json.loads(read.output)
    assert value["value"] == list(range(590, 598))
    assert value["next_call"]["offset"] == 598
    assert len(read.output) <= config.qgis.inspection_max_chars


async def test_help_is_bounded_and_nested_schema_is_retrievable_without_global_files(tmp_path):
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    tool = GeoQgisProjectTool()
    for section in ("layer", "roster", "edits", "layout", "style"):
        result = await tool.execute(GeoQgisProjectInput(action="help", section=section), context)
        assert not result.is_error, result.output
        assert len(result.output) <= context.metadata["geoharness_config"].qgis.inspection_max_chars
    result = await tool.execute(GeoQgisProjectInput(action="help", section="layer",
        detail_path=["schema", "properties", "file_path"]), context)
    assert json.loads(result.output)["value"]["type"] == "string"


def test_plan_errors_identify_missing_and_oversized_separately(tmp_path):
    settings = GeoConfig().qgis
    with pytest.raises(ValueError, match="not found"):
        load_preflight("qgis-plan:" + "a" * 64, cwd=tmp_path, settings=settings)
    ref, path = save_preflight({}, {}, cwd=tmp_path, settings=settings)
    with path.open("wb") as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    with pytest.raises(ValueError, match="size limit"):
        load_preflight(ref, cwd=tmp_path, settings=settings)


async def test_inspection_does_not_traverse_filesystem_or_render(tmp_path):
    config = GeoConfig()
    ref, _ = save_preflight({}, {"status": "passed"}, cwd=tmp_path, settings=config.qgis)
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="inspect", plan_ref=ref, detail_path=["../outside"]),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config}))
    assert result.is_error and result.metadata["render_executed"] is False


async def test_actual_spec_preflight_wrapper_keeps_final_output_bounded(tmp_path, monkeypatch):
    config = GeoConfig()
    config = replace(config, qgis=config.qgis.model_copy(update={"enabled": False}))
    source = tmp_path / "actual.geojson"
    source.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    ref, _ = save_spec({"layers": [{"file_path": str(source)}]},
                       cwd=tmp_path, settings=config.qgis)
    monkeypatch.setattr(GeoQgisProjectTool, "_preflight", lambda *a, **kw: {
        "status": "passed", "display_crs": "EPSG:3857", "map_extent": [0, 0, 1, 1],
        "map_extent_source": "explicit", "layers": [],
        "frame_checks": {"main_map": {"requirements": [
            {"layer_index": i, "valid": True, "coverage": "complete", "visible": 234}
            for i in range(8)]}}, "warnings": ["warning" * 200]})
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="preflight", spec_ref=ref), ToolExecutionContext(
            cwd=tmp_path, metadata={"geoharness_config": config}))
    assert not result.is_error, result.output
    assert len(result.output) <= config.qgis.inspection_max_chars
    assert json.loads(result.output)["create_call"]["plan_ref"] == result.metadata["plan_ref"]
    assert json.loads(result.output)["spec_ref"]
