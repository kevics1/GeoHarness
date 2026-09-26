"""RasterLoader — local raster file connector.

Handles GeoTIFF/IMG/ASC/VRT/JP2 and friends via ``rasterio``. Kept separate
from the vector ``FileLoader`` so a raster listing never walks vector files
and vice versa: each loader answers for exactly one data type.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from geoharness.data.catalog import DataSource

logger = logging.getLogger(__name__)

# PROJ requires its SQLite database to use layout version >= 6. Older ones
# (PostgreSQL's PostGIS bundle = 2, pyproj's wheel = 4) make every EPSG lookup
# fail. rasterio ships a current copy of its own.
_PROJ_DB_MIN_LAYOUT_MINOR = 6


def _proj_db_layout_minor(directory: str | None) -> int | None:
    """Read ``DATABASE.LAYOUT.VERSION.MINOR`` from a proj.db, or None.

    Deliberately uses ``sqlite3`` only — consulting rasterio here would
    initialise PROJ with the very value we are trying to correct.
    """
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
    """Point ``PROJ_LIB`` at a usable ``proj.db``.

    A system-wide ``PROJ_LIB`` aimed at a stale installation (commonly the copy
    inside ``PostgreSQL\\..\\postgis-3.6\\proj``) makes every EPSG lookup fail
    with "DATABASE.LAYOUT.VERSION.MINOR = 2 whereas a number >= 6 is expected".
    Rasters are usually projected (UTM), so this breaks practically every real
    file. When the configured database is unusable, fall back to rasterio's.
    """
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
        "PROJ_LIB (%s) uses proj.db layout %s (< %d); using rasterio's data at %s",
        current or "<unset>",
        current_minor,
        _PROJ_DB_MIN_LAYOUT_MINOR,
        bundled,
    )
    os.environ["PROJ_LIB"] = bundled
    os.environ["PROJ_DATA"] = bundled


ensure_proj_data()

# Raster formats rasterio/GDAL can open. Kept deliberately narrow so a stray
# PNG in a workspace is not mistaken for a georeferenced raster.
_RASTER_EXTS = frozenset(
    {".tif", ".tiff", ".img", ".asc", ".vrt", ".jp2", ".nc", ".hdf", ".h5"}
)

# Same pruning policy as the vector loader — never descend into build noise.
_PRUNE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env",
    "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", ".idea", ".vscode", "site-packages",
    "dist", "build", ".tox", ".nox", ".eggs",
})

_SCAN_TTL_SECONDS = 5.0


class RasterLoader:
    """Discover and describe local raster files."""

    def __init__(
        self,
        file_dir: str,
        recursive: bool = True,
        max_depth: int = 6,
    ) -> None:
        self._file_dir = Path(file_dir).expanduser()
        self._recursive = recursive
        self._max_depth = max_depth
        self._cache: list[Path] | None = None
        self._cache_time: float = 0.0
        if not self._file_dir.exists():
            logger.warning("Raster directory does not exist: %s", self._file_dir)

    # ── discovery ────────────────────────────────────────────────────

    def _iter_candidates(self) -> list[Path]:
        results: list[Path] = []

        def walk(directory: Path, depth: int) -> None:
            if depth > self._max_depth:
                return
            try:
                entries = list(directory.iterdir())
            except OSError as exc:
                logger.debug("Cannot list %s: %s", directory, exc)
                return
            for entry in entries:
                try:
                    if entry.is_dir():
                        if entry.name in _PRUNE_DIRS or entry.name.startswith("."):
                            continue
                        walk(entry, depth + 1)
                    elif entry.suffix.lower() in _RASTER_EXTS:
                        results.append(entry)
                except OSError:
                    continue

        walk(self._file_dir, 1)
        return results

    def _scan_files(self) -> list[Path]:
        """Scan for raster files (pruned, depth-bounded, briefly cached)."""
        if not self._file_dir.exists():
            return []

        import time as _time

        now = _time.monotonic()
        if self._cache is not None and (now - self._cache_time) < _SCAN_TTL_SECONDS:
            return self._cache

        if self._recursive:
            candidates = self._iter_candidates()
        else:
            candidates = []
            try:
                for entry in self._file_dir.iterdir():
                    if entry.is_file() and entry.suffix.lower() in _RASTER_EXTS:
                        candidates.append(entry)
            except OSError:
                pass

        candidates.sort(key=lambda p: str(p).lower())
        self._cache = candidates
        self._cache_time = now
        return candidates

    def list_sources(self) -> list[DataSource]:
        """List raster files as DataSource descriptors (no file opening)."""
        return [
            DataSource(
                name=path.stem,
                source_type="raster",
                metadata={
                    "format": path.suffix.lower().lstrip("."),
                    "path": str(path),
                },
            )
            for path in self._scan_files()
        ]

    # ── resolution ───────────────────────────────────────────────────

    def resolve_path(self, name: str) -> Path | None:
        """Resolve a raster name (with/without extension) to a path."""
        p = Path(name).expanduser()
        if p.exists() and p.is_file():
            return p

        p = self._file_dir / name
        if p.exists() and p.is_file():
            return p
        for ext in _RASTER_EXTS:
            cand = self._file_dir / f"{name}{ext}"
            if cand.exists():
                return cand

        if self._recursive and self._file_dir.exists():
            stem = Path(name).stem
            for path in self._scan_files():
                if path.stem == stem or path.name == name:
                    return path
        return None

    def get_source_detail(self, name: str) -> dict[str, Any]:
        """Describe a raster: size, bands, CRS, bounds, resolution, nodata."""
        path = self.resolve_path(name)
        if path is None:
            return {}
        try:
            return self._describe(path)
        except Exception as exc:  # noqa: BLE001 — unopenable file == no detail
            logger.warning("Cannot describe raster %s: %s", path, exc)
            return {}

    def _describe(self, path: Path) -> dict[str, Any]:
        import rasterio

        with rasterio.open(path) as ds:
            bounds = ds.bounds
            detail: dict[str, Any] = {
                "name": path.stem,
                "source_type": "raster",
                "format": (ds.driver or path.suffix.lstrip(".")).lower(),
                "path": str(path),
                "crs": str(ds.crs) if ds.crs else "",
                "width": ds.width,
                "height": ds.height,
                "band_count": ds.count,
                "dtype": ds.dtypes[0] if ds.dtypes else "",
                "nodata": ds.nodata,
                "bbox": [
                    float(bounds.left),
                    float(bounds.bottom),
                    float(bounds.right),
                    float(bounds.top),
                ],
                "resolution": [
                    abs(float(ds.res[0])),
                    abs(float(ds.res[1])),
                ],
            }
            return detail

    def read_band(
        self, name: str, band: int = 1, max_pixels: int = 4_000_000
    ) -> Any:
        """Read a band as a numpy array, decimating huge rasters.

        Raises:
            ValueError: If the file is missing, the band is out of range, or
                rasterio is unavailable.
        """
        import numpy as np
        import rasterio

        path = self.resolve_path(name)
        if path is None:
            raise ValueError(f"Raster not found: {name!r}")
        with rasterio.open(path) as ds:
            if band < 1 or band > ds.count:
                raise ValueError(
                    f"Band {band} out of range (file has {ds.count} band(s))"
                )
            count = ds.width * ds.height
            step = max(1, int((count / max_pixels) ** 0.5))
            data = ds.read(
                band,
                out_shape=(max(1, ds.height // step), max(1, ds.width // step)),
                masked=True,
            )
        arr = data.astype("float64").filled(np.nan)
        return arr

    def band_stats(self, name: str, band: int = 1) -> dict[str, Any]:
        """Return masked min/max/mean/std/percentiles for one band."""
        import numpy as np

        arr = self.read_band(name, band=band)
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            raise ValueError(f"Band {band} of {name!r} has no valid pixels")
        return {
            "band": band,
            "count": int(finite.size),
            "min": float(np.min(finite)),
            "max": float(np.max(finite)),
            "mean": float(np.mean(finite)),
            "std": float(np.std(finite)),
            "p5": float(np.percentile(finite, 5)),
            "p50": float(np.percentile(finite, 50)),
            "p95": float(np.percentile(finite, 95)),
        }
