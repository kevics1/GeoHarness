"""Conservative composition risks measured before the first render.

These are planning hints, never visual acceptance or a reason to remove data.
"""

from __future__ import annotations

import numpy as np


def composition_risks(frames: list[dict], *, report: dict, layout: dict) -> dict:
    """Compare a bounded sample of labeled point anchors in paper millimeters."""
    width = layout.get("width_mm", 297) - 2 * layout.get("margin_mm", 12)
    if layout.get("legend", True) or layout.get("detail_extent"):
        width -= layout.get("sidebar_width_mm", 60) + layout.get("gap_mm", 5)
    height = (layout.get("height_mm", 210) - 2 * layout.get("margin_mm", 12)
              - layout.get("title_height_mm", 16) - layout.get("footer_height_mm", 18)
              - 2 * layout.get("gap_mm", 5))
    xmin, ymin, xmax, ymax = report["map_extent"]
    mm_per_unit = min(width / (xmax - xmin), height / (ymax - ymin))
    points, labels, radii = [], [], []
    total = 0
    for index, frame in enumerate(frames):
        if "gdf" not in frame or not frame.get("label_field"):
            continue
        gdf = frame["gdf"].to_crs(report["display_crs"])
        selected = gdf[(gdf.geometry.geom_type == "Point") & gdf[frame["label_field"]].notna()]
        selected = selected.cx[xmin:xmax, ymin:ymax]
        total += len(selected)
        for geometry in selected.geometry.head(max(0, 512 - len(points))):
            points.append((geometry.x, geometry.y))
            labels.append(index)
            radii.append(max((frame.get("style") or {}).get("size_mm", 3),
                             (frame.get("label_style") or {}).get("size_pt", 9) * 25.4 / 72))
    warnings = []
    pairs = 0
    minimum = None
    if len(points) > 1:
        anchors = np.asarray(points)
        distance = np.linalg.norm(anchors[:, None] - anchors[None, :], axis=2) * mm_per_unit
        upper = np.triu(np.ones(distance.shape, dtype=bool), k=1)
        minimum = float(distance[upper].min())
        footprint = (np.asarray(radii)[:, None] + np.asarray(radii)[None, :]) / 2
        conflicts = upper & (distance < footprint)
        pairs = int(conflicts.sum())
        if pairs:
            indexes = sorted({labels[int(i)] for i in np.where(conflicts)[0]} |
                             {labels[int(i)] for i in np.where(conflicts)[1]})
            warnings.append({
                "code": "dense_point_labels", "layer_indices": indexes,
                "close_anchor_pairs": pairs, "minimum_anchor_distance_mm": minimum,
                "recommendation": "Before rendering, prioritize key labels, reduce symbol "
                                  "size or add a verified detail_extent for the dense cluster. "
                                  "Keep every required layer; do not substitute a caption for "
                                  "required key labels. If issue persists after a consolidated "
                                  "repair, use separate overview/detail maps in this request.",
            })
    return {"scope": "bounded_label_anchor_risk; not a visual overlap test",
            "labeled_point_count": total, "sample_size": len(points),
            "sample_limit": 512, "warnings": warnings}
