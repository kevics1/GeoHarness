"""Avoid rerunning deterministic map failures until arguments or local inputs change."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from functools import wraps
from pathlib import Path

from openharness.tools.base import ToolResult


def _fingerprint(tool, arguments, context) -> str | None:
    payload = arguments.model_dump(mode="json")
    paths = []

    def collect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"file_path", "project_path", "basemap_raster", "font_file"} and item:
                    paths.append(item)
                elif key == "source_name" and item:
                    path = Path(item)
                    if not (path if path.is_absolute() else context.cwd / path).is_file():
                        raise ValueError("A catalog alias has no stable local identity")
                    paths.append(item)
                elif key == "analysis_manifests":
                    paths.extend(item)
                elif key == "layers" and isinstance(item, str) and item:
                    collect(json.loads(item))
                else:
                    collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    try:
        collect(payload)
        config = context.metadata.get("geoharness_config")
        roots = []
        if config:
            from geoharness.cartography.facts import infer_analysis_manifests
            from geoharness.qgis.worker_context import WorkerContext
            from geoharness.tools.geo_qgis import qgis_read_roots

            roots = qgis_read_roots(config, context.cwd)
            guard = WorkerContext(context.cwd, context.cwd, roots)
            manifests = list(dict.fromkeys([
                *payload.get("analysis_manifests", []),
                *infer_analysis_manifests([{"file_path": value} for value in paths],
                                         cwd=context.cwd, read_roots=roots),
            ]))
            for value in manifests:
                try:
                    manifest = guard.read_path(value)
                    if manifest.stat().st_size > 1024 * 1024:
                        return None
                    document = json.loads(manifest.read_text(encoding="utf-8-sig"))
                    for entry in document.get("paths", {}).values():
                        for key in ("geojson", "csv"):
                            if isinstance(entry, dict) and entry.get(key):
                                paths.append(str(guard.read_path(entry[key])))
                    paths.append(str(manifest))
                except (ValueError, OSError, TypeError, AttributeError):
                    return None
        files = {}
        for value in paths:
            if not isinstance(value, str) or "://" in value or "|" in value or value.startswith(
                ("\\\\", "//"),
            ):
                return None
            path = Path(value).expanduser()
            path = (path if path.is_absolute() else context.cwd / path).resolve()
            try:
                stat = path.stat()
                files[str(path)] = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]
                if path.suffix.lower() == ".vrt":
                    return None  # Referenced data can change without changing the VRT.
                if path.suffix.lower() in {".shp", ".gpkg", ".tif", ".tiff"}:
                    pattern = path.stem + ".*" if path.suffix.lower() == ".shp" else path.name + "*"
                    for companion in path.parent.glob(pattern):
                        details = companion.stat()
                        files[str(companion)] = [details.st_size, details.st_mtime_ns,
                                                 details.st_ctime_ns]
            except FileNotFoundError:
                files[str(path)] = None
        settings = ([asdict(value) if is_dataclass(value) else value.model_dump(mode="json")
                     for value in (config.cartography, config.qgis)] if config else None)
        return hashlib.sha256(json.dumps(
            [tool.name, str(context.cwd.resolve()), payload, files, settings,
             [str(root) for root in roots]], sort_keys=True,
        ).encode()).hexdigest()
    except (ValueError, TypeError, OSError):
        return None  # Unstable inputs retain the normal tool path.


def guard_map_errors(execute):
    @wraps(execute)
    async def guarded(self, arguments, context, **kwargs):
        key = _fingerprint(self, arguments, context) if not kwargs else None
        cache = context.metadata.setdefault("map_deterministic_errors", {})
        previous = cache.get(key) if key else None
        if previous is not None:
            return ToolResult(
                output=previous["output"] + "\nIdentical deterministic failure already recorded; "
                "no tool was executed and no map version was consumed. Change the diagnosed "
                "arguments or input data before trying again; retain successful analysis outputs.",
                is_error=True,
                metadata={**previous["metadata"], "duplicate_error_blocked": True,
                          "render_executed": False},
            )
        result = await execute(self, arguments, context, **kwargs)
        if key and result.is_error and result.metadata.get("deterministic_error"):
            # A source may have changed during execution; do not cache that race.
            if key == _fingerprint(self, arguments, context):
                if len(cache) >= 64:
                    cache.pop(next(iter(cache)))
                cache[key] = {"output": result.output, "metadata": result.metadata}
        return result

    return guarded
