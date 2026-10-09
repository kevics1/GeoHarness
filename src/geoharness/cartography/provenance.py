"""Generate a native map footer from verified analysis manifest facts."""

from __future__ import annotations

import math


def apply_analysis_footer(payload: dict, facts: dict) -> dict:
    """Statistics come from the manifest; free text is retained as separate notes."""
    result = dict(payload)
    layout = dict(payload.get("layout") or {})
    notes = layout.get("footer_text", "")
    footer = facts["footer_text"]
    layout["footer_text"] = footer + ("\n备注：" + notes if notes else "")
    # Reserve explicit room for every generated line before starting native rendering.
    lines = layout["footer_text"].splitlines()
    body_size = layout.get("body_font_size_pt", 9)
    width = layout.get("width_mm", 297) - 2 * layout.get("margin_mm", 12)
    if layout.get("scale_bar", True):
        width -= 50
    characters_per_line = max(1, int(width / (body_size * 25.4 / 72)))
    line_count = sum(max(1, math.ceil(len(line) / characters_per_line)) for line in lines)
    required = line_count * body_size * 25.4 / 72 * 1.45 + 4
    layout["footer_height_mm"] = max(layout.get("footer_height_mm", 18), required)
    from geoharness.tools.geo_qgis_project import QgisProjectLayout

    result["layout"] = QgisProjectLayout.model_validate(layout).model_dump(exclude_none=True)
    result["analysis_facts"] = facts
    return result
