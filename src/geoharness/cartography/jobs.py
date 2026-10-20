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
    from geoharness.cartography import ledger_store

    path = _job_path(directory, job_id)
    value = ledger_store.load(path) if ledger_store.is_current(path) else _read(path, {})
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
    preview_only = manifest.get("project_fidelity") in {
        "preview_only", "native_scene_preview"}
    # Native previews and legacy QGIS previews are complete without a .qgz or
    # .qgs. The portable project is deliberately deferred to the first delivery,
    # where every source/layout/page identity is checked again.
    project_exists = False
    if not preview_only:
        project_path = (root / project).resolve() if isinstance(project, str) else (
            root / "map.qgs")
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
        "preview_integrity": (
            "passed" if matched and all(f['sha256'] for f in files) else "unverified"),
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
    from geoharness.cartography import ledger_store

    path = _job_path(directory, job["job_id"])
    if path.exists() and not ledger_store.is_current(path):
        ledger_store.migrate(path)
    ledger_store.write(path, job)
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
        if (same and previous.get("image_path") != str(image) and project_dir is not None
                and previous["stages"]["packaged"].get("files")
                and _package_signature(previous["stages"]["packaged"])
                != _package_signature(package)):
            # Equal pixels do not prove equal data/project evidence. A new export
            # package must not overwrite the verified historical candidate.
            same = False
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
            # Preview-only manifests intentionally have no project_file.
            for name in (manifest.get("project_file"), manifest.get("preview")):
                if isinstance(name, str) and name:
                    path = (root / name).resolve()
                    if path.is_relative_to(root) and (file_digest := _sha(path)):
                        _register(index, job_id, path, file_digest)
        if previous is not None and not same:
            old_pages, new_pages = _map_page_files(previous), _map_page_files(version)
            for n, page in new_pages.items():
                old = next(((i, p) for i, p in old_pages.items()
                    if page.get('page_id') and page.get('content_key')
                    and p.get('page_id') == page['page_id']
                    and p.get('content_key') == page['content_key']
                    and p['sha256'] == page['sha256']), None)
                if old:
                    review = previous.get('page_reviews', {}).get(str(old[0]), {})
                    reference = review.get('report_reference')
                    if (review.get('status') in {'passed', 'issues'}
                            and review.get('image_sha256') == page['sha256']
                            and _sha(Path(old[1]['path'])) == page['sha256']
                            and (not isinstance(reference, dict) or _sha(Path(
                                reference['path'])) == reference.get('sha256'))):
                        version.setdefault('page_reviews', {})[str(n)] = {
                            **copy.deepcopy(review), 'inherited_from_version': previous['version']}
            _aggregate_page_reviews(version, new_pages)
        _save(directory, job, index)
        _sync_state(metadata, cwd, directory, index)
        return _receipt(job, index, artifact=image)


def _version_image_paths(version: dict[str, Any]) -> set[Path]:
    """Keep image identity separate from equal pixels in different saved projects."""
    stages = version.get("stages")
    stages = stages if isinstance(stages, dict) else {}
    rendered = stages.get("rendered")
    paths = [version.get("image_path"),
             rendered.get("path") if isinstance(rendered, dict) else None]
    for stage_name in ("packaged", "delivered"):
        stage = stages.get(stage_name)
        files = stage.get("files") if isinstance(stage, dict) else None
        if isinstance(files, list):
            paths.extend(item["path"] for item in files if isinstance(item, dict)
                         and isinstance(item.get("path"), str)
                         and item.get("sha256") == version.get("image_sha256")
                         and Path(item["path"]).suffix.lower()
                         in {".png", ".jpg", ".jpeg", ".webp"})
    return {Path(path).resolve() for path in paths if isinstance(path, str) and path}


