"""Focused source-binding and measured-failure feedback checks, without QGIS renders."""

import json

from openharness.tools.base import ToolExecutionContext, ToolResult
from pydantic import BaseModel

from geoharness.cartography.facts import _binding_candidates, _EvidenceReader
from geoharness.cartography.retry_guard import guard_map_errors
from geoharness.config.settings import GeoConfig
from geoharness.qgis.prepared import execution_summary, failure_summary


def test_binding_suggestion_uses_persisted_source_path_not_layer_name(tmp_path):
    source = tmp_path / "input.geojson"
    other = tmp_path / "same-name.geojson"
    output = tmp_path / "result.geojson"
    for path in (source, other, output):
        path.write_text("{}", encoding="utf-8")
    manifest = {"action": "area", "sources": {"source": {"file_path": str(source)}}}
    outputs = {"result": {"path": output}}
    mapped = [{"layer_index": 3, "file_path": str(other), "name": "result"},
              {"layer_index": 5, "file_path": str(source), "name": "irrelevant-name"}]
    reader = _EvidenceReader(tmp_path, [])
    suggestion = _binding_candidates(manifest, outputs, mapped, reader)[0]
    assert suggestion["suggested_change"] == {
        "field": "layers[5].file_path", "current": str(source), "proposed": str(output)}
    assert suggestion["automatic_application"] is False
    ambiguous = _binding_candidates(manifest, outputs, [*mapped, mapped[1]], reader)[0]
    assert len(ambiguous["candidate_layers"]) == 2
    assert "suggested_change" not in ambiguous
    unknown = _binding_candidates({"action": "area"}, outputs, mapped, reader)[0]
    assert unknown["match_basis"] == "unresolved"
    assert "suggested_change" not in unknown


def test_layout_failure_compacts_prose_but_keeps_measured_reason_and_paths():
    diagnostic = {
        "font": {"family": "Microsoft YaHei", "loaded": True},
        "effective_parameters": {"height_mm": 195},
        "adaptations": [{"height_mm": 195}],
        "bounds_check": {"checked": True, "valid": False,
            "problems": ["legend: legend content exceeds its reserved rectangle"],
            "content_metrics": {"legend": {"required_mm": [55, 118.77],
                                            "available_mm": [55, 70.98]},
                                "title": {"required_mm": [20, 10]}},
            "items": {"legend": [100, 20, 55, 70.98]}},
        "legends": {"long": "x" * 30000}, "layout_plan": {"long": "x" * 30000},
    }
    result = {"status": "failed", "error_summary": "ValueError: legend content exceeds",
        "run_dir": "workspace/run", "manifest_path": "workspace/run/manifest.json",
        "result_path": "workspace/run/result.json", "diagnostics": diagnostic}
    compact = execution_summary(result)
    assert len(json.dumps(compact)) < 2500
    assert compact["status"] == "failed"
    assert compact["result_path"] == result["result_path"]
    assert compact["diagnostics"]["font"]["loaded"] is True
    assert compact["diagnostics"]["bounds_check"]["content_metrics"]["legend"] == (
        diagnostic["bounds_check"]["content_metrics"]["legend"])
    assert len(result["diagnostics"]["legends"]["long"]) == 30000


def test_zoom_failure_keeps_frames_suggestions_and_unrounded_value():
    diagnostics = {"code": "detail_zoom_insufficient", "actual_zoom": 1.9999953,
        "required_zoom": 2.0, "frames": {"main_map": {"scale_denominator": 400000},
        "detail_map": {"scale_denominator": 200000.47}},
        "suggestions": [{"strategy": "enlarge_detail_frame", "frame_mm": [84.1, 56.3]}]}
    result = {"status": "failed", "error_summary": "detail zoom failed",
              "diagnostics": diagnostics}
    assert failure_summary(result)["diagnostics"] == diagnostics


async def test_duplicate_failure_is_short_and_input_change_reexecutes(tmp_path):
    class Arguments(BaseModel):
        file_path: str

    class Failing:
        name = "failure_feedback_test"
        calls = 0

        @guard_map_errors
        async def execute(self, arguments, context):
            self.calls += 1
            return ToolResult(output="specific layout failure\n" + "x" * 30000, is_error=True,
                metadata={"deterministic_error": True, "error_summary": "specific layout failure",
                          "result_path": "workspace/result.json"})

    path = tmp_path / "input.geojson"
    path.write_text("{}", encoding="utf-8")
    arguments = Arguments(file_path=str(path))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    tool = Failing()
    await tool.execute(arguments, context)
    duplicate = await tool.execute(arguments, context)
    assert tool.calls == 1
    assert duplicate.is_error and duplicate.metadata["duplicate_error_blocked"]
    assert duplicate.metadata["render_executed"] is False
    assert "未执行" in duplicate.output and "specific layout failure" in duplicate.output
    assert len(duplicate.output) < 1000
    path.write_text('{"changed": true}', encoding="utf-8")
    await tool.execute(arguments, context)
    assert tool.calls == 2
