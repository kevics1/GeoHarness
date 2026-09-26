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


class FileLoader:
    """Load spatial data from local files.

    Scans a directory for supported file formats.
    CRS is auto-detected via geopandas.
    """

    def __init__(self, file_dir: str) -> None:
        self._file_dir = Path(file_dir)
        if not self._file_dir.exists():
            logger.warning("File directory does not exist: %s", self._file_dir)

    def _scan_files(self) -> list[Path]:
        """Scan directory for supported spatial files."""
        if not self._file_dir.exists():
            return []

        files: list[Path] = []
        for ext in _SPATIAL_EXTS | {_CSV_EXT}:
            files.extend(self._file_dir.glob(f"*{ext}"))
            files.extend(self._file_dir.glob(f"*{ext.upper()}"))

        # Deduplicate (Windows is case-insensitive, so *.geojson and *.GEOJSON
        # match the same files). Use lowercased str(path) as dedup key.
        seen: set[str] = set()
        unique: list[Path] = []
        for f in files:
            key = str(f).lower()
            if key not in seen:
                seen.add(key)
                unique.append(f)

        # For .shp, don't include .shx, .dbf, .prj sidecar files
        return sorted(
            f for f in unique
            if f.suffix.lower() not in {".shx", ".dbf", ".prj", ".cpg"}
        )

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
        """Find a file by name (with or without extension)."""
        # Try as full path
        p = Path(name)
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

        return None
