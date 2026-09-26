"""FileLoader — local spatial file connector.

Supports .shp, .geojson, .gpkg, .csv (with coordinate columns).
Uses geopandas for CRS detection and bbox clipping.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from geoharness.data.catalog import DataSource

logger = logging.getLogger(__name__)

# Supported file extensions
_SPATIAL_EXTS = {".shp", ".geojson", ".gpkg", ".json"}
_CSV_EXT = ".csv"

# Directories that never contain user data. Pruning them keeps a recursive
# scan of a project root (which may hold .venv/.git) from walking tens of
# thousands of irrelevant files.
_PRUNE_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "env", ".env",
    "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", ".idea", ".vscode", "site-packages",
    "dist", "build", ".tox", ".nox", ".eggs",
})

# Re-scanning on every call is wasteful; a short TTL keeps discovery snappy
# while still noticing files created seconds ago.
_SCAN_TTL_SECONDS = 5.0


class FileLoader:
    """Load spatial data from local files.

    Scans a directory for supported file formats.
    CRS is auto-detected via geopandas.
    """

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
            logger.warning("File directory does not exist: %s", self._file_dir)

    def _iter_candidates(self) -> list[Path]:
        """Walk the tree, pruning noise directories, bounded by depth."""
        results: list[Path] = []
        root = self._file_dir
        base_depth = len(root.parts)

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
                    elif entry.suffix.lower() in _SPATIAL_EXTS | {_CSV_EXT}:
                        results.append(entry)
                except OSError:
                    continue

        walk(root, 1)
        _ = base_depth
        return results

    def _scan_files(self) -> list[Path]:
        """Scan for supported spatial files (pruned, cached, depth-bounded).

        Searches subdirectories too (e.g. a workspace ``数据/`` folder), since
        spatial data is rarely kept flat. Directories such as ``.venv`` and
        ``.git`` are pruned, and results are cached briefly.
        """
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
            for ext in _SPATIAL_EXTS | {_CSV_EXT}:
                try:
                    candidates.extend(self._file_dir.glob(f"*{ext}"))
                    candidates.extend(self._file_dir.glob(f"*{ext.upper()}"))
                except OSError:
                    continue

        # Deduplicate (Windows is case-insensitive, so *.geojson and *.GEOJSON
        # match the same files). Use lowercased str(path) as dedup key.
        seen: set[str] = set()
        unique: list[Path] = []
        for f in candidates:
            if f.suffix.lower() in {".shx", ".dbf", ".prj", ".cpg"}:
                continue
            key = str(f).lower()
            if key not in seen:
                seen.add(key)
                unique.append(f)

        result = sorted(unique)
        self._cache = result
        self._cache_time = now
        return result

    def _read_file_info(self, path: Path) -> dict[str, Any]:
        """Read file metadata using geopandas."""
        info: dict[str, Any] = {
            "format": path.suffix.lower().lstrip("."),
            "path": str(path),
            "size_bytes": path.stat().st_size if path.exists() else 0,
        }

        try:
            import geopandas as gpd  # type: ignore[import-untyped]

            if path.suffix.lower() == _CSV_EXT:
                # CSV needs coordinate column specification
                info["format"] = "csv"
                info["note"] = "CSV requires coordinate column specification"
                return info

            gdf = gpd.read_file(path, rows=1)  # Read 1 row for schema
            info["crs"] = str(gdf.crs) if gdf.crs else "unknown"
            info["columns"] = list(gdf.columns)
            info["geometry_type"] = str(gdf.geom_type.iloc[0]) if len(gdf) > 0 else "unknown"

            # Get full bounds
            gdf_full = gpd.read_file(path)
            if len(gdf_full) > 0:
                bounds = gdf_full.total_bounds  # [minx, miny, maxx, maxy]
                info["bbox"] = [float(b) for b in bounds]
                info["row_count"] = len(gdf_full)

        except ImportError:
            info["error"] = "geopandas not installed"
        except Exception as e:
            info["error"] = str(e)

        return info

    def query_bbox(
        self, file_path: str, bbox: list[float]
    ) -> Any:
        """Read file and clip to bounding box.

        Args:
            file_path: Path to the spatial file.
            bbox: [xmin, ymin, xmax, ymax].

        Returns:
            GeoDataFrame clipped to bbox.
        """
        try:
            import geopandas as gpd  # type: ignore[import-untyped]
        except ImportError as e:
            raise ImportError(
                "geopandas is required for FileLoader. "
                "Install with: pip install geopandas"
            ) from e

        path = Path(file_path)
        if not path.is_absolute():
            path = self._file_dir / path

        gdf = gpd.read_file(path)
        if gdf.crs is None:
            gdf.set_crs("EPSG:4326", inplace=True)

        from shapely.geometry import box  # type: ignore[import-untyped]

        clip_geom = box(*bbox)
        return gdf[gdf.geometry.intersects(clip_geom)]

    def list_sources(self) -> list[DataSource]:
        """List all spatial files in the directory."""
        files = self._scan_files()
        sources: list[DataSource] = []

        for path in files:
            metadata: dict[str, Any] = {
                "format": path.suffix.lower().lstrip("."),
                "path": str(path),
            }
            # Don't read file info here — too expensive for listing
            # Details are fetched on demand via get_source_detail
            sources.append(DataSource(
                name=path.stem,
                source_type="file",
                metadata=metadata,
            ))

        return sources

    def get_source_detail(self, name: str) -> dict[str, Any]:
        """Get detailed information about a specific file.

        Args:
            name: File name (with or without extension) or full path.

        Returns:
            Dictionary with file details, or empty dict if not found.
        """
        path = self._find_file(name)
        if path is None:
            return {}

        return self._read_file_info(path)

    def resolve_path(self, name: str) -> Path | None:
        """Resolve a file name to an existing path, or ``None``.

        Public counterpart of ``_find_file`` for the cartography pipeline.
        """
        return self._find_file(name)

    def load_geodataframe(self, name: str) -> Any:
        """Load a spatial file as a GeoDataFrame.

        Args:
            name: File name (with/without extension) or full path.

        Returns:
            GeoDataFrame; CRS defaults to EPSG:4326 when the file has none.

        Raises:
            ValueError: If the file cannot be found or contains no features.
        """
        import geopandas as gpd  # type: ignore[import-untyped]

        path = self._find_file(name)
        if path is None:
            raise ValueError(f"File not found: {name!r}")
        gdf = gpd.read_file(path)
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        if gdf.empty:
            raise ValueError(f"File contains no features: {path}")
        return gdf

    def _find_file(self, name: str) -> Path | None:
        """Find a file by name (with or without extension).

        Searches the working directory recursively as well, so files kept in
        a subfolder (e.g. ``数据/城际公路.shp``) resolve by bare name.
        """
        # Try as full path
        p = Path(name).expanduser()
        if p.exists() and p.is_file():
            return p

        # Try relative to file_dir
        p = self._file_dir / name
        if p.exists() and p.is_file():
            return p

        # Try with each supported extension
        for ext in _SPATIAL_EXTS | {_CSV_EXT}:
            p = self._file_dir / f"{name}{ext}"
            if p.exists():
                return p

        # Recursive search by stem (name may or may not carry an extension)
        if self._recursive and self._file_dir.exists():
            stem = Path(name).stem
            for path in self._scan_files():
                if path.stem == stem or path.name == name:
                    return path

        return None
