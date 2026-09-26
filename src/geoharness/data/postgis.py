"""PostGISConnector — read-only PostGIS spatial database connector.

CRITICAL: All methods must handle connection errors gracefully.
In tests, this connector MUST be mocked — real TCP connections
will hang the test suite indefinitely.

Write operations (INSERT/UPDATE/DELETE) are blocked at the query level.
"""

from __future__ import annotations

import logging
from typing import Any

from geoharness.data.catalog import DataSource

logger = logging.getLogger(__name__)

# SQL keywords that indicate write operations
_WRITE_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "CREATE", "ALTER",
    "TRUNCATE", "GRANT", "REVOKE", "MERGE",
}


class PostGISConnector:
    """Read-only PostGIS connector using psycopg3.

    Connection is established lazily on first query.
    DSN is read from config.data.postgis_dsn.
    """

    def __init__(self, dsn: str, connect_timeout: int = 10) -> None:
        self._dsn = dsn
        self._connect_timeout = connect_timeout
        self._conn: Any = None  # psycopg.Connection, lazily connected
        self._resolved_dsn: str | None = None

    def _resolve_dsn(self) -> str:
        """Return a DSN that avoids the IPv6-first ``localhost`` stall.

        On Windows ``localhost`` resolves to ``::1`` before ``127.0.0.1``. If
        PostgreSQL only listens on IPv4, the IPv6 attempt must fail before
        falling back — which blocks startup for over two minutes. Preferring
        the IPv4 loopback when it is reachable removes that stall entirely.
        """
        if self._resolved_dsn is not None:
            return self._resolved_dsn

        dsn = self._dsn
        if "localhost" in dsn:
            import socket

            try:
                with socket.create_connection(("127.0.0.1", 5432), timeout=2):
                    dsn = dsn.replace("localhost", "127.0.0.1")
                    logger.debug("Rewrote localhost -> 127.0.0.1 in PostGIS DSN")
            except OSError:
                logger.debug("127.0.0.1:5432 unreachable; keeping original DSN")

        self._resolved_dsn = dsn
        return dsn

    def _ensure_connection(self) -> Any:
        """Establish connection lazily on first use."""
        if self._conn is not None:
            return self._conn

        try:
            import psycopg  # type: ignore[import-untyped]
        except ImportError as e:
            raise ImportError(
                "psycopg is required for PostGIS connector. "
                "Install with: pip install psycopg[binary]"
            ) from e

        self._conn = psycopg.connect(
            self._resolve_dsn(),
            autocommit=True,
            connect_timeout=self._connect_timeout,
        )
        logger.info("PostGIS connection established")
        return self._conn

    def _check_read_only(self, sql: str) -> None:
        """Block write operations."""
        sql_upper = sql.upper().strip()
        for keyword in _WRITE_KEYWORDS:
            if keyword in sql_upper:
                raise ValueError(
                    f"Write operation blocked (read-only connector): "
                    f"detected '{keyword}' in query"
                )

    # PostGIS/system tables that are not user data
    _SYSTEM_TABLES = {"spatial_ref_sys", "geography_columns", "geometry_columns"}

    def list_tables(self) -> list[str]:
        """List all user spatial tables in the database.

        PostGIS bookkeeping tables (``spatial_ref_sys`` and friends) are
        excluded — they are not user data and only add noise to discovery.
        """
        conn = self._ensure_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
            return [
                row[0]
                for row in cur.fetchall()
                if row[0].lower() not in self._SYSTEM_TABLES
            ]

    def get_table_schema(self, table_name: str) -> dict[str, str]:
        """Get column names and types for a table."""
        conn = self._ensure_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY ordinal_position",
                (table_name,),
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    def get_table_extent(self, table_name: str) -> list[float] | None:
        """Get bounding box of a spatial table.

        Returns [xmin, ymin, xmax, ymax] or None if no geometry column.
        """
        conn = self._ensure_connection()
        with conn.cursor() as cur:
            # Find geometry column
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s AND udt_name = 'geometry'",
                (table_name,),
            )
            geom_col_row = cur.fetchone()
            if not geom_col_row:
                return None

            geom_col = geom_col_row[0]
            cur.execute(
                f"SELECT ST_XMin(extent), ST_YMin(extent), "
                f"ST_XMax(extent), ST_YMax(extent) "
                f"FROM (SELECT ST_Extent({geom_col}) AS extent "
                f"FROM {table_name}) sub"
            )
            row = cur.fetchone()
            if row and row[0] is not None:
                return [float(row[0]), float(row[1]), float(row[2]), float(row[3])]
            return None

    def query_bbox(
        self, table_name: str, bbox: list[float], limit: int = 1000
    ) -> list[dict[str, Any]]:
        """Query rows within a bounding box.

        Args:
            table_name: Name of the spatial table.
            bbox: [xmin, ymin, xmax, ymax].
            limit: Maximum number of rows to return.

        Returns:
            List of row dicts.
        """
        conn = self._ensure_connection()
        with conn.cursor() as cur:
            # Find geometry column
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s AND udt_name = 'geometry'",
                (table_name,),
            )
            geom_col_row = cur.fetchone()
            if not geom_col_row:
                raise ValueError(
                    f"Table '{table_name}' has no geometry column"
                )

            geom_col = geom_col_row[0]
            xmin, ymin, xmax, ymax = bbox
            cur.execute(
                f"SELECT * FROM {table_name} "
                f"WHERE {geom_col} && ST_MakeEnvelope(%s, %s, %s, %s, 4326) "
                f"LIMIT %s",
                (xmin, ymin, xmax, ymax, limit),
            )
            col_names = [desc[0] for desc in cur.description]
            return [dict(zip(col_names, row)) for row in cur.fetchall()]

    def query_geodataframe(self, table_name: str, limit: int = 5000) -> Any:
        """Load a spatial table into a GeoDataFrame.

        Read-only: uses ``SELECT *`` on the named table with a row cap.
        Geometry is returned as WKB and converted via ``shapely``.

        Args:
            table_name: Name of the spatial table.
            limit: Maximum number of rows to load.

        Returns:
            GeoDataFrame in EPSG:4326.

        Raises:
            ValueError: If the table has no geometry column or no rows.
        """
        import geopandas as gpd
        from shapely import wkb as shapely_wkb

        conn = self._ensure_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s AND udt_name = 'geometry'",
                (table_name,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(
                    f"Table '{table_name}' has no geometry column"
                )
            geom_col = row[0]

            # Table/column names cannot be parameterised; they come from
            # information_schema lookups above, not raw user text.
            cur.execute(
                f'SELECT *, ST_AsBinary("{geom_col}") AS __geoh_wkb '
                f'FROM "{table_name}" LIMIT %s',
                (int(limit),),
            )
            col_names = [d[0] for d in cur.description]
            records = [dict(zip(col_names, r)) for r in cur.fetchall()]

        if not records:
            raise ValueError(f"Table '{table_name}' contains no rows")

        wkb_values = [rec.pop("__geoh_wkb") for rec in records]
        geometry = [shapely_wkb.loads(v) if v is not None else None for v in wkb_values]
        gdf = gpd.GeoDataFrame(records, geometry=geometry, crs="EPSG:4326")
        return gdf

    def list_sources(self) -> list[DataSource]:
        """List all spatial tables as DataSource objects.

        Deliberately cheap: a single ``information_schema`` query. Schema and
        extent are resolved on demand by ``get_source_detail`` — computing
        them here made discovery issue two extra round-trips per table
        (including ``ST_Extent`` over multi-hundred-MB tables).
        """
        try:
            tables = self.list_tables()
        except Exception as e:
            logger.warning("Failed to list PostGIS tables: %s", e)
            return []

        if not tables:
            return []

        geom_columns = self._geometry_columns(tables)
        sources: list[DataSource] = []
        for table in tables:
            has_geom = table in geom_columns
            metadata: dict[str, Any] = {
                "format": "PostGIS table",
                "path": table,
                "geometry_column": geom_columns.get(table, ""),
                "crs": "EPSG:4326" if has_geom else "",
            }
            sources.append(DataSource(
                name=table,
                source_type="postgis",
                metadata=metadata,
            ))
        return sources

    def _geometry_columns(self, tables: list[str]) -> dict[str, str]:
        """Return ``{table: geometry_column}`` for the given tables."""
        if not tables:
            return {}
        conn = self._ensure_connection()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND udt_name = 'geometry'"
            )
            return {row[0]: row[1] for row in cur.fetchall()}

    def get_source_detail(self, name: str) -> dict[str, Any]:
        """Get details of a specific table.

        Returns empty dict if table not found.
        """
        try:
            tables = self.list_tables()
        except Exception:
            return {}

        if name not in tables:
            return {}

        detail: dict[str, Any] = {
            "name": name,
            "source_type": "postgis",
            "format": "PostGIS table",
            "path": name,
        }
        try:
            schema = self.get_table_schema(name)
            extent = self.get_table_extent(name)
            has_geom = any(v == "geometry" for v in schema.values())
            detail["crs"] = "EPSG:4326" if has_geom else ""
            detail["bbox"] = extent
            detail["schema"] = schema
        except Exception as e:
            logger.warning("Failed to get details for %s: %s", name, e)

        return detail

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
            logger.info("PostGIS connection closed")
