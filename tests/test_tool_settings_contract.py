"""Public schemas must explain every field models are asked to supply."""

from __future__ import annotations

import json

from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def _walk(schema, root="$"):
    missing, undocumented = [], []
    for name, prop in (schema.get("properties") or {}).items():
        path = f"{root}.{name}"
        if not isinstance(prop, dict):
            undocumented.append(path)
            continue
        if not prop.get("description"):
            undocumented.append(path)
        missing.extend(_walk(prop, path))
        for key in ("anyOf", "oneOf", "allOf"):
            for index, item in enumerate(prop.get(key) or []):
                if isinstance(item, dict):
                    missing.extend(_walk(item, f"{path}[{key}:{index}]"))
    for name, definition in (schema.get("$defs") or {}).items():
        if isinstance(definition, dict):
            missing.extend(_walk(definition, f"$defs.{name}"))
    return missing, undocumented

def test_every_public_input_field_has_a_description_and_example():
    for tool in (GeoPythonTool(), GeoQgisProjectTool(), GeoDeliverTool()):
        schema = tool.to_api_schema()
        assert schema["input_schema"].get("properties")
        assert schema["input_schema"].get("examples"), tool.name
        _, undocumented = _walk(schema["input_schema"])
        assert not undocumented, (tool.name, undocumented)

def test_schema_hard_constraints_stay_machine_readable():
    project = json.dumps(GeoQgisProjectInput.model_json_schema(), ensure_ascii=False)
    python = json.dumps(GeoPythonInput.model_json_schema(), ensure_ascii=False)
    delivery = json.dumps(GeoDeliverInput.model_json_schema(), ensure_ascii=False)
    assert "additionalProperties" in project and "false" in project
    assert "complete Python script" in python.lower() or "完整执行" in python
    assert "project_dir" in delivery and "allow_report_only" in delivery
