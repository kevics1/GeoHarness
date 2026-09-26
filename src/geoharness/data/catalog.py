"""DataCatalog — connector registry for the type-specific data tools.

Each data type owns its connector *and* its tool:

- ``postgis``  → ``geo_db_data``      (database tables)
- ``file``     → ``geo_vector_data``  (local vector files)
- ``raster``   → ``geo_raster_data``  (local raster files)
- ``admin_kg`` → administrative-boundary helper used by cartography

A tool talks to exactly one connector, so a slow database can never delay a
local file lookup. That cross-source coupling was precisely what made the old
unified ``geo_data`` tool report "connector did not respond within 15s" for a
plain workspace shapefile.

Connectors are lazily initialized — ``__init__`` does NOT make TCP
connections. This is critical for test safety (real connections hang tests).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from geoharness.config.settings import GeoConfig

logger = logging.getLogger(__name__)


class ConnectorTimeout(Exception):
    """Raised when a connector exceeds its own per-call budget."""


@dataclass(frozen=True)
class DataSource:
    """Description of an available data source."""

    name: str
    source_type: str  # "postgis", "file", "raster", "admin_kg"
    metadata: dict[str, Any] = field(default_factory=dict)


class DataConnector(Protocol):
    """Protocol for data connectors."""

    def list_sources(self) -> list[DataSource]: ...

    def get_source_detail(self, name: str) -> dict[str, Any]: ...


class DataCatalog:
    """Registry of type-specific data connectors."""

    def __init__(self, config: GeoConfig, workspace_dir: str = "") -> None:
        self._config = config
        self._workspace_dir = workspace_dir
        self._connectors: dict[str, DataConnector] = {}
        self._initialized = False

    def _ensure_initialized(self) -> None:
        """Lazily initialize connectors on first access."""
        if self._initialized:
            return

        # Import here to avoid TCP connections at module load time
        from geoharness.data.admin_kg import AdminKGConnector
        from geoharness.data.file_loader import FileLoader
        from geoharness.data.postgis import PostGISConnector
        from geoharness.data.raster_loader import RasterLoader

        if self._config.data.postgis_dsn:
            try:
                self._connectors["postgis"] = PostGISConnector(
                    dsn=self._config.data.postgis_dsn
                )
            except Exception as e:
                logger.warning("PostGIS connector init failed: %s", e)

        # The file and raster connectors are ALWAYS registered: when
        # data.file_dir is unset they fall back to the working directory, so
        # data sitting in the workspace (e.g. 数据/*.shp) is discoverable
        # without extra config.
        file_dir = self._config.data.file_dir or self._workspace_dir or "."
        self._connectors["file"] = FileLoader(file_dir=file_dir)
        self._connectors["raster"] = RasterLoader(file_dir=file_dir)

        self._connectors["admin_kg"] = AdminKGConnector(
            api_url=self._config.data.admin_kg_api_url,
            cache_dir=self._config.data.admin_kg_cache_dir,
        )

        self._initialized = True
        logger.info(
            "DataCatalog initialized with: %s", list(self._connectors.keys())
        )

    def list_sources(self, source_type: str = "all") -> list[DataSource]:
        """List available data sources.

        Args:
            source_type: Filter by type ("postgis", "file", "raster",
                "admin_kg", or "all").

        Returns:
            List of DataSource descriptors.
        """
        self._ensure_initialized()
        results: list[DataSource] = []

        if source_type == "all":
            for name, connector in self._connectors.items():
                try:
                    results.extend(connector.list_sources())
                except Exception as e:
                    logger.warning("Connector %s list_sources failed: %s", name, e)
        else:
            connector = self._connectors.get(source_type)
            if connector:
                try:
                    results.extend(connector.list_sources())
                except Exception as e:
                    logger.warning(
                        "Connector %s list_sources failed: %s", source_type, e
                    )

        return results

    def get_connector(self, name: str) -> DataConnector | None:
        """Get a specific connector by name.

        Args:
            name: Connector name ("postgis", "file", "raster", "admin_kg").

        Returns:
            The connector instance, or None if not configured.
        """
        self._ensure_initialized()
        return self._connectors.get(name)

    def get_raster_connector(self) -> Any | None:
        """Return the raster connector, or ``None`` if unavailable.

        Raster support is optional (needs ``rasterio``); an import failure is
        reported as "no raster source" rather than crashing the tool.
        """
        try:
            self._ensure_initialized()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Raster connector unavailable: %s", exc)
            return None
        return self._connectors.get("raster")

    @property
    def connector_names(self) -> list[str]:
        """Return list of configured connector names."""
        self._ensure_initialized()
        return list(self._connectors.keys())

    def register_connector(self, name: str, connector: DataConnector) -> None:
        """Register a custom connector (for testing or extensibility).

        Bypasses lazy initialization — connector is added directly.
        """
        self._connectors[name] = connector
        self._initialized = True


def build_data_catalog(config: GeoConfig, workspace_dir: str = "") -> DataCatalog:
    """Factory function to create a DataCatalog from config.

    Args:
        config: GeoHarness configuration.
        workspace_dir: Working directory used as the file/raster-source
            fallback when ``config.data.file_dir`` is unset.

    This does NOT initialize connectors — they are lazily created on first
    access. Safe to call in tests without mocking (as long as no connection
    is opened).
    """
    return DataCatalog(config, workspace_dir=workspace_dir)
