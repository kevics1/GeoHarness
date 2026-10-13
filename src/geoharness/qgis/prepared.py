"""Hash-bound, workspace-local preflight capsules for short native create calls."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path


def _directory(cwd, settings):
    root = Path(cwd).resolve()
    value = Path(settings.plans_dir).expanduser()
    path = (value if value.is_absolute() else root / value).absolute()
    if path.resolve() != path or not path.is_relative_to(root) or path == root:
        raise ValueError("qgis.plans_dir must be a direct workspace subdirectory without links")
    return path


def save_preflight(parameters: dict, report: dict, *, cwd, settings) -> tuple[str, Path]:
    content = json.dumps({"parameters": parameters, "preflight": report}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    directory = _directory(cwd, settings)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + ".json")
    if path.resolve() != path:
        raise ValueError("Prepared preflight file cannot be a link")
    path.write_bytes(content)
    return "qgis-plan:" + digest, path


def load_preflight(reference: str, *, cwd, settings) -> dict:
    if not re.fullmatch(r"qgis-plan:[0-9a-f]{64}", reference):
        raise ValueError("plan_ref requires an actual returned qgis-plan reference")
    digest = reference.split(":", 1)[1]
    path = _directory(cwd, settings) / (digest + ".json")
    if path.resolve() != path or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Prepared preflight is missing, linked or oversized; run preflight again")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Prepared preflight changed; run preflight again")
    packet = json.loads(content)
    if (not isinstance(packet, dict) or not isinstance(packet.get("parameters"), dict)
            or not isinstance(packet.get("preflight"), dict)):
        raise ValueError("Prepared preflight capsule is incomplete")
    return packet


def diagnostic_files(*, cwd, settings) -> tuple[Path, ...]:
    """Grant exact hash-verified capsules only, never the directory or configuration."""
    try:
        directory = _directory(cwd, settings)
    except (OSError, ValueError):
        return ()
    if not directory.is_dir():
        return ()
    candidates = []
    for path in directory.glob("*.json"):
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    paths = [path for _, path in sorted(candidates, reverse=True)]
    files = []
    for path in paths[:64]:
        if re.fullmatch(r"[0-9a-f]{64}", path.stem):
            try:
                load_preflight("qgis-plan:" + path.stem, cwd=cwd, settings=settings)
                files.append(path)
            except (OSError, ValueError, TypeError):
                continue
    return tuple(files)


def summary(report: dict, reference: str, path: Path) -> dict:
    result = {key: report[key] for key in (
        "status", "display_crs", "map_extent", "map_extent_source", "warnings", "issues",
    ) if key in report}
    result["layers"] = [{"layer_index": index, **{key: layer[key] for key in (
        "label", "kind", "feature_count", "visible_main", "visible_detail", "symbolization_field",
        "label_field", "issues", "warnings",
    ) if key in layer}} for index, layer in enumerate(report.get("layers", [])[:16])]
    result["layers_omitted"] = max(0, len(report.get("layers", [])) - 16)
    layout = report.get("layout") or {}
    effective = layout.get("effective_parameters") or {}
    result["layout"] = {"effective_parameters": {key: effective[key] for key in (
        "width_mm", "height_mm", "sidebar_width_mm", "detail_height_ratio", "footer_height_mm",
        "body_font_size_pt", "legend_font_size_pt") if key in effective},
        "bounds_check": {key: (layout.get("bounds_check") or {}).get(key)
                         for key in ("checked", "valid", "problems")},
        "adaptations": layout.get("adaptations", []),
        "label_tables": [{key: table[key] for key in ("id", "rows", "visible_rows", "fields")
                          if key in table} for table in layout.get("label_tables", [])]}
    result.update(plan_ref=reference, report_path=str(path),
        create_call={"action": "create", "plan_ref": reference},
        note="Full preflight and original parameters are in report_path. "
             "Use create_call unchanged; altered sources/fonts/styles require a new preflight.")
    return result


def execution_summary(result: dict) -> dict:
    """Keep artifact paths and measured facts inline; full facts stay in project.json."""
    details = result.get("result") or {}
    if result.get("status") != "succeeded" or not details.get("project_manifest"):
        return result
    if len(json.dumps(result, ensure_ascii=False)) <= 6000:
        return result
    compact = {key: details[key] for key in (
        "backend", "project_fidelity", "project_dir", "project_file", "project_manifest",
        "output_path", "preview_path", "image_sha256", "quality_status", "visual_review_status",
        "primary_map", "map_id", "pdf_path", "automatic_repair_eligible",
    ) if key in details}
    compact["layers"] = [{key: layer[key] for key in (
        "source_index", "layer_key", "name", "kind", "feature_count", "labels_enabled",
    ) if key in layer} for layer in details.get("layers", [])[:16]]
    compact["layers_omitted"] = max(0, len(details.get("layers", [])) - 16)
    layout = details.get("layout") or {}
    compact["layout"] = {key: layout[key] for key in (
        "name", "effective_parameters", "bounds_check", "adaptations",
    ) if key in layout}
    compact["layout"]["map_frames"] = [{key: frame[key] for key in (
        "id", "crs", "scale", "extent",
    ) if key in frame} for frame in layout.get("map_frames", [])[:8]]
    compact["note"] = "Full source, layout and label evidence is in project_manifest; " \
                      "geo_python can read that exact workspace file."
    return {**{key: result[key] for key in ("status", "error_summary", "run_dir") if key in result},
            "result": compact}
