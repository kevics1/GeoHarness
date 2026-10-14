"""Bounded, measured local data descriptions shared across tool invocations.

Only metadata is retained here. Raster discovery opens headers through the
existing loader; it never reads pixels. Consumers should use ``current_profiles``
to avoid treating a description of a replaced/deleted file as current data.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

# Technical context limits, independent of any dataset or analysis domain.
MAX_AUTO_INSPECT = 8
MAX_LIST_FILES = 64
MAX_PROFILES = 32
MAX_SCHEMA_FIELDS = 40
_MAX_TEXT = 256
_SHAPEFILE_PARTS = (".shp", ".shx", ".dbf", ".prj", ".cpg")

CRS_OPERATION_HINT = (
    "CRS operation: Before each overlay/mask, transform geometries with known CRS "
    "to the target layer/raster CRS. Metric analysis may keep its own projected CRS. "
    "If a source CRS is unknown, verify it before transforming."
)


def canonical_path(path: str | Path) -> str:
    """Return a real absolute path; normalize case only for dictionary keys."""
    return str(Path(path).expanduser().resolve())


def profile_key(path: str | Path) -> str:
    return os.path.normcase(canonical_path(path))


def file_signature(path: str | Path) -> dict[str, Any] | None:
    """Stat a file and, for shapefiles, the sidecars carrying geometry/schema/CRS."""
    try:
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_file():
            return None
        parts = [resolved]
        if resolved.suffix.lower() == ".shp":
            parts.extend(resolved.with_suffix(ext) for ext in _SHAPEFILE_PARTS[1:])
        signature = {}
        for part in parts:
            try:
                stat = part.stat()
            except FileNotFoundError:
                signature[part.name] = None
                continue
            signature[part.name] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
            }
        return signature
    except (OSError, ValueError):
        return None


def _finite_sequence(value: Any, length: int) -> list[float] | None:
    try:
        if len(value) != length:
            return None
        numbers = [float(number) for number in value]
        return numbers if all(math.isfinite(number) for number in numbers) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _nodata(value: Any) -> str | float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if math.isnan(number):
        return "NaN"
    if math.isinf(number):
        return "Infinity" if number > 0 else "-Infinity"
    return number


def make_profile(detail: dict[str, Any], *, kind: str) -> dict[str, Any] | None:
    """Keep verified fields only, with JSON-safe scalars and bounded schema/text."""
    raw_path = detail.get("path")
    if not isinstance(raw_path, (str, Path)) or not str(raw_path).strip():
        return None
    try:
        path = canonical_path(raw_path)
    except (OSError, ValueError):
        return None
    # A post-read stat cannot prove which version supplied the metadata. Preserve
    # the loader's measurement signature, and never promote unsigned adapters.
    signature = detail.get("_measured_file_stat")
    if "_measured_file_stat" in detail and file_signature(path) != signature:
        return None
    profile: dict[str, Any] = {
        "kind": kind,
        "name": str(detail.get("name") or Path(path).stem)[:_MAX_TEXT],
        "path": path,
        "file_stat": signature,
        "freshness": "current" if signature is not None else "unverified",
    }
    if "crs" in detail:
        crs = detail["crs"]
        profile["crs"] = str(crs)[:_MAX_TEXT] if crs and str(crs) != "unknown" else None
    bbox = _finite_sequence(detail.get("bbox"), 4)
    if bbox is not None:
        profile["bbox"] = bbox
    if kind == "vector":
        for field in ("encoding", "encoding_source", "encoding_status"):
            if field in detail:
                profile[field] = str(detail[field])[:_MAX_TEXT]
        geometry_type = detail.get("geometry_type")
        if geometry_type and geometry_type != "unknown":
            profile["geometry_type"] = str(geometry_type)[:_MAX_TEXT]
        for field in ("feature_count", "row_count"):
            count = detail.get(field)
            if isinstance(count, int) and count >= 0:
                profile["feature_count"] = count
                break
        schema = detail.get("schema")
        if isinstance(schema, dict):
            profile["schema"] = {
                str(name)[:_MAX_TEXT]: str(dtype)[:_MAX_TEXT]
                for name, dtype in list(schema.items())[:MAX_SCHEMA_FIELDS]
            }
            if len(schema) > MAX_SCHEMA_FIELDS:
                profile["schema_omitted"] = len(schema) - MAX_SCHEMA_FIELDS
        columns = detail.get("columns")
        if isinstance(detail.get("null_counts"), dict):
            profile["null_counts"] = {str(name)[:_MAX_TEXT]: count
                for name, count in list(detail["null_counts"].items())[:MAX_SCHEMA_FIELDS]
                if type(count) is int and count >= 0}
        if isinstance(columns, (list, tuple)):
            profile["columns"] = [str(col)[:_MAX_TEXT] for col in columns[:MAX_SCHEMA_FIELDS]]
            if len(columns) > MAX_SCHEMA_FIELDS:
                profile["columns_omitted"] = len(columns) - MAX_SCHEMA_FIELDS
    elif kind == "raster":
        for field in ("width", "height", "band_count"):
            count = detail.get(field)
            if isinstance(count, int) and count >= 0:
                profile[field] = count
        if "width" in profile and "height" in profile:
            profile["size"] = [profile["width"], profile["height"]]
        if detail.get("dtype"):
            profile["dtype"] = str(detail["dtype"])[:_MAX_TEXT]
        resolution = _finite_sequence(detail.get("resolution"), 2)
        if resolution is not None:
            profile["resolution"] = resolution
        if "nodata" in detail:
            profile["nodata"] = _nodata(detail["nodata"])
    return profile


def current_profiles(metadata: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Pure bounded snapshot; changed or deleted files are excluded immediately.

    Loader adapters without a measurement signature may retain an explicitly
    unverified profile, even if their path exists. They never claim current data.
    """
    state = metadata.get("geo_data_context") or {}
    if not isinstance(state, dict):
        return {}
    stored = state.get("profiles") or {}
    if not isinstance(stored, dict):
        return {}
    result = {}
    for key, profile in list(stored.items())[-MAX_PROFILES:]:
        if not isinstance(profile, dict) or not profile.get("path"):
            continue
        previous = profile.get("file_stat")
        if previous is not None and file_signature(profile["path"]) != previous:
            continue
        result[key] = dict(profile)
    return result