def _matching_version(
    job: dict[str, Any], image: Path, *, selected_version: int | None = None,
    allow_stale_path: bool = False,
) -> dict[str, Any] | None:
    digest = _sha(image)
    versions = job.get("versions")
    versions = [v for v in versions if isinstance(v, dict)] if isinstance(versions, list) else []
    if selected_version is not None:
        return next((v for v in versions if type(selected_version) is int
                     and v.get("version") == selected_version
                     and digest and v.get("image_sha256") == digest), None)
    exact = [v for v in versions if image.resolve() in _version_image_paths(v)]
    bound = [v for v in exact if digest and v.get("image_sha256") == digest]
    if bound:
        return bound[-1]
    if exact and allow_stale_path:
        return exact[-1]
    matches = [v for v in versions if digest and v.get("image_sha256") == digest]
    # A copied image alone cannot distinguish equal previews with different QGS files.
    return matches[0] if len(matches) == 1 else None


def record_map_selection(
    job_id: str, metadata: dict[str, Any], *, cwd: Path, config: GeoConfig,
    image_path: Path, project_dir: Path | None = None, selected_version: int | None = None,
) -> dict[str, Any]:
    """Relocate identical selected artifacts without manufacturing a render version."""
    with _LOCK:
        directory = _directory(cwd, config)
        job, index = _job(directory, job_id), _index(directory)
        image = _path(image_path, cwd)
        version = _matching_version(job, image, selected_version=selected_version)
        if version is None:
            raise ValueError("Selected image is not an existing map version")
        previous = version["stages"]["packaged"]
        preview_valid = (previous.get('project_fidelity') == 'preview_only'
                         and previous.get('preview_integrity') == 'passed')
        if (previous.get("status") != "passed" and not preview_valid) or _stale_files(previous):
            raise ValueError("Original selected package changed")
        if project_dir is not None:
            package, manifest = _package(project_dir, cwd, version["image_sha256"], config)

            def signature(stage: dict) -> list:
                root = Path(stage["project_dir"]).resolve()
                return sorted((Path(f["path"]).resolve().relative_to(root).as_posix(), f["sha256"])
                              for f in stage.get("files", []))

            valid_copy = (package.get('status') == 'passed' or (
                package.get('project_fidelity') == 'preview_only'
                and package.get('preview_integrity') == 'passed'))
            if not valid_copy or signature(package) != signature(previous):
                raise ValueError("Selected copy must preserve every package file SHA256")
            version["stages"]["packaged"] = package
            root = _path(project_dir, cwd)
            for name in (manifest.get("project_file"), manifest.get("preview")):
                if isinstance(name, str):
                    path = (root / name).resolve()
                    if path.is_relative_to(root) and (digest := _sha(path)):
                        _register(index, job_id, path, digest)
        version["image_path"] = str(image)
        _register(index, job_id, image, version["image_sha256"])
        _save(directory, job, index)
        _sync_state(metadata, cwd, directory, index)
        return _receipt(job, index, artifact=image, selected_version=version["version"])


