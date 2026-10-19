"""Stable image review contract: pixels + page purpose + rules, not prose retries."""

from __future__ import annotations

import hashlib
import json
from typing import Any

CONTRACT_VERSION = 11

def _bounded(value: Any, depth: int = 0) -> Any:
    if depth > 5:
        return "[depth]"
    if value is None or type(value) in (bool, int, float, str):
        return value
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth + 1) for item in list(value)[:64]]
    if isinstance(value, dict):
        return {str(key): _bounded(item, depth + 1)
                for key, item in list(value.items())[:64]}
    return str(value)[:2000]

def page_contract(evidence: dict[str, Any], *, task: str = "") -> dict[str, Any]:
    """Stable page/scope identity. Free task prose is recorded but not cache identity."""
    from geoharness.cartography.pixel_review import page_scope

    scope = page_scope(evidence if evidence.get("render_metadata") else
                       {"render_metadata": evidence})
    # Workflow passes analyzed render metadata as evidence["render_metadata"];
    # Direct adapters may already provide it at the top level.
    metadata = evidence.get("render_metadata") or evidence
    frames = (metadata.get("native_facts") or {}).get("frames") or {}
    page = scope.get("page_index", 0)
    page_frames = {
        name: {key: value for key, value in frame.items() if key in {
            "page_index", "purpose", "label_sources", "rect_page_mm", "rect_mm",
        }}
        for name, frame in frames.items()
        if isinstance(frame, dict) and frame.get("page_index", 0) == page
    }
    layout = metadata.get("layout") or {}
    regions = (layout.get("page_regions") or {}).get(str(page),
        layout.get("regions") if page == 0 else None)
    return {
        "contract_version": CONTRACT_VERSION,
        "page_index": page,
        "page_role": scope.get("page_role", "unknown"),
        "frames": page_frames,
        "regions": _bounded(regions),
        "detail_placement": (layout.get("page") or {}).get("detail_placement"),
        "analysis_status": ((metadata.get("analysis") or {}).get("status")
                            if isinstance(metadata.get("analysis"), dict) else None),
        "analysis_manifest_count": len(
            ((metadata.get("analysis") or {}).get("analyses") or [])
            if isinstance(metadata.get("analysis"), dict) else []),
        # A changed fact package must invalidate a pixel conclusion; the actual
        # manifest path/order is not part of the stable identity.
        "analysis_manifest_digests": sorted(
            str(item.get("manifest_digest") or item.get("output_fingerprints") or "")
            for item in ((metadata.get("analysis") or {}).get("analyses") or [])
            if isinstance(item, dict)),
        "backend": evidence.get("backend", "matplotlib"),
        "checks": sorted(set(evidence.get("review_checks") or [])),
        "task_scope": task.strip()[:240],
    }

def stable_contract(evidence: dict[str, Any]) -> dict[str, Any]:
    """Review inputs only; package provenance and print order are separate bindings.

    Page role/purpose, measured regions and explicit checks do
    change the question asked of the same pixels, so they remain in identity.
    Page index, prose wording and manifest path/order do not.
    """
    contract = page_contract(evidence)
    for key in ('task_scope', 'page_index', 'detail_placement', 'analysis_status',
                'analysis_manifest_count', 'analysis_manifest_digests'):
        contract.pop(key, None)
    for frame in contract['frames'].values():
        frame.pop('page_index', None)
    return contract


def review_identity(evidence: dict[str, Any], *, task: str = "") -> str:
    return hashlib.sha256(json.dumps(stable_contract(evidence), ensure_ascii=False,
                                     sort_keys=True, default=str).encode('utf-8')).hexdigest()


def add_scope(evidence: dict[str, Any], *, task: str = "") -> dict[str, Any]:
    """Attach the stable contract to evidence without trusting caller-supplied IDs."""
    result = {**evidence}
    result["review_scope_contract"] = page_contract(result, task=task)
    # Task prose is retained for the reviewer, never as cache identity.
    result["review_scope_contract"]["task_scope"] = task.strip()[:240]
    result["review_scope_identity"] = review_identity(result, task=task)
    return result
