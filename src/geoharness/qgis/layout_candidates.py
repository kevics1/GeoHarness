"""Bounded layout proposals over measured subjects; native QGIS validates each one."""

from __future__ import annotations

import copy
import math


def subject_fit_patches(subjects: dict, layout: dict, *, minimum_span: float,
                         padding: float, focus_bounds=None) -> list[dict]:
    """Use all declared selections, never replace a requirement with a smaller count.

    A minimum-count requirement without feature_keys describes the entire source set.
    We preserve that conservative scope; callers can explicitly select stable keys.
    """
    patches = []
    focus_bounds = focus_bounds or {}
    checks = subjects.get("checks", [])
    failed = {item["frame_id"] for item in checks if not item.get("valid", True)}
    for frame_id in sorted(failed):
        field = "main_frame" if frame_id == "main_map" else "detail_frame"
        frame = layout.get(field) or {}
        selected = [item for item in checks if item["frame_id"] == frame_id]
        if any(item.get("missing_keys") or item.get("duplicate_keys")
               or not item.get("rendered")
               or item.get("selected_features", 0) < item.get("minimum_visible_features", 1)
               for item in selected):
            continue  # Layout cannot repair missing identities, omitted layers or absent data.
        bounds = [item["selected_bounds"] for item in selected if item.get("selected_bounds")]
        if frame_id in focus_bounds:
            bounds.append(focus_bounds[frame_id])
        if not bounds:
            continue
        xmin, ymin = min(b[0] for b in bounds), min(b[1] for b in bounds)
        xmax, ymax = max(b[2] for b in bounds), max(b[3] for b in bounds)
        width, height = max(xmax - xmin, minimum_span), max(ymax - ymin, minimum_span)
        pad = frame.get("padding_ratio")
        pad = padding if pad is None else pad
        width, height = width * (1 + 2 * pad), height * (1 + 2 * pad)
        cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
        values = [cx - width / 2, cy - height / 2, cx + width / 2, cy + height / 2]
        if min(width, height) <= 0 or not all(math.isfinite(v) for v in values):
            continue
        if frame.get("focus_layers"):
            patches.append({"path": ["layout", field, "focus_layers"], "operation": "remove"})
        patches.append({"path": ["map_extent"] if frame_id == "main_map" else
                        ["layout", "detail_extent"], "value": values if frame_id == "main_map"
                        else dict(zip(("xmin", "ymin", "xmax", "ymax"), values))})
    return patches


def apply_candidate(payload: dict, patches: list[dict]) -> dict:
    """Apply only locally generated paths to a copy; the public spec editor still validates."""
    candidate = copy.deepcopy(payload)
    candidate.pop("_frozen_frames", None)
    candidate.pop("_layout_zoom_history", None)
    candidate["layout"].pop("layout_plan", None)
    for patch in patches:
        target = candidate
        for key in patch["path"][:-1]:
            target = target.setdefault(key, {})
        key = patch["path"][-1]
        if patch.get("operation") == "remove":
            target.pop(key, None)
        else:
            target[key] = copy.deepcopy(patch["value"])
        if patch["path"] == ["map_extent"]:
            candidate["_explicit_map_extent"] = True
    candidate["_candidate_probe"] = True
    return candidate
