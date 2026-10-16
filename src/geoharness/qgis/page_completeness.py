"""Actual page composition, separate from visual readability."""


def page_completeness(layout):
    import json

    from qgis.core import (
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemMap,
        QgsLayoutItemScaleBar,
    )

    requirements = json.loads(layout.customProperty("geoharness/page_requirements", "{}"))
    pages, problems = [], []
    for number in requirements:
        if int(number) >= layout.pageCollection().pageCount():
            problems.append(f"page {int(number) + 1}: required page is missing")
    for number in range(layout.pageCollection().pageCount()):
        items = [item for item in layout.items()
                 if hasattr(item, "page") and item.page() == number]
        maps = [item.id() for item in items if isinstance(item, QgsLayoutItemMap)]
        if not maps and str(number) not in requirements:
            continue
        present = {item.id() for item in items}
        for item in items:
            if (isinstance(item, QgsLayoutItemLabel) and item.id() in {"title", "detail_title"}
                    and not item.text().strip()):
                present.discard(item.id())
            if isinstance(item, (QgsLayoutItemLegend, QgsLayoutItemScaleBar)):
                linked = item.linkedMap()
                if linked is None or linked.page() != number:
                    present.discard(item.id())
        missing = sorted(set(requirements.get(str(number), [])) - present)
        problems.extend(f"page {number + 1}: missing {name}" for name in missing)
        titles = [item.text() for item in items if isinstance(item, QgsLayoutItemLabel)
                  and item.id() in {"title", "detail_title"}]
        pages.append({"page_index": number, "map_frames": sorted(maps),
            "title_text": titles[0] if titles else "",
            "legends": sorted(item.id() for item in items
                              if isinstance(item, QgsLayoutItemLegend)),
            "scale_bars": sorted(item.id() for item in items
                                 if isinstance(item, QgsLayoutItemScaleBar)),
            "missing": missing})
    return {"checked": bool(requirements), "valid": not problems if requirements else None,
            "pages": pages, "problems": problems}
