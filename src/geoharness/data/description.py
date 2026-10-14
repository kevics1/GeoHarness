"""Visible source contracts shared by data tools, without connection credentials."""

from __future__ import annotations

from typing import Any


def describe_source(detail: dict[str, Any], *, kind: str) -> str:
    """Describe all measured schema fields; unavailable facts remain explicit."""
    lines = [f"  Source type: {kind}"]
    if kind == "postgis":
        lines.append(f"  Table locator: {detail.get('name') or 'unknown'} (no filesystem path)")
    else:
        lines.append(f"  Path: {detail.get('path') or 'unknown'}")
    lines.append(f"  Format: {detail.get('format') or 'unknown'}")
    lines.append(f"  CRS: {detail.get('crs') or 'unknown'}")
    for key in ("crs_note", "note"):
        if detail.get(key):
            lines.append(f"  Note: {detail[key]}")
    bounds = detail.get("bbox")
    if hasattr(bounds, "tolist"):
        bounds = bounds.tolist()
    lines.append("  Extent [xmin, ymin, xmax, ymax]: "
                 + str(bounds if bounds is not None else "unknown"))
    for key, label in (
        ("size_bytes", "File size (bytes)"),
        ("encoding", "Encoding"), ("encoding_source", "Encoding source"),
        ("encoding_status", "Encoding status"),
        ("geometry_type", "Geometry"), ("feature_count", "Features"), ("row_count", "Rows"),
        ("row_limit", "Loaded row limit"), ("width", "Width (px)"), ("height", "Height (px)"),
        ("band_count", "Bands"), ("dtype", "Pixel dtype"), ("dtypes", "Band dtypes"),
        ("resolution", "Resolution"), ("nodata", "NoData"), ("nodatavals", "Band NoData"),
        ("descriptions", "Band descriptions"), ("units", "Band units"),
        ("transform", "Affine transform"), ("scales", "Band scales"),
        ("offsets", "Band offsets"), ("colorinterp", "Band color interpretation"),
    ):
        if key in detail:
            lines.append(f"  {label}: {detail[key]}")
    schema = detail.get("schema") or {}
    if schema:
        lines.append(f"  Columns ({len(schema)}):")
        for name, dtype in schema.items():
            nulls = (detail.get("null_counts") or {}).get(name)
            suffix = f"; nulls={nulls}" if nulls is not None else ""
            lines.append(f"    - {name}: {dtype}{suffix}")
    elif kind != "raster":
        columns = detail.get("columns") or []
        lines.append(f"  Columns ({len(columns)}):" if columns else "  Columns: unknown")
        lines.extend(f"    - {name}: unknown" for name in columns)
    return "\n".join(lines)
