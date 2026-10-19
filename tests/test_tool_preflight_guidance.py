"""Call-time cautions survive bounded help and describe real invocation constraints."""

import json
from dataclasses import replace

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.config.settings import GeoConfig
from geoharness.invocation import action_forms, invocation_guide, section_cautions
from geoharness.prompts.system_prompt import _build_execution_section
from geoharness.qgis.inspection import help_output
from geoharness.qgis.specification import contract_help
from geoharness.tools.contracts import input_feedback
from geoharness.tools.geo_python import GeoPythonTool, python_recovery_hints
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


@pytest.mark.parametrize("budget", [1200, 2400])
@pytest.mark.parametrize("section", ["workflow", "layer", "frame", "layout", "roster",
                                     "style", "labels", "edits"])
def test_compact_help_never_hides_critical_rules(section, budget):
    payload = contract_help(section)
    compact = help_output(payload, call={"action": "help", "section": section}, max_chars=budget)
    assert compact["cautions"] == section_cautions(section)
    assert len(json.dumps(compact, ensure_ascii=False)) <= budget
    assert GeoQgisProjectInput.model_validate(compact["detail_call"])


async def test_public_help_detail_preserves_original_descriptions(tmp_path):
    config = replace(GeoConfig(), qgis=GeoConfig().qgis.model_copy(
        update={"inspection_max_chars": 1200}))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    tool = GeoQgisProjectTool()
    result = await tool.execute(GeoQgisProjectInput(action="help", section="frame"), context)
    assert not result.is_error, result.output
    compact = json.loads(result.output)
    assert "render_layers" in " ".join(compact["cautions"])
    # Read an exact property of the full help payload, not the stripped inline projection.
    detail = await tool.execute(GeoQgisProjectInput(action="help", section="frame",
        detail_path=["schema", "properties", "focus_layers", "description"]), context)
    assert not detail.is_error, detail.output
    description = json.loads(detail.output)["value"]
    assert "FIT" in description and "layer_key" in description


def test_tool_and_system_share_python_cautions_without_growing_the_description():
    prompt = _build_execution_section(GeoConfig())
    assert invocation_guide("geo_python") in GeoPythonTool.description
    assert invocation_guide("geo_python") in prompt
    assert invocation_guide("geo_cartography") in prompt
    assert invocation_guide("geo_qgis_project") in prompt
    assert "仅inspect" in prompt
    assert "首选 build(spec_ref, edits)" not in prompt
    assert "read(1) is 2D" in prompt and "nodata, not fill_value" in prompt
    assert "调用前注意事项" in prompt
    assert len(GeoPythonTool.description) < 4463
    assert set(action_forms()) == {"help", "spec:new", "spec:edit", "build", "preflight",
                                   "create", "inspect", "export", "recover"}


def test_cross_field_feedback_locates_the_conflict_and_measured_missing_references():
    feedback = input_feedback("geo_qgis_project", {"action": "create",
        "layers": [{"file_path": "a.gpkg"}, {"file_path": "b.gpkg"}],
        "layout": {"main_frame": {"focus_layers": [0, 1], "render_layers": [0]}}})
    assert feedback["executed"] is False
    assert feedback["failure"]["retry_without_change"] is False
    error = feedback["errors"][0]
    assert error["field"] == "layout.main_frame.focus_layers"
    assert error["missing"] == [1]
    assert error["related_fields"] == ["layout.main_frame.focus_layers",
                                        "layout.main_frame.render_layers"]
    assert feedback["help_call"]["section"] == "frame"


def test_frozen_plan_and_readonly_selector_errors_provide_the_right_next_call():
    call = {"action": "create", "plan_ref": "returned-ref", "title": "extra"}
    feedback = input_feedback("geo_qgis_project", call)
    assert feedback["errors"][0]["field"] == "plan_ref"
    assert feedback["minimal_call"] == {"action": "create", "plan_ref": "returned-ref"}
    feedback = input_feedback("geo_qgis_project", {"action": "create", "offset": 2})
    assert feedback["errors"][0]["field"] == "offset"
    assert "action" in feedback["errors"][0]["related_fields"]


def test_api_and_shape_errors_offer_different_recovery_steps():
    api = python_recovery_hints("", "TypeError: mask() got an unexpected keyword argument 'x'")
    shape = python_recovery_hints("", "IndexError: tuple index out of range")
    assert any("inspect.signature" in item for item in api)
    assert any("ndim/shape" in item and "read(1)" in item for item in shape)
