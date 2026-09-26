"""Source resolution for the cartography pipeline.

Turns a tool request (file path / inline GeoJSON / PostGIS table /
administrative region) into a ``geopandas.GeoDataFrame`` ready for the
native renderer.

Kept separate from ``renderer.py`` so rendering stays pure (GeoDataFrame in,
file out) and is trivially testable with synthetic data.
"""

from __future__ import annotations

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SOURCE_TYPES = ("file", "postgis", "admin_kg")


def _from_geojson_text(text: str) -> Any:
    """Build a GeoDataFrame from an inline GeoJSON document."""
    import geopandas as gpd

    snippet = text.strip()
    if not snippet:
        raise ValueError("Empty GeoJSON text.")
    try:
        gdf = gpd.read_file(io.StringIO(snippet))
    except Exception as exc:
        raise ValueError(f"Invalid GeoJSON: {exc}") from exc
    if gdf.empty:
        raise ValueError("Inline GeoJSON contains no features.")
    if gdf.crs is None:
        gdf = gdf.set_crs("EPSG:4326")
    return gdf


def _from_file(path_or_name: str, catalog: Any) -> Any:
    """Load a spatial file, resolving bare names via the file connector."""
    from pathlib import Path

    import geopandas as gpd

    candidate = Path(path_or_name).expanduser()
    if candidate.exists():
        return gpd.read_file(candidate)

    if candidate.suffix.lower() == ".csv":
        raise ValueError(
            "CSV sources need explicit coordinate columns and are not "
            "supported by the renderer; convert to GeoJSON/GPKG first."
        )

    connector = catalog.get_connector("file") if catalog is not None else None
    if connector is not None:
        resolved = None
        for attr in ("resolve_path", "resolve"):
            resolver = getattr(connector, attr, None)
            if callable(resolver):
                resolved = resolver(path_or_name)
                break
        if resolved is not None:
            return gpd.read_file(resolved)

    raise ValueError(
        f"File source not found: {path_or_name!r}. Pass an existing "
        "file_path, or configure data.file_dir in ~/.geoharness/config.yaml."
    )


def _from_postgis(table: str, catalog: Any) -> Any:
    """Load a PostGIS table as a GeoDataFrame via the read-only connector."""
    if not table:
        raise ValueError("source_name (table name) is required for postgis sources.")
    connector = catalog.get_connector("postgis") if catalog is not None else None
    if connector is None:
        raise ValueError(
            "PostGIS is not configured. Set data.postgis_dsn in "
            "~/.geoharness/config.yaml."
        )
    loader = getattr(connector, "query_geodataframe", None)
    if not callable(loader):
        raise ValueError("PostGIS connector does not support GeoDataFrame loading.")
    return loader(table)


def _from_admin_kg(region: str, catalog: Any) -> Any:
    """Load a Chinese administrative boundary (name or adcode) as GeoJSON."""
    if not region:
        raise ValueError(
            "source_name (region name or adcode) is required for admin_kg sources."
        )
    connector = catalog.get_connector("admin_kg") if catalog is not None else None
    if connector is None:
        raise ValueError(
            "Administrative boundary connector unavailable; check "
            "data.admin_kg_api_url in ~/.geoharness/config.yaml."
        )
    loader = getattr(connector, "load_geodataframe", None)
    if callable(loader):
        return loader(region)

    # Fallback: fetch the raw GeoJSON and convert.
    import geopandas as gpd

    detail = connector.get_source_detail(region)
    if not detail or "adcode" not in detail:
        raise ValueError(f"Administrative region not found: {region!r}")
    data = connector._fetch_boundary(detail["adcode"], "full")
    features = data.get("features", [])
    if not features:
        raise ValueError(f"No boundary features returned for {region!r}")
    return gpd.GeoDataFrame.from_features(features, crs="EPSG:4326")


def resolve_geodataframe(
    *,
    source_type: str = "",
    file_path: str = "",
    source_name: str = "",
    geojson_text: str = "",
    catalog: Any = None,
) -> Any:
    """Resolve any supported source into a GeoDataFrame.

    Args:
        source_type: ``"file"``, ``"postgis"``, ``"admin_kg"`` or ``""``
            (inferred from the other arguments).
        file_path: Direct path to a spatial file.
        source_name: Table name, file name, region name or adcode.
        geojson_text: Inline GeoJSON document (highest precedence).
        catalog: Optional ``DataCatalog`` for connector-backed sources.

    Raises:
        ValueError: If the source cannot be resolved.
    """
    kind = (source_type or "").strip().lower()

    if geojson_text.strip():
        return _from_geojson_text(geojson_text)

    if kind not in ("", *_SOURCE_TYPES):
        raise ValueError(
            f"Unknown source_type {source_type!r}; expected one of "
            f"{', '.join(_SOURCE_TYPES)}."
        )

    if not kind:
        if file_path:
            kind = "file"
        elif source_name:
            kind = "file"
        else:
            raise ValueError(
                "No data source given. Provide geojson, file_path, or "
                "source_type + source_name."
            )

    if kind == "file":
        return _from_file(file_path or source_name, catalog)
    if kind == "postgis":
        return _from_postgis(source_name, catalog)
    return _from_admin_kg(source_name, catalog)
