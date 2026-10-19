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
    hard_conflicts = []
    if len(points) > 1:
        positions = np.asarray(points)
        distance = np.linalg.norm(positions[:, None] - positions[None, :], axis=2) * mm_per_unit
        upper = np.triu(np.ones(distance.shape, dtype=bool), k=1)
        minimum = float(distance[upper].min())
        footprint = (np.asarray(radii)[:, None] + np.asarray(radii)[None, :]) / 2
        conflicts = upper & (distance < footprint)
        pairs = int(conflicts.sum())
        diameters = np.asarray([a.get('symbol_diameter_mm', 0) for a in anchors])
        # Exactly shared anchors may represent intentional coincident records
        # or thematic overlays. Their meaning cannot be inferred from geometry;
        # keep a risk finding instead of banning that existing rendering behavior.
        coincident = upper & (distance == 0)
        hard = upper & ~coincident & (diameters[:, None] > 0) & (diameters[None, :] > 0) & (
            distance < (diameters[:, None] + diameters[None, :]) / 2)
        # Label font size is a risk estimate, never a measured symbol footprint.
        conflicts |= hard
        pairs = int(conflicts.sum())
        if pairs:
            indexes = sorted({labels[int(i)] for i in np.where(conflicts)[0]} |
                             {labels[int(i)] for i in np.where(conflicts)[1]})
            conflict_ids = sorted({
                (int(i), int(j)) for i, j in zip(*np.where(conflicts))
            })[:64]
            detail = {"code": "dense_point_labels", "layer_indices": indexes,
                "close_anchor_pairs": pairs, "minimum_anchor_distance_mm": minimum,
                "coincident_anchor_pairs": int(coincident.sum()),
                "conflict_sample": [{"index": i, "other_index": j}
                                    for i, j in conflict_ids],
                "recommendation": "Before rendering, prioritize key labels, reduce symbol "
                    "size or add a verified detail_extent for the dense cluster. "
                    "Keep every required layer; do not substitute a caption for "
                    "required key labels. If issue persists after a consolidated "
                    "repair, use separate overview/detail maps in this request. "
                    "Exactly coincident records require checking the source and intended "
                    "aggregation/overlay semantics; do not move their true coordinates."}
            warnings.append(detail)
            # Two point symbols whose drawn footprints touch cannot be separated
            # by wording, a different label anchor or another visual review. This
            # is a deterministic paper-scale conflict and must be resolved before
            # rendering, not discovered after several review cycles.
            hard_ids = [(int(i), int(j)) for i, j in zip(*np.where(hard))]
            if hard_ids:
                hard_conflicts.append({**detail, "severity": "hard",
                    "hard_pair_count": len(hard_ids),
                    "hard_pairs": [{"index": i, "other_index": j,
                                    "layer_index": anchors[i]['layer_index'],
                                    "other_layer_index": anchors[j]['layer_index'],
                                    "anchor_feature_id": anchors[i].get("feature_id"),
                                    "other_feature_id": anchors[j].get("feature_id")}
                                   for i, j in hard_ids[:64]],
                    "resolution": "reduce_symbol_or_add_detail_map"})
    return {"scope": "bounded_label_anchor_risk; not a visual overlap test",
            "labeled_point_count": labeled_point_count, "sample_size": len(points),
            "sample_limit": 512, "warnings": warnings,
            "hard_conflicts": hard_conflicts,
            "hard_conflict_count": len(hard_conflicts),
            "symbol_subjects": [{'layer_index': a['layer_index'],
                'feature_id': a.get('feature_id')} for a in anchors
                if a.get('symbol_diameter_mm', 0) > 0]}


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
    symbolized = 0
    for index, frame in enumerate(frames):
        if frame.get("role") == "table_only" or "gdf" not in frame:
            continue
        gdf = frame["gdf"].to_crs(report["display_crs"])
        points = gdf[gdf.geometry.geom_type == "Point"]
        if points.empty:
            continue
        label_field = frame.get("label_field")
        has_labels = bool(label_field and label_field in points.columns)
        if has_labels:
            selected = points[points[label_field].notna()]
            total += len(selected)
        else:
            selected = points
        selected = selected.cx[xmin:xmax, ymin:ymax]
        style = frame.get("style") or {}
        sizes = [style.get("size_mm") or 3]
        sizes.extend(item.get("size_mm") or sizes[0] for item in
                     [*(style.get("categories") or []), *(style.get("ranges") or [])])
        radius = max(sizes)
        if has_labels:
            radius = max(radius,
                ((frame.get("label_style") or {}).get("size_pt") or 9) * 25.4 / 72)
        for geometry in selected.geometry.head(max(0, 512 - len(anchors))):
            anchors.append({"x": geometry.x, "y": geometry.y,
                "layer_index": frame.get("layer_index", index),
                "feature_id": getattr(geometry, "name", None),
                "radius_mm": radius, "labeled": has_labels})
            symbolized += 1
    result = composition_from_anchors(anchors, labeled_point_count=total,
        bounds=report["map_extent"], frame_mm=[width, height])
    result["symbolized_point_count"] = symbolized
    return result
