"""Persistent map identities and receipts for evidence that can be checked again.

The ledger certifies file identity, data contracts, measured layout and visual
review only. It never certifies the mathematical or semantic analysis result.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import threading
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from geoharness.cartography.quality import _review_secrets
from geoharness.config.settings import GeoConfig

_LOCK = threading.RLock()
_STAGES = (
    "parameters",
    "data_checked",
    "rendered",
    "layout_checked",
    "visual_checked",
    "packaged",
    "delivered",
)
_LAYOUT_ISSUES = (
    "panel_overlaps",
    "label_overlaps",
    "clipped_labels",
    "overflow_text",
    "figure_text_overlaps",
    "clipped_figure_text",
    "text_panel_overlaps",
    "panels_outside_canvas",
    "problems",
    "issues",
)
_PRIVATE_KEY = re.compile(
    r"(?:api[_-]?key|password|credential|secret|connection_string|dsn|authorization|"
    r"cookie|private_key|access_token|refresh_token)|^(?:code|source|source_text|"
    r"script|gdf|geodataframe|raw_response)$",
    re.I,
)
_MAX_FILES = 5000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _directory(cwd: Path, config: GeoConfig) -> Path:
    root = Path(cwd).resolve()
    configured = Path(config.cartography.jobs_dir).expanduser()
    target = (configured if configured.is_absolute() else root / configured).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError("cartography.jobs_dir must be a subdirectory of the workspace")
    return target


def _path(value: str | Path, cwd: Path) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else Path(cwd) / path).resolve()


def _sha(path: Path) -> str | None:
    try:
        if not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _read(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Map ledger JSON exceeds the size limit")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Map ledger must contain a JSON object")
    return value


def _index(directory: Path) -> dict[str, Any]:
    result = _read(
        directory / "index.json",
        {"schema_version": 1, "aliases": {}, "jobs": {}, "paths": {}, "digests": {}},
    )
    for key in ("aliases", "jobs", "paths", "digests"):
        if not isinstance(result.get(key), dict):
            raise ValueError(f"Invalid map index field: {key}")
    return result


def _atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _job_path(directory: Path, job_id: str) -> Path:
    try:
        canonical = str(uuid.UUID(job_id))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Map job ID must be a server UUID") from exc
    if canonical != job_id:
        raise ValueError("Map job ID must be a canonical server UUID")
    return directory / job_id / "receipt.json"


def _job(directory: Path, job_id: str) -> dict[str, Any]:
    value = _read(_job_path(directory, job_id), {})
    if value.get("job_id") != job_id or not isinstance(value.get("versions"), list):
        raise ValueError("Map job receipt is missing or invalid")
    return value


def _sync_state(
    metadata: dict[str, Any], cwd: Path, directory: Path, index: dict[str, Any]
) -> dict[str, Any]:
    state = metadata.get("map_job_state")
    if not isinstance(state, dict):
        state = {}
        metadata["map_job_state"] = state
    if state.get("cwd") not in (None, str(Path(cwd).resolve())):
        state.clear()
    state.update(cwd=str(Path(cwd).resolve()), jobs_dir=str(directory))
    aliases = state.setdefault("aliases", {})
    aliases.clear()
    aliases.update(index["aliases"])
    return state


def begin_map_request(
    metadata: dict[str, Any], *, cwd: Path, config: GeoConfig, continuation: bool = False
) -> None:
    """Keep shared state identity; only actual input establishes a new request.

    The engine resets workflow budgets separately for every new user input,
    including a continuation. Internal retries do neither operation.
    """
    with _LOCK:
        directory = _directory(cwd, config)
        index = _index(directory)
        state = _sync_state(metadata, cwd, directory, index)
        active = state.get("active_job_id") if continuation else None
        if continuation and active not in index["jobs"]:
            active = max(
                index["jobs"], key=lambda key: index["jobs"][key]["updated_at"], default=None
            )
        state["active_job_id"] = active
        state["request_id"] = str(uuid.uuid4())
        state.pop("budget_job_id", None)


def _find(index: dict[str, Any], map_id: str, artifact: Path | None) -> str | None:
    if map_id in index["jobs"]:
        return map_id
    if artifact is not None:
        digest = _sha(artifact)
        match = index["paths"].get(str(artifact)) or index["digests"].get(digest)
        if match in index["jobs"]:
            return match
    match = index["aliases"].get(map_id)
    return match if match in index["jobs"] else None


def _register(index: dict[str, Any], job_id: str, path: Path, digest: str) -> None:
    index["paths"][str(path)] = job_id
    index["digests"][digest] = job_id


def ensure_map_job(
    metadata: dict[str, Any],
    *,
    cwd: Path,
    config: GeoConfig,
    map_id: str = "",
    artifact_path: Path | None = None,
) -> str:
    """Resolve model aliases to one server identity for the active request."""
    with _LOCK:
        directory = _directory(cwd, config)
        index = _index(directory)
        state = _sync_state(metadata, cwd, directory, index)
        artifact = _path(artifact_path, cwd) if artifact_path is not None else None
        job_id = _find(index, map_id, artifact)
        if job_id is None:
            active = state.get("active_job_id")
            job_id = active if active in index["jobs"] else None
        if job_id is None:
            job_id = str(uuid.uuid4())
            created = _now()
            index["jobs"][job_id] = {"created_at": created, "updated_at": created}
            _atomic(
                _job_path(directory, job_id),
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "created_at": created,
                    "updated_at": created,
                    "versions": [],
                    "current_version": None,
                },
            )
        if map_id:
            index["aliases"][map_id] = job_id
        if artifact is not None and (digest := _sha(artifact)):
            _register(index, job_id, artifact, digest)
        state["active_job_id"] = job_id
        _atomic(directory / "index.json", index)
        _sync_state(metadata, cwd, directory, index)
        return job_id


def canonical_budget_map(name: str, metadata: dict[str, Any], config: GeoConfig) -> str:
    """Legacy calls without a request/cwd context do not create a job ledger."""
    state = metadata.get("map_job_state")
    if not isinstance(state, dict) or not state.get("cwd"):
        return name
    job_id = ensure_map_job(metadata, cwd=Path(state["cwd"]), config=config, map_id=name)
    # One active request cannot replenish its render budget by selecting an
    # alias belonging to a different historical job. Artifact identity and
    # request budget identity have separate purposes.
    return state.setdefault("budget_job_id", job_id)


def saved_project_job(
    metadata: dict[str, Any], *, cwd: Path, config: GeoConfig, project_path: Path
) -> str | None:
    """Recover an existing workspace UUID from a user-saved QGIS project.

    A new Save As path may have different bytes from the indexed original.
    Unknown IDs are ignored; project contents do not establish ledger evidence.
    """
    from geoharness.qgis.worker_context import WorkerContext
    from geoharness.tools.geo_qgis import qgis_read_roots

    guard = WorkerContext(cwd, cwd, qgis_read_roots(config, cwd))
    path = guard.read_path(str(project_path))
    if path.suffix.lower() not in {".qgs", ".qgz"}:
        return None
    limit = 16 * 1024 * 1024
    try:
        if path.suffix.lower() == ".qgz":
            with zipfile.ZipFile(path) as archive:
                projects = [
                    item for item in archive.infolist() if item.filename.lower().endswith(".qgs")
                ]
                if len(projects) != 1 or projects[0].file_size > limit:
                    return None
                content = archive.read(projects[0])
        else:
            if path.stat().st_size > limit:
                return None
            content = path.read_bytes()
        job_id = ET.fromstring(content).findtext("properties/geoharness/map_job_id")
    except (OSError, ValueError, ET.ParseError, zipfile.BadZipFile, RuntimeError):
        return None
    with _LOCK:
        index = _index(_directory(cwd, config))
        return job_id if job_id in index["jobs"] else None


def _bounded(value: Any, config: GeoConfig) -> Any:
    secrets = _review_secrets(config)
    remaining = [8000]

    def clean(item: Any, depth: int = 0) -> Any:
        remaining[0] -= 1
        if remaining[0] < 0 or depth > 8:
            return "[truncated]"
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[redacted]")
            item = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", item)
            item = re.sub(r"(?i)(?:postgres(?:ql)?|mysql)://\S+", "[redacted]", item)
            return item[:2000]
        if isinstance(item, dict):
            return {
                str(key)[:100]: clean(child, depth + 1)
                for key, child in list(item.items())[:100]
                if not _PRIVATE_KEY.search(str(key))
            }
        if isinstance(item, (list, tuple)):
            return [clean(child, depth + 1) for child in item[:100]]
        return f"[unsupported {type(item).__name__}]"

    return clean(value)


def _report(value: dict[str, Any] | None, cwd: Path, config: GeoConfig) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = {"report": _bounded(value, config)}
    for key in ("report_path", "review_path", "manifest_path"):
        if isinstance(value.get(key), str):
            path = _path(value[key], cwd)
            if digest := _sha(path):
                result["report_reference"] = {"path": str(path), "sha256": digest}
                break
    return result


def _data_passed(report: dict[str, Any] | None) -> bool:
    if not isinstance(report, dict) or report.get("status") != "passed":
        return False
    bounds = report.get("map_extent")
    return bool(
        report.get("scope") == "map_data_preflight"
        and isinstance(report.get("layers"), list)
        and report["layers"]
        and isinstance(report.get("checks"), dict)
        and report["checks"]
        and isinstance(report.get("display_crs"), str)
        and report["display_crs"]
        and isinstance(bounds, (list, tuple))
        and len(bounds) == 4
        and all(
            isinstance(n, (int, float)) and not isinstance(n, bool) and math.isfinite(n)
            for n in bounds
        )
        and bounds[0] < bounds[2]
        and bounds[1] < bounds[3]
        and all(value != "failed" for value in report["checks"].values())
    )


def _package(
    project_dir: Path | None, cwd: Path, digest: str | None, config: GeoConfig
) -> tuple[dict[str, Any], dict[str, Any]]:
    if project_dir is None:
        return {"status": "unverified"}, {}
    root = _path(project_dir, cwd)
    allowed = [Path(cwd).resolve()]
    allowed.extend(
        _path(getattr(config, section).outputs_dir, cwd)
        for section in ("analysis", "execution", "cartography", "qgis")
    )
    if not any(root.is_relative_to(folder) for folder in allowed):
        raise ValueError("Map project must stay inside the workspace or artifact folders")
    manifest_path = next(
        (root / name for name in ("project.json", "manifest.json") if (root / name).is_file()), None
    )
    if manifest_path is None:
        return {"status": "unverified"}, {}
    manifest = _read(manifest_path, {})
    paths = {manifest_path}
    declared = manifest.get("files")
    if isinstance(declared, list):
        if len(declared) > _MAX_FILES:
            raise ValueError("Map package contains too many files")
        for item in declared:
            relative = item.get("path") if isinstance(item, dict) else item
            if not isinstance(relative, str):
                raise ValueError("Map package file references must be strings")
            path = (root / relative).resolve()
            if not path.is_relative_to(root):
                raise ValueError("Map package file reference escapes its directory")
            paths.add(path)
    else:
        for path in root.rglob("*"):
            if path.is_file():
                if not path.resolve().is_relative_to(root):
                    raise ValueError("Map package file reference escapes its directory")
                paths.add(path.resolve())
                if len(paths) > _MAX_FILES:
                    raise ValueError("Map package contains too many files")
    files = [{"path": str(path), "sha256": _sha(path)} for path in sorted(paths)]
    preview = manifest.get("preview")
    preview_path = (root / preview).resolve() if isinstance(preview, str) else None
    preview_digest = (
        _sha(preview_path) if preview_path and preview_path.is_relative_to(root) else None
    )
    matched = bool(digest and preview_digest == digest == manifest.get("preview_sha256"))
    project = manifest.get("project_file")
    project_path = (root / project).resolve() if isinstance(project, str) else root / "map.qgs"
    if not project_path.is_relative_to(root):
        raise ValueError("Map project reference escapes its directory")
    project_exists = project_path.is_file()
    status = (
        "passed" if matched and project_exists and all(f["sha256"] for f in files) else "unverified"
    )
    return {
        "status": status,
        "project_dir": str(root),
        "manifest_path": str(manifest_path),
        "image_sha256": digest,
        "files": files,
        "project_fidelity": manifest.get("project_fidelity", "unverified"),
        "fidelity_note": ("QGIS reference layout differs from the supplied image; "
                          "package check verifies file integrity only."
                          if manifest.get("project_fidelity") == "reference" else ""),
    }, manifest


def _layout_status(
    layout: dict[str, Any] | None, digest: str | None, manifest: dict[str, Any]
) -> str:
    if not isinstance(layout, dict) or not digest:
        return "unverified"
    bound = layout.get("image_sha256") == digest
    if "bounds_check" in layout:
        bound = bound or (
            manifest.get("preview_sha256") == digest and manifest.get("layout") == layout
        )
        check = layout.get("bounds_check")
        if not bound or not isinstance(check, dict) or check.get("checked") is not True:
            return "unverified"
        if check.get("valid") is False or check.get("problems"):
            return "issues"
        return (
            "passed" if check.get("valid") is True and check.get("problems") == [] else "unverified"
        )
    if not bound or layout.get("scope") != "layout_geometry":
        return "unverified"
    if any(layout.get(key) for key in _LAYOUT_ISSUES):
        return "issues"
    # The native report must include the measured issue collections.
    measured = all(isinstance(layout.get(key), list) for key in _LAYOUT_ISSUES[:8])
    return "passed" if layout.get("status") == "passed" and measured else "unverified"


def _save(directory: Path, job: dict[str, Any], index: dict[str, Any]) -> None:
    job["updated_at"] = _now()
    index["jobs"][job["job_id"]]["updated_at"] = job["updated_at"]
    _atomic(_job_path(directory, job["job_id"]), job)
    _atomic(directory / "index.json", index)


def _package_signature(stage: dict[str, Any]) -> list[tuple[str, str | None]]:
    root = Path(stage["project_dir"]) if stage.get("project_dir") else None
    return sorted(
        (Path(item["path"]).relative_to(root).as_posix(), item.get("sha256"))
        for item in stage.get("files", [])
        if root is not None
    )


def record_map_version(
    job_id: str,
    metadata: dict[str, Any],
    *,
    cwd: Path,
    config: GeoConfig,
    image_path: Path,
    preflight: dict[str, Any] | None,
    layout: dict[str, Any] | None,
    project_dir: Path | None,
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _LOCK:
        directory = _directory(cwd, config)
        job = _job(directory, job_id)
        index = _index(directory)
        image = _path(image_path, cwd)
        digest = _sha(image)
        package, manifest = _package(project_dir, cwd, digest, config)
        previous = job["versions"][-1] if job["versions"] else None
        same = previous is not None and digest and previous.get("image_sha256") == digest
        if same:
            version = previous
        else:
            version = {
                "version": len(job["versions"]) + 1,
                "created_at": _now(),
                "stages": {key: {"status": "unverified"} for key in _STAGES},
            }
            job["versions"].append(version)
        stages = version["stages"]
        old_package = stages["packaged"]
        package_changed = (
            same
            and project_dir is not None
            and old_package.get("files")
            and _package_signature(old_package) != _package_signature(package)
        )
        version.update(image_path=str(image), image_sha256=digest)
        stages["rendered"] = {
            "status": "passed" if digest else "unverified",
            "path": str(image),
            "image_sha256": digest,
        }
        if preflight is not None:
            stages["parameters"] = {"status": "recorded", **_report(preflight, cwd, config)}
            stages["data_checked"] = {
                "status": "passed" if _data_passed(preflight) else "unverified",
                "image_sha256": digest,
                **_report(preflight, cwd, config),
            }
        elif package_changed:
            stages["data_checked"]["status"] = "stale"
        if layout is not None:
            stages["layout_checked"] = {
                "status": "unverified"
                if package_changed
                else _layout_status(layout, digest, manifest),
                "image_sha256": digest,
                **_report(layout, cwd, config),
            }
        elif package_changed:
            stages["layout_checked"] = {"status": "unverified", "image_sha256": digest}
        if project_dir is not None or not same:
            stages["packaged"] = package
        if analysis is not None:
            version["analysis"] = {"status": "recorded", **_report(analysis, cwd, config)}
        elif package_changed and version.get("analysis"):
            version["analysis"]["status"] = "stale"
        job["current_version"] = version["version"]
        if digest:
            _register(index, job_id, image, digest)
        if project_dir is not None:
            root = _path(project_dir, cwd)
            for name in (manifest.get("project_file", "map.qgs"), manifest.get("preview")):
                if isinstance(name, str):
                    path = (root / name).resolve()
                    if path.is_relative_to(root) and (file_digest := _sha(path)):
                        _register(index, job_id, path, file_digest)
        _save(directory, job, index)
        _sync_state(metadata, cwd, directory, index)
        return _receipt(job, index, artifact=image)


def _matching_version(job: dict[str, Any], image: Path) -> dict[str, Any] | None:
    digest = _sha(image)
    return next(
        (
            version
            for version in reversed(job["versions"])
            if digest and version.get("image_sha256") == digest
        ),
        None,
    )


def record_map_review(
    job_id: str,
    metadata: dict[str, Any],
    *,
    cwd: Path,
    config: GeoConfig,
    image_path: Path,
    review: dict[str, Any],
    layout_status: str | None = None,
    measured_layout: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with _LOCK:
        directory = _directory(cwd, config)
        job = _job(directory, job_id)
        index = _index(directory)
        image = _path(image_path, cwd)
        version = _matching_version(job, image)
        if version is None:
            return _receipt(job, index, artifact=image)
        digest = version["image_sha256"]
        bound = review.get("image_sha256") == digest
        status = review.get("visual_review_status", review.get("status", "unverified"))
        diagnostics = review.get("diagnostics", {})
        incomplete = (
            not bound
            or not isinstance(diagnostics, dict)
            or diagnostics.get("failure_stage")
            or diagnostics.get("finish_reason") in {"length", "content_filter"}
        )
        issues = review.get("issues", [])
        issue = isinstance(issues, list) and any(
            isinstance(item, dict) and item.get("severity") in {"warning", "error"}
            for item in issues
        )
        patch = review.get("layout_patch", {})
        patch = isinstance(patch, dict) and any(value is not None for value in patch.values())
        if incomplete or status not in {"passed", "issues"}:
            status = "unverified"
        elif issue or patch or status == "issues":
            status = "issues"
        version["stages"]["visual_checked"] = {
            "status": status,
            "image_sha256": digest,
            **_report(review, cwd, config),
        }
        if measured_layout is not None:
            version["stages"]["layout_checked"] = {
                "status": _layout_status(measured_layout, digest, {}),
                "image_sha256": digest,
                **_report(measured_layout, cwd, config),
            }
        # An asserted layout_status cannot turn an absent measured check into passed.
        if layout_status in {"issues", "unverified"}:
            version["stages"]["layout_checked"]["status"] = layout_status
        _save(directory, job, index)
        _sync_state(metadata, cwd, directory, index)
        return _receipt(job, index, artifact=image)


def record_map_delivery(
    job_id: str,
    metadata: dict[str, Any],
    *,
    cwd: Path,
    config: GeoConfig,
    image_path: Path,
    delivery: dict[str, Any],
) -> dict[str, Any]:
    with _LOCK:
        directory = _directory(cwd, config)
        job = _job(directory, job_id)
        index = _index(directory)
        image = _path(image_path, cwd)
        version = _matching_version(job, image)
        if version is None:
            return _receipt(job, index, artifact=image)
        root = _path(delivery.get("delivery_dir", cwd), cwd)
        declared_files = delivery.get("files", [])
        if not isinstance(declared_files, list) or len(declared_files) > _MAX_FILES:
            raise ValueError("Delivery files must be a bounded list")
        candidates = [item for item in declared_files if isinstance(item, str)]
        candidates.extend(
            delivery[key]
            for key in ("primary_map", "manifest_path")
            if isinstance(delivery.get(key), str)
        )
        files = []
        for item in dict.fromkeys(candidates):
            path = _path(item, root)
            if not path.is_relative_to(Path(cwd).resolve()):
                raise ValueError("Delivered map files must stay inside the workspace")
            if digest := _sha(path):
                files.append({"path": str(path), "sha256": digest})
                _register(index, job_id, path, digest)
        primary = delivery.get("primary_map")
        primary_digest = _sha(_path(primary, root)) if isinstance(primary, str) else None
        matched = bool(primary_digest and primary_digest == version["image_sha256"])
        copied = matched and files and len(files) == len(dict.fromkeys(candidates))
        version["stages"]["delivered"] = {
            "status": "stale"
            if delivery.get("snapshot_status") == "changed"
            else ("passed" if copied else "unverified"),
            "image_sha256": version["image_sha256"],
            "files": files,
            **_report(delivery, cwd, config),
        }
        _save(directory, job, index)
        _sync_state(metadata, cwd, directory, index)
        return _receipt(job, index, artifact=image)


def _stale_files(stage: dict[str, Any]) -> bool:
    return any(
        not item.get("sha256") or _sha(Path(item["path"])) != item["sha256"]
        for item in stage.get("files", [])
    )


def _receipt(
    job: dict[str, Any], index: dict[str, Any], *, artifact: Path | None = None
) -> dict[str, Any]:
    result = copy.deepcopy(job)
    aliases = [alias for alias, job_id in index["aliases"].items() if job_id == job["job_id"]]
    result.update(
        map_id=job["job_id"],
        aliases=aliases,
        quality_scope=["map_data_contract", "layout_geometry", "visual_image"],
        analysis_correctness="unverified",
    )
    result["latest_version"] = job["current_version"]
    version = result["versions"][-1] if result["versions"] else None
    if artifact is not None:
        actual_digest = _sha(artifact)
        selected = next(
            (
                item
                for item in reversed(result["versions"])
                if actual_digest and item.get("image_sha256") == actual_digest
            ),
            None,
        )
        if selected is None:
            selected = next(
                (
                    item
                    for item in reversed(result["versions"])
                    if item.get("image_path") == str(artifact)
                ),
                None,
            )
        if selected is not None:
            version = selected
    if version is None:
        stages = {key: {"status": "unverified"} for key in _STAGES}
    else:
        stages = version["stages"]
        selected_path = artifact if artifact is not None else Path(version["image_path"])
        image_changed = (
            _sha(selected_path) != version["image_sha256"] or not version["image_sha256"]
        )
        if image_changed:
            for key in _STAGES:
                if stages[key]["status"] in {"passed", "recorded", "issues"}:
                    stages[key]["status"] = "stale"
        package_changed = _stale_files(stages["packaged"])
        if package_changed:
            stages["packaged"]["status"] = "stale"
            if stages["layout_checked"]["status"] in {"passed", "issues"}:
                stages["layout_checked"]["status"] = "stale"
            if stages["data_checked"]["status"] in {"passed", "issues"}:
                stages["data_checked"]["status"] = "stale"
            if version.get("analysis"):
                version["analysis"]["status"] = "stale"
        if _stale_files(stages["delivered"]):
            stages["delivered"]["status"] = "stale"
        for stage in stages.values():
            reference = stage.get("report_reference")
            if isinstance(reference, dict) and _sha(Path(reference["path"])) != reference.get(
                "sha256"
            ):
                stage["status"] = "stale"
        result.update(
            image_path=str(selected_path),
            image_sha256=version["image_sha256"],
            current_version=version["version"],
            selected_version=version["version"],
        )
        if stages["packaged"].get("project_dir"):
            result["project_dir"] = stages["packaged"]["project_dir"]
    result["stages"] = stages
    result["quality_status"] = (
        "passed"
        if all(
            stages[key]["status"] == "passed"
            for key in ("data_checked", "rendered", "layout_checked", "visual_checked")
        )
        else "draft"
    )
    result["status"] = (
        "stale"
        if any(item["status"] == "stale" for item in stages.values())
        else result["quality_status"]
    )
    return result


def map_job_receipt(
    metadata: dict[str, Any],
    *,
    cwd: Path,
    config: GeoConfig,
    map_id: str = "",
    artifact_path: Path | None = None,
) -> dict[str, Any]:
    """Read existing evidence and rehash files; never create a job or metadata."""
    with _LOCK:
        directory = _directory(cwd, config)
        index = _index(directory)
        artifact = _path(artifact_path, cwd) if artifact_path is not None else None
        job_id = _find(index, map_id, artifact)
        if job_id is None and not map_id and artifact_path is None:
            state = metadata.get("map_job_state", {})
            active = state.get("active_job_id") if isinstance(state, dict) else None
            job_id = active if active in index["jobs"] else None
        if job_id is None:
            return {
                "job_id": None,
                "map_id": None,
                "status": "draft",
                "quality_status": "draft",
                "current_version": None,
                "versions": [],
                "stages": {key: {"status": "unverified"} for key in _STAGES},
                "quality_scope": ["map_data_contract", "layout_geometry", "visual_image"],
                "analysis_correctness": "unverified",
            }
        return _receipt(_job(directory, job_id), index, artifact=artifact)
