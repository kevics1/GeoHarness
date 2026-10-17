"""Read the explicit preview contract shared by full and compact QGIS results."""

from __future__ import annotations


def returned_page_previews(payload: dict) -> list[dict]:
    """Preserve page identity; do not discover or infer filenames from directories."""
    native = payload.get("native_facts")
    native = native if isinstance(native, dict) else {}
    pages = payload.get("page_previews", native.get("page_previews", []))
    if not isinstance(pages, list):
        return []
    return [item for item in pages if isinstance(item, dict)
            and isinstance(item.get("path"), str) and item["path"]]


def returned_preview_paths(payload: dict) -> tuple[str, ...]:
    """Explicit returned paths only; callers must still enforce filesystem boundaries."""
    values = [payload.get("preview_path"), payload.get("output_path")]
    values.extend(item["path"] for item in returned_page_previews(payload))
    native = payload.get("native_facts")
    if isinstance(native, dict) and isinstance(native.get("roster_previews"), list):
        values.extend(native["roster_previews"])
    return tuple(dict.fromkeys(value for value in values if isinstance(value, str) and value))
