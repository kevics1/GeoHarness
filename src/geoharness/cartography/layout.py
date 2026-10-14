"""Separate map panels and measured point labels for custom Python maps.

These checks prove layout geometry only. They cannot prove that data, legends,
projection, or layer visibility are correct; the rendered image still needs review.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.text import Text
from matplotlib.transforms import Bbox

from geoharness.cartography.renderer import _configure_matplotlib
from geoharness.config.settings import MapLayout


@dataclass
class MapFigure:
    figure: Figure
    main: Axes
    detail: Axes | None
    legend: Axes
    footer: Axes

    @property
    def panels(self) -> dict[str, Axes]:
        return {
            "main": self.main,
            **({"detail": self.detail} if self.detail is not None else {}),
            "legend": self.legend,
            "footer": self.footer,
        }


def create_map_figure(
    layout: MapLayout, *, title: str, subtitle: str = "", detail: bool = True
) -> MapFigure:
    """Reserve distinct map/detail/legend/footer panels before plotting.

    Do not add an inset inside the main axes or call tight_layout afterwards.
    Panel coordinates stay fixed; equal-aspect maps may shrink within them.
    """
    _configure_matplotlib()
    fig = plt.figure(figsize=(layout.width, layout.height), dpi=layout.dpi)
    fig.text(.035, .965, title, va="top", fontsize=layout.title_font_size, weight="bold")
    if subtitle:
        fig.text(.035, .924, subtitle, va="top", fontsize=layout.legend_font_size)
    main = fig.add_axes([.035, .32 if detail else .225, .415 if detail else .61,
                         .54 if detail else .64])
    detail_ax = fig.add_axes([.50, .32, .465, .54]) if detail else None
    legend = fig.add_axes([.035 if detail else .69, .16 if detail else .225,
                           .93 if detail else .275, .115 if detail else .64])
    footer = fig.add_axes([.035, .025 if detail else .04, .93, .11 if detail else .115])
    page = MapFigure(fig, main, detail_ax, legend, footer)
    for ax in page.panels.values():
        ax.set_axis_off()
    return page


def _contains(container: Bbox, box: Bbox, tolerance: float = .5) -> bool:
    return (
        box.x0 >= container.x0 - tolerance and box.y0 >= container.y0 - tolerance
        and box.x1 <= container.x1 + tolerance and box.y1 <= container.y1 + tolerance
    )


def place_point_labels(
    ax: Axes,
    points: Iterable[tuple[float, float, str]],
    *,
    font_size: float,
    max_offset_points: float,
    color: str = "#333333",
    protected_points: Iterable[tuple[float, float, float]] = (),
) -> list[dict]:
    """Place labels in priority order using actual text boxes and leader lines.

    ``protected_points`` is (x, y, marker radius in points). Crowded labels are
    omitted and explicitly reported instead of printed on top of each other.
    Callers should provide a numbered table for any omitted priority names.
    Final axes limits/aspect must be set before this call; check again at export.
    """
    fig = ax.figure
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    frame = ax.get_window_extent(renderer).padded(-3)
    occupied = [a.get_bbox_patch().get_window_extent(renderer).padded(3)
                for a in getattr(ax, "_geoharness_labels", [])]
    dot_boxes = []
    for x, y, radius in protected_points:
        px, py = ax.transData.transform((x, y))
        radius_px = radius * fig.dpi / 72
        dot_boxes.append(Bbox.from_extents(px-radius_px, py-radius_px,
                                          px+radius_px, py+radius_px))
    records = []
    annotations = list(getattr(ax, "_geoharness_labels", []))
    # Start near each point, then explore further rings. All distances are points.
    radii = list(range(12, math.floor(max_offset_points) + 1, 12))
    for x, y, text in points:
        px, py = ax.transData.transform((x, y))
        record = {"text": str(text), "point": [float(x), float(y)], "placed": False}
        if not frame.contains(px, py):
            record["reason"] = "point_outside_panel"
            records.append(record)
            continue
        annotation = ax.annotate(
            str(text), (x, y), xytext=(12, 12), textcoords="offset points",
            fontsize=font_size, color=color, va="center", ha="left", zorder=30,
            bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": color,
                  "lw": .5, "alpha": .96},
            arrowprops={"arrowstyle": "-", "color": color, "lw": .6,
                        "shrinkA": 2, "shrinkB": 4},
        )
        for radius in radii:
            for degrees in (45, -45, 135, -135, 0, 90, 180, -90):
                angle = math.radians(degrees)
                dx, dy = radius * math.cos(angle), radius * math.sin(angle)
                annotation.set_position((dx, dy))
                annotation.set_ha("left" if dx >= 0 else "right")
                annotation.update_positions(renderer)
                annotation.update_bbox_position_size(renderer)
                box = annotation.get_bbox_patch().get_window_extent(renderer)
                padded = box.padded(3)
                if _contains(frame, padded) and not any(
                    padded.overlaps(other) for other in occupied + dot_boxes
                ):
                    record.update(placed=True, offset_points=[dx, dy])
                    occupied.append(padded)
                    annotations.append(annotation)
                    break
            if record["placed"]:
                break
        if not record["placed"]:
            record["reason"] = "no_collision_free_position"
            annotation.remove()
        records.append(record)
    ax._geoharness_labels = annotations
    ax._geoharness_label_records = list(getattr(ax, "_geoharness_label_records", [])) + records
    return records


def inspect_figure_layout(page: MapFigure) -> dict:
    """Measure panel and label intersections on the final canvas."""
    fig = page.figure
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    boxes = {name: ax.get_window_extent(renderer) for name, ax in page.panels.items()}
    panel_overlaps = []
    panels_outside_canvas = [name for name, box in boxes.items() if not _contains(fig.bbox, box)]
    names = list(boxes)
    for i, first in enumerate(names):
        for second in names[i+1:]:
            overlap = Bbox.intersection(boxes[first], boxes[second])
            if overlap is not None and overlap.width > 1 and overlap.height > 1:
                panel_overlaps.append([first, second])
    label_overlaps, clipped_labels, omitted_labels, overflow_text = [], [], [], []
    figure_text_overlaps, clipped_figure_text, text_panel_overlaps = [], [], []
    for text in fig.texts:
        if not text.get_visible() or not text.get_text():
            continue
        box = text.get_window_extent(renderer)
        if not _contains(fig.bbox, box):
            clipped_figure_text.append(text.get_text())
        for name, frame in boxes.items():
            if box.overlaps(frame):
                figure_text_overlaps.append({"panel": name, "text": text.get_text()})
    visible_headers = [text for text in fig.texts if text.get_visible() and text.get_text()]
    for i, first in enumerate(visible_headers):
        for second in visible_headers[i+1:]:
            if first.get_window_extent(renderer).overlaps(second.get_window_extent(renderer)):
                figure_text_overlaps.append({"texts": [first.get_text(), second.get_text()]})
    for name, ax in page.panels.items():
        annotations = getattr(ax, "_geoharness_labels", [])
        label_boxes = [(a.get_text(), a.get_bbox_patch().get_window_extent(renderer))
                       for a in annotations]
        for i, (text, box) in enumerate(label_boxes):
            if not _contains(boxes[name], box):
                clipped_labels.append({"panel": name, "text": text})
            for other_text, other_box in label_boxes[i+1:]:
                if box.padded(1).overlaps(other_box.padded(1)):
                    label_overlaps.append({"panel": name, "texts": [text, other_text]})
        omitted_labels.extend({"panel": name, **r} for r in
                              getattr(ax, "_geoharness_label_records", []) if not r["placed"])
        # Legend/footer content must stay within their reserved panels.
        if name in {"legend", "footer"}:
            content = [*ax.texts, *([ax.get_legend()] if ax.get_legend() else [])]
            for artist in content:
                if artist.get_visible() and not _contains(
                    boxes[name], artist.get_window_extent(renderer)
                ):
                    overflow_text.append({"panel": name, "text":
                                          artist.get_text() if hasattr(artist, "get_text")
                                          else "legend"})
        texts = [text for text in [*ax.texts, ax.title]
                 if text.get_visible() and text.get_text()]
        for text in texts:
            # Text's base implementation excludes an Annotation's leader line.
            box = Text.get_window_extent(text, renderer)
            if not _contains(fig.bbox, box):
                clipped_figure_text.append(text.get_text())
            for other, frame in boxes.items():
                if other != name and box.overlaps(frame):
                    text_panel_overlaps.append({"source_panel": name, "panel": other,
                                                "text": text.get_text()})
    problems = (panels_outside_canvas or panel_overlaps or label_overlaps or clipped_labels
                or overflow_text
                or figure_text_overlaps or clipped_figure_text or text_panel_overlaps)
    return {
        "status": "issues" if problems else "passed",
        "scope": "layout_geometry",
        "visual_review_status": "unverified",
        "data_correctness_status": "unverified",
        "panel_overlaps": panel_overlaps,
        "panels_outside_canvas": panels_outside_canvas,
        "label_overlaps": label_overlaps,
        "clipped_labels": clipped_labels,
        "overflow_text": overflow_text,
        "figure_text_overlaps": figure_text_overlaps,
        "clipped_figure_text": clipped_figure_text,
        "text_panel_overlaps": text_panel_overlaps,
        "omitted_labels": omitted_labels,
        "panels_pixels": {name: list(map(float, b.extents)) for name, b in boxes.items()},
    }


def save_map_figure(page: MapFigure, output_path: str | Path) -> dict:
    """Export at the measured canvas size and bind the layout report to its hash."""
    output_path = Path(output_path)
    report = inspect_figure_layout(page)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    page.figure.savefig(output_path, facecolor="white", dpi=page.figure.dpi)
    report.update(image_path=str(output_path.resolve()),
                  image_sha256=hashlib.sha256(output_path.read_bytes()).hexdigest())
    output_path.with_suffix(".layout.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report
