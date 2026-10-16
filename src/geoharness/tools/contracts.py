"""Actionable local schema errors without tool execution or upstream modification."""

from __future__ import annotations

from pydantic import ValidationError


def input_feedback(name: str, arguments: dict) -> dict | None:
    if name != "geo_qgis_project":
        return None
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput

    try:
        GeoQgisProjectInput.model_validate(arguments)
        return None
    except ValidationError as exc:
        errors = [{"field": ".".join(str(key) for key in error["loc"]) or "request",
                   "reason": error["msg"][:900]}
                  for error in exc.errors(include_input=False, include_url=False)[:8]]
    paths = " ".join(error["field"] for error in errors)
    section = ("labels" if "label_style" in paths
               else "style" if any(key in paths for key in ("style", "color", "categories"))
               else "frame" if any(key in paths for key in ("frame", "extent", "focus"))
               else "roster" if "label_table" in paths
               else "layout" if "layout" in paths
               else "layer" if "layers" in paths else "workflow")
    result = {"status": "invalid_input", "executed": False, "errors": errors,
        "help_call": {"action": "help", "section": section},
        "recovery": "Use the actual geo_qgis_project contract, not geo_cartography bridge "
                    "fields or Processing help. Inspect saved spec; change only failed inputs."}
    if arguments.get("spec_ref"):
        result["inspect_call"] = {"action": "inspect", "spec_ref": arguments["spec_ref"]}
    if arguments.get("plan_ref"):
        result["minimal_call"] = {"action": "create", "plan_ref": arguments["plan_ref"]}
    return result
