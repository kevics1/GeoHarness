"""Describe only the selected, hash-checked delivered project and image."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pyproj import CRS

from geoharness._proj_fix import ensure_proj_data


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _selected_manifest(
    path: Path | None, expected: dict[str, str], image_sha256: str | None,
) -> tuple[dict, str]:
    """No global latest state, request parameters or arbitrary attachments are evidence."""
    if path is None or not image_sha256 or str(path) not in expected:
        return {}, "unverified"
    root = path.parent.resolve()
    try:
        for filename, digest in expected.items():
            candidate = Path(filename).resolve()
            if not candidate.is_relative_to(root) or _sha(candidate) != digest:
                return {}, "changed"
        if path.stat().st_size > 4 * 1024 * 1024:
            return {}, "unverified"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("preview_sha256") != image_sha256:
            return {}, "unverified"
        declared = (manifest.get("files") if manifest.get("backend") == "qgis" else
                    ["map.qgs", "redraw.py", "README.md",
                     *(layer.get("file") for layer in manifest.get("layers", []))])
        if not isinstance(declared, list) or not declared:
            return {}, "unverified"
        for reference in [*declared, manifest.get("project_file", "map.qgs")]:
            if not isinstance(reference, str) or not reference:
                return {}, "unverified"
            relative_file = Path(reference)
            candidate = (root / relative_file).resolve()
            if (relative_file.is_absolute() or relative_file.drive
                    or ".." in relative_file.parts or not candidate.is_relative_to(root)
                    or str(candidate) not in expected):
                return {}, "unverified"
        relative = Path(manifest.get("preview", ""))
        preview = (root / relative).resolve()
        if (relative.is_absolute() or relative.drive or ".." in relative.parts
                or not preview.is_relative_to(root) or str(preview) not in expected
                or _sha(preview) != image_sha256):
            return {}, "unverified"
        return manifest, "passed"
    except (OSError, ValueError, TypeError):
        return {}, "unverified"


def _crs_details(value: Any) -> dict:
    if not isinstance(value, str) or not value:
        return {"identifier": value, "official_name": None, "status": "unverified"}
    try:
        ensure_proj_data()
        crs = CRS.from_user_input(value)
    except Exception:
        return {"identifier": value, "official_name": None, "status": "unverified"}
    authority = crs.to_authority()
    return {"identifier": ":".join(authority) if authority else value,
            "official_name": crs.name, "status": "verified_by_pyproj"}


def _attached_analysis(delivery: dict) -> list[dict]:
    """只核对已复制包内的报告和数据；绝不沿报告中的任意源路径读取。"""
    from geoharness.execution.contracts import artifact_facts, check_claims

    if not delivery.get('delivery_dir'):
        return []
    root = Path(delivery['delivery_dir']).resolve()
    files = [Path(p).resolve() for p in delivery.get('files', [])
             if isinstance(p, str) and Path(p).resolve().is_relative_to(root)]
    reports = []
    for path in files:
        if path.suffix.lower() != '.json' or not path.is_file() or path.stat().st_size > 1024*1024:
            continue
        try:
            report = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(report, dict) or report.get('kind') not in {
                'persisted_analysis_report', 'analysis_result_package'}:
            continue
        try:
            if not isinstance(report.get('method'), dict) or not report['method']:
                raise ValueError('Analysis method declaration missing')
            facts = report['facts']
            sources = [p for p in files if p != path and p.is_file()
                       and _sha(p) == facts['sha256']]
            if not sources:
                raise ValueError('Matching source not delivered')
            if report.get('kind') == 'analysis_result_package':
                from geoharness.analysis.result_package import FactRequest, _members, _selection

                request = FactRequest.model_validate(report['request'])
                inventory = report['source_inventory']
                copied_hashes = {_sha(p) for p in files if p.is_file()}
                if (not isinstance(inventory, dict) or not inventory
                        or not set(inventory.values()) <= copied_hashes):
                    raise ValueError('Result source companions not delivered intact')
                if (report.get('units') != request.units or report['method'] != request.method
                        or report['selection'] != _selection(request, facts, inventory,
                            _members(sources[0], request.key))):
                    raise ValueError('Result selection or declarations disagree')
                package_measured = artifact_facts(sources[0], key=request.key,
                    numeric_fields=request.numeric_fields, group_by=request.group_by,
                    predicates=request.predicates, include_median=True)
                if ({k: v for k, v in package_measured.items() if k != 'source'} !=
                        {k: v for k, v in facts.items() if k != 'source'}):
                    raise ValueError('Result request disagrees with delivered statistics')
                if request.nearest_checks:
                    from geoharness.analysis.result_package import _nearest_checks

                    previous = report.get('nearest_checks', [])
                    if len(previous) != len(request.nearest_checks):
                        raise ValueError('Nearest verification evidence missing')
                    copied = {}
                    for check, saved in zip(request.nearest_checks, previous):
                        hashes = saved['reference_inventory']
                        if not set(hashes.values()) <= copied_hashes:
                            raise ValueError('Nearest reference companions not delivered intact')
                        digest = hashes[saved['reference']]
                        copied[check.reference_ref] = next(p for p in files if _sha(p) == digest)
                    fresh = _nearest_checks(sources[0], request, copied.__getitem__)
                    for actual_check, saved in zip(fresh, previous):
                        if any(actual_check.get(k) != v for k, v in saved.items()
                               if k not in {'reference', 'reference_inventory'}):
                            raise ValueError('Nearest check disagrees with delivered geometry')
            measured = artifact_facts(sources[0], key=facts['key'],
                numeric_fields=list(facts.get('numeric', {})),
                group_by=list(facts.get('groups', {})),
                predicates=[item['predicate'] for item in facts.get('counts', {}).values()],
                include_median=any('median' in v for v in facts.get('numeric', {}).values()),
                stratify_by=facts.get('stratify_by', ()))
            expected = {k: v for k, v in facts.items() if k != 'source'}
            actual = {k: v for k, v in measured.items() if k != 'source'}
            if actual != expected:
                raise ValueError('Report statistics disagree with copied source')
            claims = report.get('claims')
            if claims:
                raw = [{k: v for k, v in item.items() if k in {
                    'path', 'expected', 'absolute_tolerance'}} for item in claims['checks']]
                if check_claims(measured, raw)['status'] != 'passed':
                    raise ValueError('Report claims disagree with copied source')
            reports.append({'report': str(path), 'source': str(sources[0]),
                'source_sha256': measured['sha256'], 'facts': measured,
                'status': 'persisted_values_checked', 'method': report.get('method'),
                'execution_evidence': report.get('execution_evidence', {}),
                'selection': report.get('selection'), 'units': report.get('units', {}),
                'method_status': 'declared_not_independently_verified',
                'analysis_correctness': 'unverified'})
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            reports.append({'report': str(path), 'status': 'unverified',
                            'reason': '报告与包内数据未能完成一致性核对'})
    return reports


def build_delivery_summary(
    delivery: dict, receipt: dict, *, manifest_path: Path | None,
    package_hashes: dict[str, str],
) -> dict:
    """Read the copied selected package after its snapshot hashes were checked."""
    primary = delivery.get("primary_map")
    actual_digest = _sha(Path(primary)) if primary else None
    manifest, integrity = _selected_manifest(manifest_path, package_hashes, actual_digest)
    if delivery.get("snapshot_status") == "changed":
        manifest, integrity = {}, "changed"
    layout = manifest.get("layout") or {}
    layout = layout if isinstance(layout, dict) else {}
    frames = []
    for frame in layout.get("map_frames", []):
        if isinstance(frame, dict):
            frames.append({**frame, "crs_details": _crs_details(frame.get("crs")),
                           "scope": "selected_saved_layout"})
    if not frames and manifest.get("project_crs"):
        frames.append({"id": "main_map", "crs": manifest["project_crs"],
                       "crs_details": _crs_details(manifest["project_crs"]),
                       "scope": "declared_project_crs", "extent": manifest.get("extent")})
    renderer_counts = []
    for index, layer in enumerate(manifest.get("layers", [])):
        if not isinstance(layer, dict):
            continue
        style = layer.get("style") or {}
        style = style if isinstance(style, dict) else {}
        categories = layer.get("categories", style.get("categories"))
        ranges = layer.get("ranges", style.get("ranges"))
        renderer_counts.append({
            "layer_index": layer.get("source_index", index),
            "name": layer.get("name", layer.get("label")),
            "renderer": layer.get("renderer", style.get("type")),
            "category_count": len(categories) if isinstance(categories, list) else None,
            "range_count": len(ranges) if isinstance(ranges, list) else None,
            "scope": "selected_project_renderer; not_bounded_geometry_samples",
        })
    analysis = manifest.get("analysis") or {}
    analysis = analysis if isinstance(analysis, dict) else {}
    analysis_status = analysis.get("status", "unverified")
    if analysis_status == "verified":
        explanation = (
            "原生分析已生成持久化结果；verified 表示按各条 verification_scope 核对"
            "输出，旧清单仅核对计数，提供指纹的输出还核对 SHA256，面积统计还从"
            "输出几何重新计算。原始源数据尚未独立重新分析，"
            "因此 analysis_correctness=unverified 不表示分析未执行。"
        )
    elif analysis_status == "historical_unverified":
        explanation = (
            "工程保留原分析的历史统计与参数。用户编辑后的当前导出未重新绑定并验证"
            "这些分析事实；历史统计不能作为当前地图的已验证统计。"
        )
    else:
        explanation = (
            "本次选中且校验的工程未提供已验证分析事实；不能据此判断分析是否执行。"
            "analysis_correctness 记录原始源数据的独立重新分析验证状态。"
        )
    stages = receipt.get("stages") or {}
    selected_version = (receipt.get("selected_version") if receipt.get("artifact_registered")
                        else delivery.get("selection", {}).get("selected_version"))
    analysis_reports = _attached_analysis(delivery)
    page_reviews = {}
    for version in receipt.get('versions', []):
        if isinstance(version, dict) and version.get('version') == selected_version:
            page_reviews = {page: {'status': r.get('status', 'unverified'),
                'review_path': (r.get('report_reference') or {}).get('path'),
                'issues': (r.get('report') or {}).get('issues', []),
                'limitations': (r.get('report') or {}).get('limitations', [])} for page, r in (
                    version.get('page_reviews') or {}).items() if isinstance(r, dict)}
            break
    remaining = [dict(issue) for issue in delivery.get('remaining_issues') or []
                 if isinstance(issue, dict)]
    for page, review in page_reviews.items():
        for issue in review['issues']:
            if isinstance(issue, dict) and not any(
                    all(existing.get(k) == issue.get(k) for k in (
                        'severity', 'description', 'location', 'frame_id'))
                    for existing in remaining):
                remaining.append({**issue, 'page_index': page})
    issue_count = sum(isinstance(issue, dict) and issue.get('severity') in {'warning', 'error'}
                      for issue in remaining)
    notice = (f"交付质量：{delivery.get('quality_status', 'draft')}；"
              f"视觉审查：{delivery.get('review_status', 'unverified')}；"
              f"当前已列出{issue_count}项错误或警告，其他逐页限制见审查记录。分析科学正确性："
              f"{receipt.get('analysis_correctness', 'unverified')}。"
              "工程打包或文件存在不代表分析与视觉质量均已通过。")
    return {
        "schema_version": 1,
        "primary_map": primary,
        "primary_map_sha256": actual_digest,
        "selected_version": selected_version,
        "selection": delivery.get("selection", {}),
        "selected_manifest": str(manifest_path) if manifest_path else None,
        "selected_manifest_sha256": package_hashes.get(str(manifest_path)),
        "package_integrity": integrity,
        "quality_status": delivery.get("quality_status", "draft"),
        "review_status": delivery.get("review_status", "unverified"),
        "report_status": {name: stage.get("status", "unverified")
                          for name, stage in stages.items() if isinstance(stage, dict)},
        "project_fidelity": manifest.get("project_fidelity", "unverified"),
        "map_frames": frames,
        "renderer_counts": renderer_counts,
        "actual_legend": layout.get("legends", {}),
        "analysis_status": analysis_status,
        "analysis_correctness": receipt.get("analysis_correctness", "unverified"),
        "analysis_explanation": explanation,
        "analysis": analysis,
        "attached_analysis_reports": analysis_reports,
        "final_answer_facts": [{"report": r['report'], "source_sha256": r['source_sha256'],
            "row_count": r['facts']['row_count'], "numeric": r['facts']['numeric'],
            "groups": r['facts']['groups'], "counts": r['facts']['counts'],
            "strata": r['facts'].get('strata', []), "method": r.get('method'),
            "selection": r.get('selection'), "units": r.get('units', {}),
            "analysis_correctness": 'unverified',
            "execution_evidence": r.get('execution_evidence', {})}
            for r in analysis_reports if r['status'] == 'persisted_values_checked'],
        "page_reviews": page_reviews,
        "remaining_issues": remaining,
        "historical_findings": delivery.get('historical_findings', []),
        "quality_notice": notice,
        "delivered_files": delivery.get('files', []),
    }


def _markdown(summary: dict) -> str:
    lines = ["# 地图交付摘要", "", f"- 主图：{summary['primary_map'] or '无'}",
             f"- PNG/主图 SHA256：`{summary['primary_map_sha256'] or 'unverified'}`",
             f"- 选中版本：{summary['selected_version'] or '未登记'}",
             f"- 质量：{summary['quality_status']}；视觉审查：{summary['review_status']}",
             f"- 工程一致性：{summary['project_fidelity']}；包校验：{summary['package_integrity']}",
             f"- 实际选中工程清单：{summary['selected_manifest'] or '无'}", ""]
    if summary["report_status"]:
        lines.append("检查状态：" + "；".join(
            f"{name}={status}" for name, status in summary["report_status"].items()))
        lines.append("")
    for frame in summary["map_frames"]:
        details = frame["crs_details"]
        lines.append(f"- 图框 {frame.get('id', '')}：{details['identifier']} — "
                     f"{details['official_name'] or '官方名称未核实'}")
    for renderer in summary["renderer_counts"]:
        counts = []
        if renderer["category_count"] is not None:
            counts.append(f"类别 {renderer['category_count']}")
        if renderer["range_count"] is not None:
            counts.append(f"分级范围 {renderer['range_count']}")
        lines.append(f"- 图层 {renderer['name']}：{renderer['renderer']}"
                     + ("；" + "；".join(counts) if counts else ""))
    lines.extend(["", f"分析事实状态：{summary['analysis_status']}。",
                  summary["analysis_explanation"], "",
                  "实际图例节点、完整工程事实及状态见 summary.json；"
                  "分析统计、参数、输出指纹与完整来源备注保留在工程事实报告中。", ""])
    lines.extend([summary['quality_notice'], ''])
    for page, review in summary['page_reviews'].items():
        lines.append(f"- 页面 {page}：{review['status']}")
    for report in summary['attached_analysis_reports']:
        lines.append(f"- 分析事实报告：{report['report']}；核对状态：{report['status']}")
    for issue in summary['remaining_issues']:
        if isinstance(issue, dict):
            lines.append(f"- 待解决（{issue.get('severity', 'unknown')}）："
                         f"{issue.get('description', '')}")
    history = summary.get('historical_findings', [])
    if history:
        lines.extend(['', f'历史待处置记录：{len(history)}项；未计入当前页面错误/警告。',
                      '稳定issue_id、旧图片身份和处置状态见summary.json。'])
    return "\n".join(lines)


def write_delivery_summary(
    directory: Path, delivery: dict, receipt: dict, *, manifest_path: Path | None,
    package_hashes: dict[str, str],
) -> dict[str, Any]:
    summary = build_delivery_summary(delivery, receipt, manifest_path=manifest_path,
                                     package_hashes=package_hashes)
    json_path, markdown_path = directory / "summary.json", directory / "summary.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(summary), encoding="utf-8")
    return {"summary_json": str(json_path), "summary_markdown": str(markdown_path),
            "quality_notice": summary['quality_notice'],
            "analysis_correctness": summary['analysis_correctness'],
            "final_answer_facts": summary['final_answer_facts'],
            "remaining_issues": summary['remaining_issues']}
