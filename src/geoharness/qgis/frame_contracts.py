"""Pure evaluation of declared frame subjects and bounded paper proposals."""

from __future__ import annotations

import math


def evaluate_requirements(measurements: list[dict]) -> dict:
    checks = []
    for value in measurements:
        minimum = value.get("minimum_visible_features", 1)
        missing = value.get("missing_keys", [])
        duplicate = value.get("duplicate_keys", [])
        complete = value.get("coverage", "intersects") == "complete"
        valid = (not missing and not duplicate and value.get("frame_present", True)
                 and value.get("rendered", True)
                 and value["visible_features"] >= minimum
                 and (not complete or value["complete_features"] == value["selected_features"]))
        checks.append({**value, "valid": valid})
    return {"scope": "declared_frame_subjects", "checked": bool(checks), "checks": checks,
            "valid": all(check["valid"] for check in checks)}


def paper_limits(original: dict, current: dict) -> dict:
    """Keep every proposal inside both the public API and an optional growth envelope."""
    ratio = original.get("auto_expand_max_ratio")
    width = min(2000, original.get("width_mm", 297) * ratio) if ratio else 2000
    height = min(2000, original.get("height_mm", 210) * ratio) if ratio else 2000
    return {"width_mm": width, "height_mm": height, "sidebar_width_mm": 500,
            "within_limits": (current.get("width_mm", 297) <= width + 1e-7
                and current.get("height_mm", 210) <= height + 1e-7
                and current.get("sidebar_width_mm", 60) <= 500)}


def joint_paper_proposal(spec: dict, regions: dict, zoom: float, required: float,
                         content_fraction=(1.0, 1.0), *, original=None) -> dict | None:
    """Propose existing public dimensions without changing a geographic subject.

    This is a candidate, not a claim that coupled map scales will meet the target.
    The native caller measures once again and stops when no valid proposal exists.
    """
    if not math.isfinite(zoom) or zoom <= 0 or not math.isfinite(required):
        return None
    factor = required * 1.01 / zoom
    detail = regions["detail_map"]
    if spec.get("detail_placement", "sidebar") == "page":
        width = max(spec.get("width_mm", 297),
                    detail[2] * factor * content_fraction[0] + 2 * spec.get("margin_mm", 12))
        height = max(spec.get("height_mm", 210),
                     detail[3] * factor * content_fraction[1] + 2 * spec.get("margin_mm", 12))
        candidate = {**spec, "width_mm": width, "height_mm": height}
    else:
        sidebar = max(detail[2], detail[2] * factor * content_fraction[0])
        candidate = {**spec, "sidebar_width_mm": sidebar,
                     "width_mm": spec.get("width_mm", 297) + sidebar - detail[2]}
        map_height = regions["main_map"][3]
        detail_height = max(20, detail[3] * factor * content_fraction[1])
        if spec.get("legend", True):
            needed = max(map_height, detail_height / 0.8,
                         detail_height + spec.get("gap_mm", 5) + regions["legend"][3])
            candidate["detail_height_ratio"] = max(0.1, min(0.8, detail_height / needed))
        else:
            needed = max(map_height, detail_height)
        candidate["height_mm"] = spec.get("height_mm", 210) + needed - map_height
    return candidate if paper_limits(original or spec, candidate)["within_limits"] else None
