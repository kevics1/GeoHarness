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
        errors = []
        for error in exc.errors(include_input=False, include_url=False)[:8]:
            prefix = ".".join(str(key) for key in error["loc"])
            detail = error.get("ctx") or {}
            field = detail.get("field", "")
            path = ".".join(value for value in (prefix, field) if value) or "request"
            item = {"field": path, "reason": error["msg"][:900], "code": error["type"]}
            if detail.get("related_fields"):
                item["related_fields"] = [".".join(part for part in (prefix, value) if part)
                                          for value in detail["related_fields"]]
            if "missing" in detail:
                item["missing"] = detail["missing"]
            errors.append(item)
    paths = " ".join(error["field"] for error in errors)
    section = ("labels" if "label_style" in paths
               else "style" if any(key in paths for key in ("style", "color", "categories"))
               else "frame" if any(key in paths for key in ("frame", "extent", "focus"))
               else "roster" if "label_table" in paths
               else "layout" if "layout" in paths
               else "layer" if "layers" in paths else "workflow")
    result = {"status": "invalid_input", "executed": False, "errors": errors,
        "failure": {"category": "argument_contract", "retry_without_change": False},
        "help_call": {"action": "help", "section": section},
        "recovery": "使用geo_qgis_project实际契约，不能照搬geo_cartography或Processing参数。"
                    "查看已保存spec及错误字段，仅修改失败项；仍缺业务含义时询问用户，不猜测。"}
    if arguments.get("spec_ref"):
        result["inspect_call"] = {"action": "inspect", "spec_ref": arguments["spec_ref"]}
    if arguments.get("plan_ref") and arguments.get("action") == "create":
        result["minimal_call"] = {"action": "create", "plan_ref": arguments["plan_ref"]}
    return result
