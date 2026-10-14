"""Declared text encodings, without guessing from filenames or neighboring layers."""

from __future__ import annotations

import codecs
from pathlib import Path


def source_encoding(path: str | Path) -> dict[str, str]:
    path = Path(path)
    if path.suffix.lower() in {".geojson", ".json"}:
        return {"encoding": "utf-8", "encoding_source": "format",
                "encoding_status": "format_defined"}
    if path.suffix.lower() == ".shp":
        cpg = path.with_suffix(".cpg")
        try:
            if cpg.is_symlink() or cpg.stat().st_size > 128:
                raise ValueError("Invalid encoding declaration")
            declared = cpg.read_text(encoding="utf-8-sig").strip()
            # Numeric Windows code pages are common .cpg declarations.
            candidate = "cp" + declared if declared.isdigit() else declared
            encoding = codecs.lookup(candidate).name
            return {"encoding": encoding, "encoding_source": "cpg",
                    "encoding_status": "declared"}
        except (OSError, ValueError, LookupError):
            pass
    return {"encoding": "unknown", "encoding_source": "driver_default",
            "encoding_status": "unknown"}


def vector_read_options(info: dict) -> dict[str, str]:
    encoding = info.get("encoding")
    if encoding and encoding != "unknown":
        return {"encoding": codecs.lookup(encoding).name}
    return {}