def record_profiles(metadata: dict[str, Any], profiles: list[dict[str, Any]]) -> None:
    """Commit a successful tool result on the calling thread, without GeoDataFrames."""
    retained = current_profiles(metadata)
    for profile in profiles:
        if not profile or not profile.get("path"):
            continue
        measured = profile.get("file_stat")
        if measured is not None and file_signature(profile["path"]) != measured:
            continue
        key = profile_key(profile["path"])
        retained.pop(key, None)
        retained[key] = profile
    state = metadata.setdefault("geo_data_context", {})
    state["profiles"] = dict(list(retained.items())[-MAX_PROFILES:])


def local_source_listing(loader: Any, *, kind: str) -> tuple[str, dict[str, Any]]:
    """Inspect a limited number of sources, preserving listing on per-file errors."""
    sources = loader.list_sources()
    if not sources:
        return f"No local {kind} files found.", {"data_profiles": []}
    lines = []
    profiles = []
    inspected = 0
    for index, source in enumerate(sources[:MAX_LIST_FILES]):
        meta = getattr(source, "metadata", {}) or {}
        path = meta.get("path")
        line = f"  - {source.name} ({meta.get('format', kind)})"
        if path:
            line += f"; Path: {path}"
        detail = None
        reason = "inspection limit"
        if index < MAX_AUTO_INSPECT:
            try:
                detail = loader.get_source_detail(str(path or source.name))
                reason = "unreadable or metadata unavailable"
            except Exception:  # noqa: BLE001 — one bad file must not hide usable sources
                reason = "metadata inspection failed"
        profile = (
            make_profile(detail, kind=kind)
            if isinstance(detail, dict) and detail and not detail.get("error") else None
        )
        if profile is None:
            line += f"; not inspected ({reason})"
        else:
            inspected += 1
            profiles.append(profile)
            line += f"; CRS: {profile.get('crs') or 'unknown'}"
            if "bbox" in profile:
                line += f"; BBox: {profile['bbox']}"
            if kind == "vector":
                if "geometry_type" in profile:
                    line += f"; Geometry: {profile['geometry_type']}"
                if "feature_count" in profile:
                    line += f"; Features: {profile['feature_count']}"
            else:
                for field in ("size", "band_count", "resolution", "dtype", "nodata"):
                    if field in profile:
                        line += f"; {field}: {profile[field]}"
            if not path:
                line += f"; Path: {profile['path']}"
        lines.append(line)
    omitted = max(0, len(sources) - MAX_LIST_FILES)
    if omitted:
        lines.append(f"  ... {omitted} additional files not displayed or inspected.")
    lines.append(
        f"Metadata inspected: {inspected}; not inspected: {len(sources) - inspected}. "
        "Use inspect for any uninspected file before spatial operations."
    )
    lines.append(CRS_OPERATION_HINT)
    return f"Found {len(sources)} {kind} file(s):\n" + "\n".join(lines), {
        "data_profiles": profiles,
        "data_discovery": {
            "total": len(sources), "inspected": inspected,
            "not_inspected": len(sources) - inspected, "omitted": omitted,
        },
    }
