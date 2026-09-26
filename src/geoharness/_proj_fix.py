"""Early PROJ environment repair.

A system-wide ``PROJ_LIB`` pointing at a stale ``proj.db`` (commonly the copy
inside ``PostgreSQL\\..\\postgis-3.6\\proj``, layout version 2) poisons every
EPSG lookup in the process — and PROJ initialises ONCE, on first use, so
whatever database is configured at that moment wins for the whole session.

Import this module as early as possible (every geoharness module that touches
geopandas/rasterio imports it at load time). It must stay dependency-free:
checking rasterio/pyproj here would itself initialise PROJ with the bad value.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

# PROJ requires its SQLite database to use layout version >= 6. Older ones
# (PostgreSQL's PostGIS bundle = 2, pyproj's wheel = 4) make every EPSG lookup
# fail. rasterio ships a current copy of its own.
_PROJ_DB_MIN_LAYOUT_MINOR = 6
_fixed = False


def _proj_db_layout_minor(directory: str | None) -> int | None:
    """Read ``DATABASE.LAYOUT.VERSION.MINOR`` from a proj.db, or ``None``."""
    if not directory:
        return None
    db = Path(directory) / "proj.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(str(db))
        try:
            row = con.execute(
                "SELECT value FROM metadata "
                "WHERE key = 'DATABASE.LAYOUT.VERSION.MINOR'"
            ).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — unreadable db == unusable
        return None
    if not row:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _rasterio_proj_dir() -> str | None:
    """Locate rasterio's bundled PROJ data without importing rasterio."""
    spec = importlib.util.find_spec("rasterio")
    if not spec or not spec.origin:
        return None
    candidate = Path(spec.origin).parent / "proj_data"
    return str(candidate) if candidate.exists() else None


def ensure_proj_data() -> None:
    """Point ``PROJ_LIB``/``PROJ_DATA`` at a usable ``proj.db`` (idempotent)."""
    global _fixed
    if _fixed:
        return
    _fixed = True

    current = os.environ.get("PROJ_LIB")
    current_minor = _proj_db_layout_minor(current)
    if current_minor is not None and current_minor >= _PROJ_DB_MIN_LAYOUT_MINOR:
        return  # already usable

    bundled = _rasterio_proj_dir()
    if not bundled:
        return
    if (_proj_db_layout_minor(bundled) or 0) < _PROJ_DB_MIN_LAYOUT_MINOR:
        return  # nothing better to fall back to

    logger.warning(
        "PROJ_LIB (%s) uses proj.db layout %s (< %d); using rasterio's data "
        "at %s",
        current or "<unset>",
        current_minor,
        _PROJ_DB_MIN_LAYOUT_MINOR,
        bundled,
    )
    os.environ["PROJ_LIB"] = bundled
    os.environ["PROJ_DATA"] = bundled
