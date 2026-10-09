"""Generate a native map footer from verified analysis manifest facts."""

from __future__ import annotations

def apply_analysis_footer(payload: dict, facts: dict) -> dict:
    """Statistics come from the manifest; free text is retained as separate notes."""
    result = dict(payload)
    layout = dict(payload.get("layout") or {})
    notes = layout.get("footer_text", "")
    footer = facts["footer_text"]
    layout["footer_text"] = footer + ("\n备注：" + notes if notes else "")
    # QGIS measures and wraps the actual font beside the actual scale bar. A
    # character-count estimate here could reject a page which fits in reality.
    from geoharness.tools.geo_qgis_project import QgisProjectLayout

    result["layout"] = QgisProjectLayout.model_validate(layout).model_dump(exclude_none=True)
    result["analysis_facts"] = facts
    return result
