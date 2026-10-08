"""Early PROJ environment repair.

A system-wide ``PROJ_DATA``/``PROJ_LIB`` can point at a database belonging to
another PROJ installation. Repair that inherited path before GDAL/rasterio
creates its PROJ context. Different wheels can bundle different PROJ versions:
pyproj selects its own internal data directory, while rasterio needs an
environment path compatible with its bundled database.

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

_fixed = False


def _proj_db_layout_minor(directory: str | None) -> int | None:
    """Read ``DATABASE.LAYOUT.VERSION.MINOR`` from a proj.db, or ``None``."""
    if not directory:
        return None
    db = Path(directory) / "proj.db"
    if not db.exists():
        return None
    try:
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
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
    """Repair stale inherited data using this rasterio wheel's database.

    PROJ_DATA takes precedence over the older PROJ_LIB. A successful repair is
    normal environment setup, so it does not produce a user-facing warning.
    No system environment, PostGIS data, or pyproj data directory is modified.
    """
    global _fixed
    if _fixed:
        return
    _fixed = True

    bundled = _rasterio_proj_dir()
    if not bundled:
        return
    bundled_minor = _proj_db_layout_minor(bundled)
    if bundled_minor is None:
        logger.warning("Cannot read rasterio's bundled PROJ database at %s", bundled)
        return

    current = os.environ.get("PROJ_DATA") or os.environ.get("PROJ_LIB")
    current_minor = _proj_db_layout_minor(current)
    if current_minor is not None and current_minor >= bundled_minor:
        return

    logger.debug(
        "PROJ data (%s) uses proj.db layout %s; selecting rasterio's bundled "
        "layout %s at %s",
        current or "<unset>",
        current_minor,
        bundled_minor,
        bundled,
    )
    os.environ["PROJ_LIB"] = bundled
    os.environ["PROJ_DATA"] = bundled
