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
    return "\n".join(lines)


def write_delivery_summary(
    directory: Path, delivery: dict, receipt: dict, *, manifest_path: Path | None,
    package_hashes: dict[str, str],
) -> dict[str, str]:
    summary = build_delivery_summary(delivery, receipt, manifest_path=manifest_path,
                                     package_hashes=package_hashes)
    json_path, markdown_path = directory / "summary.json", directory / "summary.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_markdown(summary), encoding="utf-8")
    return {"summary_json": str(json_path), "summary_markdown": str(markdown_path)}