def record_map_materialization(
    job_id, metadata, *, cwd, config, image_path, project_dir, selected_version,
):
    """Attach the first editable package to exactly the approved preview version."""
    with _LOCK:
        directory = _directory(cwd, config)
        job, index = _job(directory, job_id), _index(directory)
        image = _path(image_path, cwd)
        version = _matching_version(job, image, selected_version=selected_version)
        if version is None or _sha(image) != version.get('image_sha256'):
            raise ValueError('交付预览版本或图片已变化；重新确认实际版本')
        previous = version['stages']['packaged']
        if _stale_files(previous):
            raise ValueError('冻结预览文件已变化；不能沿用原审查记录生成工程')
        preview_fidelities = {'preview_only', 'native_scene_preview'}
        if previous.get('project_fidelity') not in preview_fidelities:
            raise ValueError('只有尚未物化的预览可以绑定首次工程')
        original = _read(Path(previous['manifest_path']), {})
        package, manifest = _package(project_dir, cwd, version['image_sha256'], config)
        if package['status'] != 'passed':
            raise ValueError('最终工程预览与冻结图片不一致；保留原预览和工程供定位')
        if original.get('project_fidelity') == 'native_scene_preview':
            validation = manifest.get('project_validation') or {}
            if (manifest.get('project_fidelity') != 'scene_bound'
                    or validation.get('status') != 'passed'
                    or validation.get('scene_plan_sha256') != original.get('scene_plan_sha256')
                    or original.get('scene_ref') != manifest.get('scene_ref')):
                raise ValueError('最终工程缺少同一场景计划的成功编译验证')
            # Native scenes freeze a renderer plan, source inventory and page
            # hashes, not a QGIS creation spec. The final project must carry the
            # same scene plan and the same source inventory.
            if original.get('scene_plan_sha256') != manifest.get('scene_plan_sha256'):
                raise ValueError('最终工程与已审原生场景计划不一致；重新预览与审查')
            if original.get('source_inventory') != manifest.get('source_inventory'):
                raise ValueError('冻结预览来源文件在工程生成期间发生变化；不能沿用原审查')
            if not original.get('scene_plan_sha256') or not original.get('source_inventory'):
                raise ValueError('冻结原生预览缺少来源或场景计划，不能沿用原审查')
        elif original.get('export_binding'):
            if original['export_binding'] != manifest.get('export_binding'):
                raise ValueError('冻结预览的原工程或数据已变化；重新预览与审查')
        else:
            inventory = (original.get('creation_spec') or {}).get('source_inventory')
            if inventory is not None and inventory != (
                    manifest.get('creation_spec') or {}).get('source_inventory'):
                raise ValueError('冻结预览来源文件在工程生成期间发生变化；不能沿用原审查')
            for key in ('source_identities', 'input_digest'):
                before = (original.get('layout_plan') or {}).get(key)
                after = (manifest.get('layout_plan') or {}).get(key)
                if before is None or before != after:
                    raise ValueError('冻结预览的来源或布局契约已变化；重新预览与审查')
        old_pages = _map_page_files(version)
        candidate = {**version, 'stages': {**version['stages'], 'packaged': package}}
        new_pages = _map_page_files(candidate)
        if (set(old_pages) != set(new_pages) or any(
                old_pages[n]['sha256'] != new_pages[n]['sha256'] for n in old_pages)):
            raise ValueError('最终工程逐页像素与冻结预览不一致；不能复用原审查结论')
        version['preview_package'] = copy.deepcopy(previous)
        version['stages']['packaged'] = package
        version['materialization'] = {
            'status': 'passed', 'selected_version': selected_version,
            'spec_ref': (original.get('creation_spec') or {}).get('spec_ref', ''),
            'preview_manifest': previous['manifest_path'],
            'preview_manifest_sha256': _sha(Path(previous['manifest_path'])),
            'scope': 'same_sources_layout_and_all_page_images',
        }
        root = _path(project_dir, cwd)
        for name in (manifest['project_file'], manifest['preview']):
            path = (root / name).resolve()
            _register(index, job_id, path, _sha(path))
        _aggregate_page_reviews(version, new_pages)
        _save(directory, job, index)
        _sync_state(metadata, cwd, directory, index)
        return _receipt(job, index, artifact=image, selected_version=selected_version)


def _map_page_files(version: dict, package=None) -> dict[int, dict]:
    """Read page membership only from the hash-recorded native project manifest."""
    package = package if package is not None else version.get("stages", {}).get("packaged", {})
    manifest_path = package.get("manifest_path")
    if not manifest_path:
        return {}
    files = {entry.get("path"): entry.get("sha256") for entry in package.get("files", [])}
    manifest_hash = files.get(manifest_path)
    if not manifest_hash or _sha(Path(manifest_path)) != manifest_hash:
        return {}
    manifest = _read(Path(manifest_path), {})
    if manifest.get("backend") not in {"qgis", "matplotlib"}:
        return {}
    from geoharness.qgis.artifacts import returned_page_previews

    entries = returned_page_previews(manifest)
    root = Path(manifest_path).parent.resolve()
    pages = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        index, locator = entry.get("page_index"), entry.get("path")
        frames = entry.get('frames', [])
        if type(index) is not int or index < 0 or not isinstance(frames, list) or not locator:
            continue
        path = Path(locator)
        path = (path if path.is_absolute() else root / path).resolve()
        if not path.is_relative_to(root) or str(path) not in files or index in pages:
            raise ValueError("Native page preview is not a unique recorded package member")
        pages[index] = {"path": str(path), "sha256": files[str(path)],
                        "manifest_sha256": manifest_hash, "frames": frames,
                        "page_id": entry.get('page_id'), "content_key": entry.get('content_key'),
                        "role": entry.get('role', 'map' if frames else 'unknown')}
    return pages


