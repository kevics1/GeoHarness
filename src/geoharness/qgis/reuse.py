"""Reuse a native render only after fresh input measurement and complete file revalidation."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from openharness.tools.base import ToolResult

from geoharness.qgis.project_worker import _layout_input_digest


def render_key(payload, config):
    if (not config.qgis.reuse_cache_entries or not payload.get("_layout_sources")
            or len(payload["_layout_sources"]) != len(payload.get("layers", []))):
        return None
    # Frozen sources bind actual vector rows/raster samples, not random temporary paths.
    if any(not item.get("data_sha256") for item in payload["_layout_sources"]):
        return None
    def external_font(value):
        if isinstance(value, dict):
            return bool(value.get("font_file")) or any(external_font(v) for v in value.values())
        return isinstance(value, list) and any(external_font(v) for v in value)

    if external_font(payload):
        return None  # Explicit fonts have file content outside the frozen layer identity.
    return hashlib.sha256(json.dumps({
        "input": _layout_input_digest(payload), "map_id": payload.get("map_id"),
        "output_format": payload.get("output_format"),
        "output_path": payload.get("output_path"),
        "project_format": payload.get("project_format"),
        "strict_layout": payload.get("strict_layout"),
        "qgis": config.qgis.model_dump(mode="json"),
    }, sort_keys=True).encode()).hexdigest()


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _package_files(result):
    path = result.metadata.get("project_manifest")
    if not isinstance(path, str):
        return {}
    manifest = Path(path).absolute()
    if manifest.resolve() != manifest or not manifest.is_file():
        return {}
    packet = json.loads(manifest.read_text(encoding="utf-8"))
    files = packet.get("files")
    if not isinstance(files, list) or not files or len(files) > 1024:
        return {}
    measured = {str(manifest): _digest(manifest)}
    for entry in files:
        if not isinstance(entry, str):
            return {}
        file = manifest.parent / entry
        if (file.resolve() != file or not file.is_relative_to(manifest.parent)
                or not file.is_file()):
            return {}
        measured[str(file)] = _digest(file)
    for key in ("output_path", "preview_path", "project_file"):
        value = result.metadata.get(key)
        if isinstance(value, str):
            file = Path(value).absolute()
            if file.resolve() != file or not file.is_file():
                return {}
            measured[str(file)] = _digest(file)
    return measured


def recall_render(metadata, key, *, current_version=None):
    if not key:
        return None
    cache = metadata.setdefault("geo_render_reuse", {})
    item = cache.get(key)
    if item is None:
        return None
    try:
        receipt = item['result'].metadata.get('map_receipt', {})
        version = receipt.get('selected_version', receipt.get('current_version'))
        if version is not None and current_version != version:
            return None  # A past version must never silently replace the current selection.
        for value, digest in item["files"].items():
            path = Path(value)
            if path.resolve() != path or not path.is_file() or _digest(path) != digest:
                cache.pop(key, None)
                return None
        previous = item["result"]
        raw, end = json.JSONDecoder().raw_decode(previous.output.lstrip())
        raw.update(reused=True, render_executed=False,
            reused_artifact_version=version,
            reuse_note="当前输入已重新测量且工程全部文件哈希未变，复用同一版本，未重启原生渲染。")
        return ToolResult(
            output=json.dumps(raw, ensure_ascii=False) + previous.output.lstrip()[end:],
            metadata={**deepcopy(previous.metadata), "reused": True, "render_executed": False})
    except (OSError, ValueError, TypeError):
        cache.pop(key, None)
        return None


def remember_render(metadata, key, result, *, limit):
    if not key or result.is_error or result.metadata.get("receipt_status") == "failed":
        return
    try:
        files = _package_files(result)
        if not files:
            return
        cache = metadata.setdefault("geo_render_reuse", {})
        cache[key] = {"files": files, "result": deepcopy(result)}
        for old in list(cache)[:-limit]:
            cache.pop(old)
    except (OSError, ValueError, TypeError):
        return  # Cache failure never turns a completed render into a failure.
