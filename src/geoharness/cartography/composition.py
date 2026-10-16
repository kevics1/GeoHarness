"""Conservative composition risks measured before the first render.

These are planning hints, never visual acceptance or a reason to remove data.
"""

from __future__ import annotations

import numpy as np


def composition_from_anchors(
    anchors: list[dict], *, labeled_point_count: int, bounds: list[float], frame_mm: list[float],
) -> dict:
    """Use the same bounded paper-space check for GeoPandas and native QGIS anchors."""
    xmin, ymin, xmax, ymax = bounds
    mm_per_unit = min(frame_mm[0] / (xmax - xmin), frame_mm[1] / (ymax - ymin))
    points = [(anchor["x"], anchor["y"]) for anchor in anchors]
    labels = [anchor["layer_index"] for anchor in anchors]
    radii = [anchor["radius_mm"] for anchor in anchors]
    warnings = []
    if len(points) > 1:
        positions = np.asarray(points)
        distance = np.linalg.norm(positions[:, None] - positions[None, :], axis=2) * mm_per_unit
        upper = np.triu(np.ones(distance.shape, dtype=bool), k=1)
        minimum = float(distance[upper].min())
        footprint = (np.asarray(radii)[:, None] + np.asarray(radii)[None, :]) / 2
        conflicts = upper & (distance < footprint)
        pairs = int(conflicts.sum())
        if pairs:
            indexes = sorted({labels[int(i)] for i in np.where(conflicts)[0]} |
                             {labels[int(i)] for i in np.where(conflicts)[1]})
            warnings.append({"code": "dense_point_labels", "layer_indices": indexes,
                "close_anchor_pairs": pairs, "minimum_anchor_distance_mm": minimum,
                "recommendation": "Before rendering, prioritize key labels, reduce symbol "
                    "size or add a verified detail_extent for the dense cluster. "
                    "Keep every required layer; do not substitute a caption for "
                    "required key labels. If issue persists after a consolidated "
                    "repair, use separate overview/detail maps in this request."})
    return {"scope": "bounded_label_anchor_risk; not a visual overlap test",
            "labeled_point_count": labeled_point_count, "sample_size": len(points),
            "sample_limit": 512, "warnings": warnings}


def composition_risks(
    frames: list[dict], *, report: dict, layout: dict, map_frames: dict | None = None,
) -> dict:
    """Compare a bounded sample of labeled point anchors in paper millimeters."""
    width = layout.get("width_mm", 297) - 2 * layout.get("margin_mm", 12)
    if layout.get("legend", True) or layout.get("detail_extent"):
        width -= layout.get("sidebar_width_mm", 60) + layout.get("gap_mm", 5)
    height = (layout.get("height_mm", 210) - 2 * layout.get("margin_mm", 12)
              - layout.get("title_height_mm", 16) - layout.get("footer_height_mm", 18)
              - 2 * layout.get("gap_mm", 5))
    if map_frames is not None:
        measured = {}
        for name, view in map_frames.items():
            selected = [{**frames[index], "layer_index": index}
                        for index in view["render_layers"]]
            measured[name] = composition_risks(selected,
                report={**report, "map_extent": view["extent"]},
                layout={**layout, "_frame_mm": view["rect_mm"][2:]})
        return {**measured["main_map"], "map_frames": measured}
    width, height = layout.get("_frame_mm", [width, height])
    xmin, ymin, xmax, ymax = report["map_extent"]
    anchors = []
    total = 0
    for index, frame in enumerate(frames):
        if frame.get("role") == "table_only":
            continue
        if "gdf" not in frame or not frame.get("label_field"):
            continue
        gdf = frame["gdf"].to_crs(report["display_crs"])
        selected = gdf[(gdf.geometry.geom_type == "Point") & gdf[frame["label_field"]].notna()]
        selected = selected.cx[xmin:xmax, ymin:ymax]
        total += len(selected)
        for geometry in selected.geometry.head(max(0, 512 - len(anchors))):
            anchors.append({"x": geometry.x, "y": geometry.y,
                "layer_index": frame.get("layer_index", index),
                "radius_mm": max((frame.get("style") or {}).get("size_mm", 3),
                    (frame.get("label_style") or {}).get("size_pt", 9) * 25.4 / 72)})
    return composition_from_anchors(anchors, labeled_point_count=total,
        bounds=report["map_extent"], frame_mm=[width, height])
