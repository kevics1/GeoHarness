"""DataCatalog — unified data catalog with pluggable connectors.

Injected into tool_metadata["data_catalog"] for geo_data tool access.
Connectors are lazily initialized to avoid TCP connections during construction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from geoharness.config.settings import GeoConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataSource:
    """Description of an available data source."""

    name: str
    source_type: str  # "postgis", "file", "admin_kg"
    metadata: dict[str, Any] = field(default_factory=dict)


class DataConnector(Protocol):
    """Protocol for data connectors."""

    def list_sources(self) -> list[DataSource]: ...

    def get_source_detail(self, name: str) -> dict[str, Any]: ...


class DataCatalog:
    """Unified data catalog aggregating multiple connectors.

    Connectors are lazily initialized — __init__ does NOT make TCP connections.
    This is critical for test safety (known pitfall: real connections hang tests).
    """

    def __init__(self, config: GeoConfig) -> None:
        self._config = config
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

        if self._config.data.postgis_dsn:
            try:
                self._connectors["postgis"] = PostGISConnector(
                    dsn=self._config.data.postgis_dsn
                )
            except Exception as e:
                logger.warning("PostGIS connector init failed: %s", e)

        if self._config.data.file_dir:
            self._connectors["file"] = FileLoader(
                file_dir=self._config.data.file_dir
            )

        self._connectors["admin_kg"] = AdminKGConnector(
            api_url=self._config.data.admin_kg_api_url,
            cache_dir=self._config.data.admin_kg_cache_dir,
        )

        self._initialized = True
        logger.info(
            "DataCatalog initialized with: %s", list(self._connectors.keys())
        )

    def list_sources(
        self, source_type: str = "all"
    ) -> list[DataSource]:
        """List available data sources.

        Args:
            source_type: Filter by type ("postgis", "file", "admin_kg", or "all").

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

    def get_source_detail(self, name: str) -> dict[str, Any] | None:
        """Get detailed information about a specific data source.

        Args:
            name: Source name (e.g., table name, file name).

        Returns:
            Dictionary with source details, or None if not found.
        """
        self._ensure_initialized()
        for connector in self._connectors.values():
            try:
                detail = connector.get_source_detail(name)
                if detail:
                    return detail
            except Exception as e:
                logger.warning("get_source_detail(%s) failed: %s", name, e)
        return None

    def get_connector(self, name: str) -> DataConnector | None:
        """Get a specific connector by name.

        Args:
            name: Connector name ("postgis", "file", "admin_kg").

        Returns:
            The connector instance, or None if not configured.
        """
        self._ensure_initialized()
        return self._connectors.get(name)

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


def build_data_catalog(config: GeoConfig) -> DataCatalog:
    """Factory function to create a DataCatalog from config.

    This does NOT initialize connectors — they are lazily created on first access.
    Safe to call in tests without mocking.
    """
    return DataCatalog(config)
