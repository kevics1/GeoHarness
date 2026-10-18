"""Bounded review history and unlimited generation, without changing receipts."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from geoharness.config.settings import GeoConfig

_ISSUE_CODES = {
    "label_overlap", "symbol_overlap", "label_missing", "layer_occlusion",
    "extent_whitespace", "footer_density", "legend_mismatch", "other",
}


def _existing_receipt(metadata: dict[str, Any], map_id: str, config: GeoConfig,
                      *, image_sha256: str = "", page_index: int = 0) -> dict:
    state = metadata.get("map_job_state", {})
    if not isinstance(state, dict) or not state.get("cwd"):
        return {}
    aliases = state.get("aliases", {})
    identity = aliases.get(map_id, map_id) if isinstance(aliases, dict) else map_id
    try:
        identity = str(uuid.UUID(identity))
        cwd = Path(state["cwd"]).resolve()
        configured = Path(config.cartography.jobs_dir).expanduser()
        root = (configured if configured.is_absolute() else cwd / configured).resolve()
        if root == cwd or not root.is_relative_to(cwd):
            return {}
        path = (root / identity / "receipt.json").resolve()
        if not path.is_relative_to(root) or path.stat().st_size > 16 * 1024 * 1024:
            return {}
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(receipt, dict) and receipt.get("storage"):
            from geoharness.cartography import ledger_store

            if path.parent.name != identity or not ledger_store.is_current(path):
                return {}
            return ledger_store.review_tail(path, exclude_image_sha256=image_sha256,
                                            page_index=page_index)
        return receipt if isinstance(receipt, dict) and receipt.get("job_id") == identity else {}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def build_review_iteration_context(
    metadata: dict[str, Any], map_id: str, config: GeoConfig, *,
    receipt: dict[str, Any] | None = None, image_sha256: str = "", page_index: int = 0,
) -> dict[str, Any]:
    """Read at most two earlier visual summaries; expose no raw provider diagnostics."""
    result = {
        "version_limit": None,
        "remaining_render_versions": None,
        "generation_unlimited": True,
        "previous_visual_reviews": [],
    }
    supplied = isinstance(receipt, dict)
    receipt = receipt if supplied else _existing_receipt(
        metadata, map_id, config, image_sha256=image_sha256, page_index=page_index)
    aliases = metadata.get("map_job_state", {}).get("aliases", {})
    identity = aliases.get(map_id, map_id) if isinstance(aliases, dict) else map_id
    if receipt.get("job_id") not in (None, identity):
        return result
    versions = receipt.get("versions", [])
    if not isinstance(versions, list):
        return result
    if supplied and page_index:
        versions = [{"version": v.get("version"),
                     "image_sha256": v["page_reviews"][str(page_index)].get("image_sha256"),
                     "stages": {"visual_checked": v["page_reviews"][str(page_index)]}}
                    for v in versions if isinstance(v, dict)
                    and str(page_index) in v.get("page_reviews", {})]
    earlier = [version for version in versions if isinstance(version, dict)
               and version.get("image_sha256") != image_sha256]
    for version in earlier[-2:]:
        stages = version.get("stages", {})
        visual = stages.get("visual_checked", {}) if isinstance(stages, dict) else {}
        report = visual.get("report", {}) if isinstance(visual, dict) else {}
        if not isinstance(report, dict):
            continue
        issues = report.get("issues", [])
        if not isinstance(issues, list):
            continue
        bounded = []
        for issue in issues[:5]:
            if not isinstance(issue, dict) or issue.get("severity") not in {"error", "warning"}:
                continue
            code = issue.get("issue_code", "other")
            bounded.append({
                "issue_code": code if isinstance(code, str) and code in _ISSUE_CODES else "other",
                "severity": issue["severity"],
                "description": str(issue.get("description", ""))[:240],
                "location": str(issue.get("location", ""))[:120],
                "frame_id": str(issue.get("frame_id", ""))[:80],
                "roi_ref": str(issue.get("roi_ref", ""))[:100],
                "bbox": issue.get("bbox"),
                "feature_ids": (issue["feature_ids"][:64]
                                if isinstance(issue.get("feature_ids"), list) else []),
                "source_indices": (issue["source_indices"][:64]
                                   if isinstance(issue.get("source_indices"), list) else []),
            })
        number = version.get("version")
        status = report.get("status", visual.get("status", "unverified"))
        result["previous_visual_reviews"].append({
            "page_index": page_index,
            "image_sha256": version.get('image_sha256', ''),
            "version": number if isinstance(number, int) and not isinstance(number, bool) else None,
            "status": (status if status in ("passed", "issues", "unverified", "stale")
                       else "unverified"),
            "issues": bounded,
        })
    return result