def _aggregate_page_reviews(version: dict, pages: dict[int, dict]) -> None:
    if len(pages) <= 1:
        return
    recorded = version.get("page_reviews", {})
    summaries = []
    for index, page in sorted(pages.items()):
        review = recorded.get(str(index), {})
        status = review.get("status", "unverified")
        if review.get("image_sha256") != page["sha256"]:
            status = "unverified"
        reference = review.get("report_reference")
        if (_sha(Path(page["path"])) != page["sha256"] or
                isinstance(reference, dict) and _sha(Path(reference["path"])) !=
                reference.get("sha256")):
            status = "stale"
        summaries.append({"page_index": index, "status": status, **page})
    statuses = {page["status"] for page in summaries}
    status = ("stale" if "stale" in statuses else "issues" if "issues" in statuses
              else "passed" if statuses == {"passed"} else "unverified")
    primary = recorded.get("0", {})
    version["stages"]["visual_checked"] = {**primary, "status": status,
        "image_sha256": version["image_sha256"], "page_reviews": summaries,
        "scope": "all_native_map_pages"}


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
        raw_diagnostics = review.get("diagnostics", {})
        diagnostics = raw_diagnostics if isinstance(raw_diagnostics, dict) else {}
        binding = diagnostics.get("review_binding") or {}
        binding = binding if isinstance(binding, dict) else {}
        page_index = binding.get("page_index", 0)
        if type(page_index) is not int or page_index < 0:
            raise ValueError("Page review requires an actual nonnegative page index")
        if version is None and type(page_index) is int and page_index > 0:
            version = next((item for item in job.get("versions", [])
                if item.get("version") == binding.get("version") and
                item.get("image_sha256") == binding.get("primary_image_sha256")), None)
            pages = _map_page_files(version) if version else {}
            page = pages.get(page_index)
            if version and (not page or page['path'] != str(image)):
                page = _map_page_files(version, version.get('preview_package', {})).get(page_index)
            if not page or page["path"] != str(image) or page["sha256"] != _sha(image) or \
                    page["manifest_sha256"] != binding.get("manifest_sha256"):
                version = None
        if version is None:
            return _receipt(job, index, artifact=image)
        digest = version["image_sha256"]
        pages = _map_page_files(version)
        if page_index != 0:
            page = pages.get(page_index)
            if not page or page['path'] != str(image):
                alias = _map_page_files(version, version.get('preview_package', {})).get(page_index)
                if alias and page and alias['sha256'] == page['sha256']:
                    page = alias
            if (not page or binding.get("version") != version.get("version") or
                    binding.get("primary_image_sha256") != version["image_sha256"] or
                    binding.get("manifest_sha256") != page["manifest_sha256"] or
                    str(image) != page["path"]):
                raise ValueError("Page review does not bind this recorded map version")
        page_digest = pages.get(page_index, {}).get("sha256", digest)
        bound = review.get("image_sha256") == page_digest == _sha(image)
        status = review.get("visual_review_status", review.get("status", "unverified"))
        if len(pages) > 1 and diagnostics.get("page_visual_status") in {"passed", "issues"}:
            status = diagnostics["page_visual_status"]
        incomplete = (
            not bound
            or not isinstance(raw_diagnostics, dict)
            or diagnostics.get("failure_stage")
            or diagnostics.get("finish_reason") in {"length", "content_filter"}
        )
        issues = review.get("issues", [])
        issue = isinstance(issues, list) and any(
            isinstance(item, dict) and item.get("severity") == "error"
            for item in issues
        )
        patch = review.get("layout_patch", {})
        patch = isinstance(patch, dict) and any(value is not None for value in patch.values())
        if incomplete or status not in {"passed", "issues"}:
            status = "unverified"
        elif issue or patch or status == "issues":
            status = "issues"
        stage = {
            "status": status,
            "image_sha256": page_digest,
            **_report(review, cwd, config),
        }
        if pages:
            version.setdefault("page_reviews", {})[str(page_index)] = stage
        if page_index == 0:
            version["stages"]["visual_checked"] = stage
        _aggregate_page_reviews(version, pages)
        if measured_layout is not None and page_index == 0:
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
        return _receipt(job, index, artifact=Path(version["image_path"]))


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
    job: dict[str, Any], index: dict[str, Any], *, artifact: Path | None = None,
    include_candidate: bool = True, selected_version: int | None = None,
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
    result["artifact_registered"] = artifact is None
    if artifact is not None:
        actual_digest = _sha(artifact)
        selected = _matching_version(result, artifact, selected_version=selected_version,
                                     allow_stale_path=True)
        version = selected
        if selected is not None:
            version = selected
            # A path still identifies stale history after an in-place user edit,
            # but those new bytes have not been registered for automatic replacement.
            result["artifact_registered"] = bool(
                actual_digest and actual_digest == selected.get("image_sha256")
            )
    if version is None:
        stages = {key: {"status": "unverified"} for key in _STAGES}
    else:
        _aggregate_page_reviews(version, _map_page_files(version))
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
    if include_candidate:
        result["delivery_candidate"] = _delivery_candidate(job, index)
    return result


