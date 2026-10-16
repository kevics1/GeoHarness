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


def save_preflight(parameters: dict, report: dict, *, cwd, settings,
                   binding=None) -> tuple[str, Path]:
    packet = {"parameters": parameters, "preflight": report}
    if binding is not None:
        packet["binding"] = binding
    content = json.dumps(packet, ensure_ascii=False,
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
    if path.resolve() != path:
        raise ValueError("Prepared preflight is a link; use a direct workspace plan")
    if not path.is_file():
        raise ValueError("Prepared preflight not found; copy the exact returned plan_ref")
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Prepared preflight exceeds the 10 MiB size limit")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Prepared preflight changed; run preflight again")
    packet = json.loads(content)
    if (not isinstance(packet, dict) or not isinstance(packet.get("parameters"), dict)
            or not isinstance(packet.get("preflight"), dict)):
        raise ValueError("Prepared preflight capsule is incomplete")
    binding = packet.get("binding")
    if binding is not None and (not isinstance(binding, dict) or
            not isinstance(binding.get("map_id"), str) or
            type(binding.get("base_version")) is not int or binding["base_version"] < 0 or
            not isinstance(binding.get("spec_ref", ""), str)):
        raise ValueError("Prepared preflight revision binding is invalid")
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
    for path in (*directory.glob("*.json"), * (directory / "specifications").glob("*.json")):
        try:
            candidates.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    paths = [path for _, path in sorted(candidates, reverse=True)]
    files = []
    for path in paths[:64]:
        if re.fullmatch(r"[0-9a-f]{64}", path.stem):
            try:
                if path.parent.name == "specifications":
                    from geoharness.qgis.specification import load_spec

                    load_spec("qgis-spec:" + path.stem, cwd=cwd, settings=settings)
                else:
                    load_preflight("qgis-plan:" + path.stem, cwd=cwd, settings=settings)
                files.append(path)
            except (OSError, ValueError, TypeError):
                continue
    return tuple(files)


def summary(report: dict, reference: str, path: Path, *, max_chars=2400) -> dict:
    result = {"status": report.get("status"), "plan_ref": reference,
        "create_call": {"action": "create", "plan_ref": reference},
        "inspect_call": {"action": "inspect", "plan_ref": reference},
        "report_path": str(path)}
    details = {key: report[key] for key in (
        "status", "display_crs", "map_extent", "map_extent_source", "warnings", "issues",
        "parameter_normalizations", "analysis_binding_note",
    ) if key in report}
    details["layers"] = [{"layer_index": index, **{key: layer[key] for key in (
        "label", "kind", "feature_count", "visible_main", "visible_detail", "symbolization_field",
        "label_field", "issues", "warnings",
    ) if key in layer}} for index, layer in enumerate(report.get("layers", [])[:16])]
    details["layers_omitted"] = max(0, len(report.get("layers", [])) - 16)
    if report.get("frame_checks"):
        details["frame_checks"] = _bounded(report["frame_checks"])
    layout = report.get("layout") or {}
    effective = layout.get("effective_parameters") or {}
    details["layout"] = {"effective_parameters": {key: effective[key] for key in (
        "width_mm", "height_mm", "sidebar_width_mm", "detail_height_ratio", "footer_height_mm",
        "body_font_size_pt", "legend_font_size_pt") if key in effective},
        "bounds_check": {key: (layout.get("bounds_check") or {}).get(key)
                         for key in ("checked", "valid", "problems")},
        "adaptations": layout.get("adaptations", []),
        "label_tables": [{key: table[key] for key in ("id", "rows", "visible_rows", "fields")
                          if key in table} for table in layout.get("label_tables", [])]}
    for key, item in details.items():
        candidate = {**result, key: item}
        if len(json.dumps(candidate, ensure_ascii=False)) <= max_chars - 200:
            result[key] = item
    result["note"] = "Use create_call unchanged. Inspect the plan for full diagnostics."
    return result


def _bounded(value, *, depth=0):
    """Bound exceptional worker prose/arrays; diagnostic files retain exact values."""
    if isinstance(value, str):
        return value if len(value) <= 1200 else value[:1200] + "… [see diagnostic file]"
    if depth >= 7 and isinstance(value, (dict, list)):
        return "[see diagnostic file]"
    if isinstance(value, dict):
        values = list(value.items())
        compact = {key: _bounded(item, depth=depth + 1) for key, item in values[:24]}
        if len(values) > 24:
            compact["fields_omitted"] = len(values) - 24
        return compact
    if isinstance(value, list):
        compact = [_bounded(item, depth=depth + 1) for item in value[:12]]
        if len(value) > 12:
            compact.append({"items_omitted": len(value) - 12})
        return compact
    return value


def failure_summary(result: dict) -> dict:
    """Measured failure facts for inline display; never mutate full tool metadata."""
    compact = {key: result[key] for key in (
        "status", "error_summary", "run_dir", "manifest_path", "request_path", "result_path",
        "stdout_path", "stderr_path", "exit_code", "timeout_seconds",
    ) if key in result}
    if "error_summary" in compact:
        compact["error_summary"] = _bounded(compact["error_summary"])
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, dict):
        if "bounds_check" in diagnostics:
            # A layout includes legends, full labels and frozen plans. The failure
            # needs actual rectangles and text measurements, not all source prose.
            selected = {key: diagnostics[key] for key in (
                "code", "font", "effective_parameters", "creation_parameters", "adaptations",
            ) if key in diagnostics}
            bounds = diagnostics.get("bounds_check") or {}
            problems = bounds.get("problems") or []
            relevant = {str(problem).split(":", 1)[0].split("/", 1)[0] for problem in problems}
            selected["bounds_check"] = {key: bounds[key] for key in (
                "checked", "valid", "problems", "reason") if key in bounds}
            for key in ("content_metrics", "items"):
                values = bounds.get(key) or {}
                selected["bounds_check"][key] = {
                    name: value for name, value in values.items() if name in relevant}
            selected["map_frames"] = [{key: frame[key] for key in (
                "id", "crs", "scale", "extent") if key in frame}
                for frame in diagnostics.get("map_frames", [])]
            compact["diagnostics"] = _bounded(selected)
        else:
            compact["diagnostics"] = _bounded(diagnostics)
    elif diagnostics is not None:
        compact["diagnostics"] = _bounded(diagnostics)
    # Some runners attach the failure details as result instead of diagnostics.
    if result.get("result") is not None and not diagnostics:
        details = result["result"]
        if isinstance(details, dict):
            compact["result"] = _bounded({key: details[key] for key in (
                "error", "error_summary", "diagnostics", "issues", "warnings") if key in details})
        else:
            compact["result"] = _bounded(details)
    compact["note"] = ("Failed; inspect these measured diagnostics before changing inputs. "
                       "Full unabridged evidence remains in manifest_path/result_path "
                       "when present.")
    return compact


def execution_summary(result: dict) -> dict:
    """Keep artifact paths and measured facts inline; full facts stay in project.json."""
    details = result.get("result") or {}
    if result.get("status") != "succeeded":
        return failure_summary(result)
    if not details.get("project_manifest"):
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
        "id", "crs", "scale", "extent", "page_index",
    ) if key in frame} for frame in layout.get("map_frames", [])[:8]]
    native = details.get("native_facts") or {}
    if native.get("page_previews"):
        compact["page_previews"] = native["page_previews"]
    compact["note"] = "Full source, layout and label evidence is in project_manifest; " \
                      "geo_python can read that exact workspace file."
    return {**{key: result[key] for key in ("status", "error_summary", "run_dir") if key in result},
            "result": compact}
