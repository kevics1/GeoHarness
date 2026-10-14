"""Generate a native map footer from verified analysis manifest facts."""

from __future__ import annotations

import re


def _source_note(notes: str) -> str:
    """Keep whole short source/time clauses; never slice dates or numeric claims."""
    clauses = [part.strip() for part in re.split(r"[；;\n。]", notes) if part.strip()]
    selected = []
    for clause in clauses:
        if re.match(r"(?:数据)?(?:来源|时间|日期|时段|年份)|source\b|date\b|time\b",
                    clause, re.I) and len(clause) <= 80:
            candidate = "；".join([*selected, clause])
            if len(candidate) <= 96:
                selected.append(clause)
    return ("；".join(selected) + "；完整备注见工程事实报告" if selected else
            "完整来源备注见工程事实报告")


def apply_analysis_footer(payload: dict, facts: dict) -> dict:
    """Statistics come from the manifest; free text is retained as separate notes."""
    result = dict(payload)
    layout = dict(payload.get("layout") or {})
    notes = layout.get("footer_text", "")
    claimed = set(re.findall(r"计量\s*(?:CRS|坐标系)\s*[:：]?\s*(EPSG:\d+)", notes, re.I))
    verified = {item["metric_crs"].upper() for item in facts.get("analyses", [])}
    if facts.get("analyses") and {item.upper() for item in claimed} - verified:
        raise ValueError("footer_text metric CRS contradicts verified analysis; "
                         "remove manual statistics/CRS notes and use the generated facts footer")
    facts = dict(facts)
    if notes:
        facts["source_notes"] = notes
    footer = facts.get("footer_text", "")
    lines = footer.splitlines()
    if notes:
        short_note = _source_note(notes)
        if len(lines) < 3:
            lines.append(short_note)
        else:
            # Three fact lines already exist: add only the bounded source note.
            addition = "；" + short_note
            if len(footer) + len(addition) > 400:
                addition = "；完整来源备注见工程事实报告"
            lines[1] += addition
    layout["footer_text"] = "\n".join(lines)
    # QGIS measures and wraps the actual font beside the actual scale bar. A
    # character-count estimate here could reject a page which fits in reality.
    from geoharness.tools.geo_qgis_project import QgisProjectLayout

    result["layout"] = QgisProjectLayout.model_validate(layout).model_dump(exclude_none=True)
    result["analysis_facts"] = facts
    return result