def _layer_key(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() and len(value) <= 128 else None


def map_mutation_state(metadata, *, cwd, config, map_id) -> dict:
    """Read current revision and editable head without rehashing historical packages."""
    with _LOCK:
        from geoharness.cartography import ledger_store

        directory = _directory(cwd, config)
        path = _job_path(directory, map_id)
        job = ledger_store.head(path) if ledger_store.is_current(path) else _job(directory, map_id)
        return {"map_id": map_id, "latest_version": job.get("current_version") or 0,
                "latest_spec_ref": (job.get("editable_draft") or {}).get("spec_ref", ""),
                "mutation": copy.deepcopy(job.get("mutation") or {})}


def page_review_requirements(metadata, *, cwd, config, map_id, updates=None):
    """Persist acceptance requirements and invalidate only pages they change."""
    with _LOCK:
        directory = _directory(cwd, config)
        job, index = _job(directory, map_id), _index(directory)
        current = copy.deepcopy(job.get('page_review_requirements', {}))
        updates = updates or {}
        changed = {key for key, value in updates.items()
                   if current.get(key, []) != value}
        current.update(updates)
        if changed:
            job['page_review_requirements'] = current
            for version in job.get('versions', []):
                pages = _map_page_files(version)
                for n, page in pages.items():
                    if (page.get('page_id') or str(n)) in changed:
                        stage = version.get('page_reviews', {}).get(str(n))
                        if stage:
                            stage['status'] = 'stale'
                            stage.pop('page_contract', None)
                        if n == 0:
                            version['stages'].setdefault('visual_checked', {})['status'] = 'stale'
                _aggregate_page_reviews(version, pages)
            _save(directory, job, index)
            _sync_state(metadata, cwd, directory, index)
        return current


def record_map_draft(map_id, metadata, *, cwd, config, spec_ref) -> None:
    if not re.fullmatch(r"qgis-spec:[0-9a-f]{64}", spec_ref):
        raise ValueError("Editable draft requires an actual qgis-spec reference")
    with _LOCK:
        directory = _directory(cwd, config)
        from geoharness.cartography import ledger_store

        path = _job_path(directory, map_id)
        if ledger_store.is_current(path):
            ledger_store.update_head(path, {"editable_draft": {
                "spec_ref": spec_ref, "updated_at": _now()}})
            return
        job, index = _job(directory, map_id), _index(directory)
        job["editable_draft"] = {"spec_ref": spec_ref, "updated_at": _now()}
        _save(directory, job, index)


def record_map_mutation(map_id, metadata, *, cwd, config, value) -> None:
    with _LOCK:
        directory = _directory(cwd, config)
        from geoharness.cartography import ledger_store

        path = _job_path(directory, map_id)
        if ledger_store.is_current(path):
            head = ledger_store.head(path)
            ledger_store.update_head(path, {"mutation": {**(head.get("mutation") or {}), **value}})
            return
        job, index = _job(directory, map_id), _index(directory)
        job["mutation"] = {**(job.get("mutation") or {}), **value}
        _save(directory, job, index)


def inspect_map_ledger(*, cwd, config, map_id, recover=False, offset=0, limit=8):
    """Diagnose a registered ledger without hydrating or trusting all its history."""
    from geoharness.cartography import ledger_store

    with _LOCK:
        directory = _directory(cwd, config)
        index = _index(directory)
        job_id = _find(index, map_id, None)
        if job_id is None:
            raise ValueError("Unknown map_id; use an actual returned map identity")
        if recover:
            from geoharness.cartography.mutation_control import mutation_active

            if mutation_active(cwd, job_id):
                raise ValueError('Map mutation is active; inspect health now and recover '
                                 'after it ends')
        path = _job_path(directory, job_id)
        value = {"map_id": job_id, "receipt_bytes": path.stat().st_size,
                 "status": "legacy", "requires_recovery": True,
                 "recover_call": {"action": "recover", "map_id": job_id}}
        if recover:
            value["migration"] = ledger_store.migrate(path)
        if ledger_store.is_current(path):
            head = ledger_store.head(path)
            value.update(status="current", requires_recovery=False,
                         latest_version=head.get("current_version"),
                         latest_spec_ref=(head.get("editable_draft") or {}).get("spec_ref", ""),
                         version_count=head["version_count"], mutation=head.get("mutation"),
                         history=ledger_store.history(path, offset, limit))
            if offset + limit < head["version_count"]:
                value["next_call"] = {"action": "inspect", "map_id": job_id,
                                      "offset": offset + limit, "limit": limit}
        return value


def _layer_metadata(report: dict, name: str, source_index: int) -> dict:
    """Array positions may bind evidence inside one version, never between versions."""
    values = report.get(name)
    if not isinstance(values, list) or not 0 <= source_index < len(values):
        return {}
    value = values[source_index]
    return value if isinstance(value, dict) else {}


def _retention_layer_key(layer: dict, report: dict) -> str | None:
    if key := _layer_key(layer.get("layer_key")):
        return key
    source_index = layer.get("source_index")
    if type(source_index) is not int or source_index < 0:
        return None
    binding = _layer_metadata(report, "source_bindings", source_index)
    metadata = _layer_metadata(report, "layers", source_index)
    source = _layer_metadata(report, "layout_sources", source_index)
    if key := _layer_key(binding.get("layer_key")):
        return key
    # A label, packaged data path, or feature count alone cannot establish identity.
    path, label = binding.get("file_path"), binding.get("label")
    kind = metadata.get("kind") or source.get("kind")
    if (not isinstance(path, str) or not Path(path).is_absolute()
            or "[redacted]" in path or not isinstance(label, str) or not label
            or kind not in {"vector", "raster"}
            or metadata.get("label", label) != label
            or source.get("kind", kind) != kind):
        return None
    if "source_layer" in binding:
        selector = binding["source_layer"]
    elif "source_layer" in source:
        selector = source["source_layer"]
    elif kind == "raster" or Path(path).suffix.lower() in {".shp", ".geojson", ".json"}:
        selector = ""
    else:
        # Legacy GeoPackages may hold several layers; the chosen one must be known.
        return None
    if type(selector) not in {str, int}:
        return None
    from geoharness.qgis.layer_identity import stable_layer_key

    return stable_layer_key(kind, label, path, selector)


def _retention_evidence(report: dict) -> tuple[dict[str, int], set[str], bool, bool]:
    frame_checks = report.get("frame_checks")
    checks = frame_checks.get("layers") if isinstance(frame_checks, dict) else None
    if not isinstance(checks, list) or not checks:
        return {}, set(), True, False
    rows = []
    seen: set[str] = set()
    duplicated: set[str] = set()
    for layer in checks:
        if not isinstance(layer, dict):
            rows.append((None, None, True))
            continue
        key = _retention_layer_key(layer, report)
        if key in seen:
            duplicated.add(key)
        if key is not None:
            seen.add(key)
        count = layer.get("visible_feature_count")
        count = count if type(count) is int and count >= 0 else None
        rows.append((key, count, bool(layer.get("required"))))
    visible, required = {}, set()
    unknown_required, complete = False, True
    for key, count, is_required in rows:
        if key is None or count is None or key in duplicated:
            unknown_required |= is_required
            complete = False
            continue
        visible[key] = count
        if is_required:
            required.add(key)
    return visible, required, unknown_required, complete


def _delivery_candidate(job: dict[str, Any], index: dict[str, Any]) -> dict | None:
    """Compare stable retained identities; unknown coverage is never a verified full score."""
    candidates = []
    maximum_visible: dict[str, int] = {}
    evidence = {}
    unknown_requirements = False
    for version in job["versions"]:
        report = version.get("stages", {}).get("data_checked", {}).get("report", {})
        visible, required, unknown, complete = _retention_evidence(report)
        evidence[version["version"]] = visible, complete
        unknown_requirements |= unknown
        for key in required:
            maximum_visible[key] = max(maximum_visible.get(key, 0), visible[key])
    for version in reversed(job["versions"]):
        checked = _receipt({**job, "versions": [version]}, index, include_candidate=False)
        stages = checked["stages"]
        if (stages["rendered"]["status"] != "passed"
                or stages["layout_checked"]["status"] != "passed"
                or stages["data_checked"]["status"] in {"issues", "stale", "failed"}
                or stages["visual_checked"]["status"] == "stale"
                or stages["packaged"].get("project_dir")
                and stages["packaged"]["status"] != "passed"):
            continue
        candidate = {key: checked.get(key) for key in (
            "image_path", "image_sha256", "project_dir", "quality_status",
        )}
        reference = stages["visual_checked"].get("report_reference", {})
        if reference.get("path"):
            candidate["review_path"] = reference["path"]
        visible, complete = evidence[version["version"]]
        measured = [min(1.0, visible.get(key, 0) / count)
                    for key, count in maximum_visible.items()
                    if count > 0]
        known_coverage = min(measured, default=0.0)
        coverage_known = bool(measured) and complete and not unknown_requirements
        coverage = known_coverage if coverage_known else None
        # An unidentified historical requirement can hide missing content. Keep its
        # lower bound at zero, and prefer complete identities over ambiguous records.
        coverage_lower_bound = known_coverage if coverage_known else 0.0
        visual = stages["visual_checked"]
        issues = visual.get("report", {}).get("issues", [])
        errors = sum(issue.get("severity") == "error" for issue in issues)
        warnings = sum(issue.get("severity") == "warning" for issue in issues)
        tier = {"passed": 3, "issues": 2, "unverified": 1}.get(visual["status"], 0)
        candidate.update(visual_review_status=visual["status"], retained_coverage=coverage,
                         coverage_status="verified" if coverage_known else "unknown",
                         known_identity_coverage=known_coverage if measured else None,
                         coverage_lower_bound=coverage_lower_bound,
                         coverage_scope=("required_native_frame_union"
                                         if coverage_known else "unknown"))
        candidate.update(version=version["version"], reason=(
            "Selected unchanged measured-layout candidate by required retained coverage, "
            "completed visual evidence and defect severity; all stage statuses stay truthful."
        ))
        candidates.append(((coverage_lower_bound, int(complete), tier, -errors, -warnings,
                            version["version"]), candidate))
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


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
