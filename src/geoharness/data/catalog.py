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

# Per-connector wall-clock budget for a broad lookup. One blocked backend
# (DNS/connect/TLS) must not consume the caller's whole action budget.
_PER_CONNECTOR_SECONDS = 15.0


class ConnectorTimeout(Exception):
    """Raised when a connector exceeds its per-call budget."""


# Backwards-compatible private alias.
_ConnectorTimeout = ConnectorTimeout


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

        if self._config.data.postgis_dsn:
            try:
                self._connectors["postgis"] = PostGISConnector(
                    dsn=self._config.data.postgis_dsn
                )
            except Exception as e:
                logger.warning("PostGIS connector init failed: %s", e)

        # The file connector is ALWAYS registered: when data.file_dir is unset
        # it falls back to the working directory, so files sitting in the
        # workspace (e.g. 数据/*.shp) are discoverable without extra config.
        file_dir = self._config.data.file_dir or self._workspace_dir or "."
        self._connectors["file"] = FileLoader(file_dir=file_dir)

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

    def get_source_detail(
        self, name: str, source_type: str = ""
    ) -> dict[str, Any] | None:
        """Get detailed information about a specific data source.

        Args:
            name: Source name (e.g., table name, file name).
            source_type: Optional connector filter ("postgis", "file",
                "admin_kg"). When given, ONLY that connector is consulted —
                essential because a network-backed connector (admin_kg) can
                take seconds to fail, and querying it for a local file is
                both unnecessary and slow.

        Returns:
            Dictionary with source details, or None if not found.
        """
        self._ensure_initialized()

        if source_type and source_type != "all":
            connector = self._connectors.get(source_type)
            if connector is None:
                return None
            try:
                return self._call_with_budget(connector, name) or None
            except ConnectorTimeout:
                # Propagate: callers distinguish "backend slow" from "absent".
                raise
            except Exception as e:
                logger.warning(
                    "get_source_detail(%s) on %s failed: %s", name, source_type, e
                )
                return None

        # Broad lookup: try local connectors first so a slow/network-backed
        # connector (admin_kg) is only reached when nothing local matches.
        order = [n for n in ("file", "postgis") if n in self._connectors]
        order += [n for n in self._connectors if n not in order]

        last_error: Exception | None = None
        slow: list[str] = []
        for name_ in order:
            connector = self._connectors[name_]
            try:
                detail = self._call_with_budget(connector, name)
                if detail:
                    return detail
            except _ConnectorTimeout:
                slow.append(name_)
                logger.warning("get_source_detail(%s) via %s timed out", name, name_)
            except Exception as e:
                last_error = e
                logger.warning("get_source_detail(%s) via %s failed: %s", name, name_, e)

        # A slow backend is a different problem from a missing source: say so
        # rather than silently reporting "not found".
        if slow:
            raise _ConnectorTimeout(
                "connector(s) did not respond in time: " + ", ".join(slow)
            )
        if last_error is not None:
            raise last_error
        return None

    def _call_with_budget(
        self, connector: DataConnector, name: str, budget: float | None = None
    ) -> dict[str, Any] | None:
        """Call ``connector.get_source_detail`` under a hard wall-clock budget.

        Connectors are synchronous and may block on DNS/connect/TLS. A daemon
        thread plus ``join(timeout)`` lets the caller move on without ever
        waiting for a hung worker — and without the process being kept alive
        by a stuck thread at interpreter shutdown.
        """
        import threading

        # Resolve the budget at call time (not as a default argument) so the
        # module constant stays tunable, including in tests.
        limit = _PER_CONNECTOR_SECONDS if budget is None else budget

        box: dict[str, Any] = {}

        def _run() -> None:
            try:
                box["detail"] = connector.get_source_detail(name)
            except BaseException as exc:  # noqa: BLE001 — re-raised in caller
                box["error"] = exc

        worker = threading.Thread(
            target=_run, name=f"geoh-detail-{name}", daemon=True
        )
        worker.start()
        worker.join(timeout=limit)

        if worker.is_alive():
            # The worker is wedged inside the connector (a hung socket query).
            # Drop any cached connection so the *next* call reconnects rather
            # than reusing a poisoned handle and hanging again.
            self._reset_connector(connector)
            raise ConnectorTimeout(
                f"connector did not respond within {limit:.0f}s"
            )
        if "error" in box:
            raise box["error"]
        return box.get("detail")

    @staticmethod
    def _reset_connector(connector: DataConnector) -> None:
        """Ask a connector to drop a possibly-wedged cached connection."""
        reset = getattr(connector, "reset", None)
        if callable(reset):
            try:
                reset()
            except Exception as exc:  # noqa: BLE001
                logger.debug("connector reset failed: %s", exc)

    def resolve_source_detail(
        self, name: str, source_type: str = ""
    ) -> tuple[dict[str, Any] | None, str]:
        """Resolve a source, falling back to local files on backend timeout.

        Returns ``(detail, warning)``. Exactly one of the two is meaningful:
        a hit gives ``(detail, "")``; a miss gives ``(None, "")``; a backend
        timeout that was covered by a local fallback gives
        ``(detail, "warning text")``.

        Rationale: a network backend (PostGIS/DAV) being unreachable must not
        hide a perfectly good local file — and must never cost the user more
        than the per-connector budget.
        """
        try:
            return self.get_source_detail(name, source_type), ""
        except ConnectorTimeout as exc:
            if source_type == "file":
                raise
            if source_type in ("", "all"):
                # Broad lookup raises only after the local file connector was
                # already consulted; do not query it a second time.
                raise
            # A specific network backend timed out: try local files before
            # giving up, since the source may simply be a workspace shapefile.
            try:
                detail = self.get_source_detail(name, "file")
            except Exception:
                detail = None
            if detail:
                return detail, (
                    f"Backend '{source_type}' timed out ({exc}); "
                    "resolved from local files instead."
                )
            raise

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


def build_data_catalog(config: GeoConfig, workspace_dir: str = "") -> DataCatalog:
    """Factory function to create a DataCatalog from config.

    Args:
        config: GeoHarness configuration.
        workspace_dir: Working directory used as the file-source fallback when
            ``config.data.file_dir`` is unset.

    This does NOT initialize connectors — they are lazily created on first access.
    Safe to call in tests without mocking (as long as no connection is opened).
    """
    return DataCatalog(config, workspace_dir=workspace_dir)
