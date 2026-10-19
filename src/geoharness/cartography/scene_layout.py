"""Compile independent paper regions from a frozen geographic extent."""

from __future__ import annotations

import math


def _inside(rect, width, height):
    return (
        rect[0] >= 0
        and rect[1] >= 0
        and rect[0] + rect[2] <= width
        and (rect[1] + rect[3] <= height)
    )


def _overlap(a, b):
    return min(a[0] + a[2], b[0] + b[2]) > max(a[0], b[0]) and (
        min(a[1] + a[3], b[1] + b[3]) > max(a[1], b[1])
    )


def compile_layout(scene, page, extent, legend_labels, measure):
    """Choose a paper layout once. Never use rendered pixel guesses as geometry."""
    layout = page.layout
    width, height = scene.width_mm, scene.height_mm
    margin, gap = layout.margin_mm, layout.gap_mm
    top, bottom = 25, height - 31  # Reserved title, scale and source/footer rows.
    ratio = (extent[2] - extent[0]) / (extent[3] - extent[1])

    def legend_grid(available, side=False):
        columns = 1 if side else max(1, min(len(legend_labels) or 1, int(available // 70)))
        cell = available / columns
        heights = [
            max(6, 4 * math.ceil(measure(label) / max(8, cell - 9)) + 2) for label in legend_labels
        ]
        rows = [max(heights[i : i + columns]) for i in range(0, len(heights), columns)]
        return columns, cell, rows, sum(rows)

    candidates = []
    for strategy in ("side", "top", "bottom"):
        if layout.placement not in {"auto", strategy}:
            continue
        if strategy == "side" and legend_labels:
            lw = min(layout.legend_width_mm, width / 3)
            cols, cell, rows, lh = legend_grid(lw, True)
            box = [margin, top, width - 2 * margin - gap - lw, bottom - top]
        else:
            lw = width - 2 * margin
            cols, cell, rows, lh = legend_grid(lw)
            box = [
                margin,
                top + (lh + gap if strategy == "top" and legend_labels else 0),
                lw,
                bottom - top - (lh + gap if legend_labels else 0),
            ]
        if min(box[2:]) <= 20 or lh > bottom - top:
            continue
        fitted_w, fitted_h = min(box[2], box[3] * ratio), min(box[3], box[2] / ratio)
        actual = [box[0], box[1], fitted_w, fitted_h]
        legend = (
            [box[0] + fitted_w + gap, top, lw, lh]
            if strategy == "side"
            else [margin, top if strategy == "top" else box[1] + fitted_h + gap, lw, lh]
        )
        candidates.append((fitted_w * fitted_h, strategy, box, actual, legend, cols, cell, rows))
    if layout.placement == "manual":
        box = list(layout.map_rect_mm)
        if not _inside(box, width, height):
            raise ValueError("map_rect_mm超出纸张边界")
        actual = [box[0], box[1], min(box[2], box[3] * ratio), min(box[3], box[2] / ratio)]
        legend = list(
            layout.legend_rect_mm
            or [width - margin - layout.legend_width_mm, top, layout.legend_width_mm, bottom - top]
        )
        cols, cell, rows, lh = legend_grid(legend[2], True)
        if legend_labels and (
            not _inside(legend, width, height) or _overlap(box, legend) or lh > legend[3]
        ):
            raise ValueError("legend_rect_mm越界、与图框重叠或高度不足")
        candidates = [(actual[2] * actual[3], "manual", box, actual, legend, cols, cell, rows)]
    if not candidates:
        raise ValueError("纸张/图例无法容纳当前布局；增大纸张或明确manual区域，不改变分析范围")
    _, strategy, box, actual, legend, columns, cell, rows = max(candidates, key=lambda c: c[0])
    final_extent = list(extent)
    if layout.fit == "frame":
        cx, cy = (extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2
        dx, dy = extent[2] - extent[0], extent[3] - extent[1]
        dx, dy = max(dx, dy * box[2] / box[3]), max(dy, dx * box[3] / box[2])
        final_extent = [cx - dx / 2, cy - dy / 2, cx + dx / 2, cy + dy / 2]
        actual = box
        if strategy == "side":
            legend[0] = box[0] + box[2] + gap
        elif strategy == "bottom":
            legend[1] = box[1] + box[3] + gap
    positions = []
    y = legend[1]
    for i, row_height in enumerate(rows):
        for j in range(columns):
            if i * columns + j < len(legend_labels):
                positions.append([legend[0] + j * cell, y, cell, row_height])
        y += row_height
    return {
        "strategy": strategy,
        "requested_extent": list(extent),
        "extent": final_extent,
        "map_rect_mm": actual,
        "legend_rect_mm": legend,
        "legend_cells_mm": positions,
        "map_area_ratio": actual[2] * actual[3] / (width * height),
        "scope": "paper_layout_geometry; no_pixel_or_business_acceptance",
    }
