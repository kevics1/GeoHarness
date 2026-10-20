"""Stable image review contract: pixels + page purpose + rules, not prose retries."""

from __future__ import annotations

import hashlib
import json
from typing import Any

CONTRACT_VERSION = 14

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
            "page_index", "purpose", "label_sources", "rect_page_mm", "rect_mm", "extent",
        }}
        for name, frame in frames.items()
        if isinstance(frame, dict) and frame.get("page_index", 0) == page
    }
    layout = metadata.get("layout") or {}
    regions = (layout.get("page_regions") or {}).get(str(page),
        layout.get("regions") if page == 0 else None)
    # These requirements belong to the page duty, not to the model's prose.
    # A roster and an overview must never receive the same acceptance question
    # merely because both were passed through all_pages=true.
    duty_checks = {
        "map": ["visible_extent", "legend_matches_drawn_content", "readable_labels",
                "no_occlusion", "no_clipping"],
        "detail": ["visible_extent", "legend_matches_drawn_content", "readable_labels",
                   "no_occlusion", "no_clipping", "detail_subjects_visible"],
        "roster": ["table_headers_visible", "cells_readable", "no_text_collision",
                   "no_cell_clipping", "pagination_footer_consistent"],
        "locator": ["visible_extent", "readable_labels", "no_clipping"],
        "mixed": ["readable_labels", "no_clipping", "legend_matches_drawn_content"],
        "unknown": ["readable_labels", "no_clipping"],
    }.get(scope.get("page_role", "unknown"), ["readable_labels", "no_clipping"])
    return {
        "contract_version": CONTRACT_VERSION,
        "page_index": page,
        "page_role": scope.get("page_role", "unknown"),
        "page_id": scope.get('page_id'),
        "purpose": scope.get('purpose', ''),
        "required_content": scope.get('required_content', []),
        "frames": page_frames,
        "regions": _bounded(regions),
        "paper": {k: (layout.get('effective_parameters') or {}).get(k)
                  for k in ('width_mm', 'height_mm')},
        "duty_checks": duty_checks,
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
        # Free prose is not an acceptance rule. Explicit checks are normalized
        # to stable, page-relevant IDs before they can become review identity.
        "checks": normalized_checks(evidence.get("review_checks") or [], scope.get(
            "page_role", "unknown")),
        "page_acceptance_requirements": sorted(set(
            evidence.get("page_acceptance_requirements") or [])),
        "task_scope": task.strip()[:240],
    }

def normalized_checks(checks: Any, page_role: str = "unknown") -> list[str]:
    """Convert known intent to stable IDs; keep unknown requirements explicit.

    Progress prose such as "final check" must not invalidate a conclusion. A
    genuinely new requirement that cannot be mapped remains part of the
    contract, because silently dropping it would weaken review acceptance.
    """
    if not isinstance(checks, (list, tuple)):
        return []
    aliases = {
        "label_overlap": {"label_overlap", "标签重叠", "编号重叠", "文字重叠", "标注重叠"},
        "label_readability": {"label_readability", "文字可读", "编号可读", "标签可读",
                              "字体太小", "字太小"},
        "symbol_overlap": {"symbol_overlap", "符号重叠", "点重叠", "点位重叠"},
        "layer_occlusion": {"layer_occlusion", "图层遮挡", "压盖", "被面盖住"},
        "legend_consistency": {"legend_consistency", "图例一致", "图例对应", "图例不符"},
        "extent_whitespace": {"extent_whitespace", "范围留白", "大片空白", "过多空白"},
        "pagination": {"pagination", "分页", "页脚", "行号", "页码"},
        "table_readability": {"table_readability", "表格可读", "表头", "单元格"},
        "clipping": {"clipping", "裁切", "截断", "溢出"},
    }
    ignored = {"最终复核", "再检查一次", "再审一次", "确认修复", "再次审查",
               "final check", "final review", "recheck", "再确认一次"}
    result = set()
    for item in checks[:16]:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if not text:
            continue
        lowered = text.lower()
        if lowered in ignored or text in ignored:
            continue
        matched = {key for key, words in aliases.items()
                   if any(word.lower() in lowered for word in words)}
        if matched:
            result.update(matched)
        else:
            result.add(text[:160])
    return sorted(result)

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
