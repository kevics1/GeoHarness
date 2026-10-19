"""GeoDBDataTool — load data from the PostGIS spatial database.

Dedicated to the *database* source. It talks only to the PostGIS connector,
never to local files or the network admin-boundary API — that separation is
the whole point: a database query can never be delayed by an unrelated
filesystem walk or an unreachable HTTP endpoint.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from geoharness.data.description import describe_source

logger = logging.getLogger(__name__)

# A single database action is bounded so a hung socket cannot freeze the TUI.
_DB_TIMEOUT_SECONDS = 30.0


class GeoDBDataInput(BaseModel):
    """Input model for geo_db_data."""

    action: Literal["list", "inspect", "load"] = Field(
        default="list",
        description=(
            '默认list列出PostGIS表；inspect查看字段/CRS/范围；load读取并返回行数/字段。'
        ),
    )
    name: str = Field(
        default="",
        description='表名，inspect/load时必填，默认空。',
    )
    limit: int = Field(
        default=5000,
        ge=1,
        le=200_000,
        description='默认5000，读取行数上限，范围1..200000。',
    )


class GeoDBDataTool(BaseTool):
    """Load and inspect data held in the PostGIS database."""

    name: str = "geo_db_data"
    description: str = (
        (
        '读取配置的PostGIS数据库。list发现表，inspect检查字段/'
        'CRS/范围，load读取并返回行数与字段；本地文件使用geo_vect'
        'or_data。'
    )
    )
    input_model: type[BaseModel] = GeoDBDataInput

    async def execute(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        assert isinstance(arguments, GeoDBDataInput)
        args: GeoDBDataInput = arguments

        catalog = context.metadata.get("data_catalog")
        if catalog is None:
            return ToolResult(
                output="Error: DataCatalog not configured. Run `geoh init`.",
                is_error=True,
            )

        try:
            connector = catalog.get_connector("postgis")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                output=f"Error: PostGIS connector unavailable — {exc}",
                is_error=True,
            )
        if connector is None:
            return ToolResult(
                output=(
                    "Error: PostGIS is not configured. Set data.postgis_dsn in "
                    "~/.geoharness/config.yaml or GEOH_POSTGIS_DSN in .env."
                ),
                is_error=True,
            )

        if args.action == "list":
            work = partial(self._list, connector)
        elif args.action == "inspect":
            work = partial(self._inspect, connector, args.name)
        elif args.action == "load":
            work = partial(self._load, connector, args.name, args.limit)
        else:
            return ToolResult(output=f"Unknown action: {args.action}", is_error=True)

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(work), timeout=_DB_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            self._reset(connector)
            return ToolResult(
                output=(
                    f"Error: PostGIS did not respond within "
                    f"{_DB_TIMEOUT_SECONDS:.0f}s (action={args.action}). "
                    "The database may be unreachable; check data.postgis_dsn "
                    "and that the server is running."
                ),
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("geo_db_data action failed")
            return ToolResult(
                output=f"Error: {args.action} failed — {exc}", is_error=True
            )

    @staticmethod
    def _reset(connector: Any) -> None:
        reset = getattr(connector, "reset", None)
        if callable(reset):
            try:
                reset()
            except Exception:  # noqa: BLE001
                pass

    def _list(self, connector: Any) -> ToolResult:
        sources = connector.list_sources()
        if not sources:
            return ToolResult(output="No PostGIS tables found.")
        lines = []
        for src in sources:
            meta = getattr(src, "metadata", {}) or {}
            crs = meta.get("crs", "")
            suffix = f" [CRS: {crs}]" if crs else ""
            lines.append(f"  - {src.name} ({meta.get('format', 'table')}){suffix}")
        return ToolResult(
            output=f"Found {len(sources)} PostGIS table(s):\n" + "\n".join(lines)
        )

    def _inspect(self, connector: Any, name: str) -> ToolResult:
        if not name:
            return ToolResult(
                output="Error: 'name' is required for inspect.", is_error=True
            )
        detail = connector.get_source_detail(name)
        if not detail:
            return ToolResult(
                output=f"Table '{name}' not found in PostGIS.", is_error=True
            )
        lines = [f"Table: {detail.get('name', name)}", describe_source(detail, kind="postgis")]
        return ToolResult(output="\n".join(lines), metadata={"source_detail": detail})

    def _load(self, connector: Any, name: str, limit: int) -> ToolResult:
        if not name:
            return ToolResult(
                output="Error: 'name' is required for load.", is_error=True
            )
        loader = getattr(connector, "query_geodataframe", None)
        if not callable(loader):
            return ToolResult(
                output="Error: PostGIS connector does not support data loading.",
                is_error=True,
            )
        try:
            gdf = loader(name, limit=limit)
        except ValueError as exc:
            # Pure-attribute tables (no geometry column) are legitimate data —
            # e.g. admin_attributes carries population/GDP for joins. Report
            # them usefully instead of failing the whole call.
            if "no geometry column" in str(exc):
                detail = connector.get_source_detail(name) or {}
                detail.setdefault("name", name)
                return ToolResult(
                    output=(
                        f"Table '{name}' is an attribute table (no geometry) — "
                        f"not directly mappable, but usable for joins/analysis.\n"
                        f"{describe_source(detail, kind='postgis')}\n"
                        f"  Typical use: join on region_id with a spatial table "
                        f"(province/city/county), or query via postgres MCP "
                        f"tools for aggregates."
                    ),
                    metadata={"attribute_only": True, "table": name, "source_detail": detail},
                )
            return ToolResult(output=f"Error: {exc}", is_error=True)
        b = gdf.total_bounds
        detail = {"name": name, "source_type": "postgis", "format": "PostGIS table",
            "crs": str(gdf.crs) if gdf.crs is not None else None,
            "bbox": b.tolist() if len(gdf) else None, "feature_count": len(gdf),
            "row_count": len(gdf), "row_limit": limit,
            "schema": {str(col): str(dtype) for col, dtype in gdf.dtypes.items()},
            "null_counts": {str(col): int(series.isna().sum()) for col, series in gdf.items()},
            "geometry_type": ", ".join(str(t) for t in gdf.geom_type.dropna().unique()[:8])}
        return ToolResult(
            output=(
                f"Loaded table '{name}':\n"
                f"{describe_source(detail, kind='postgis')}\n"
                "Extent and row count describe loaded rows, not the full table."
            ),
            metadata={"feature_count": len(gdf), "crs": str(gdf.crs), "source_detail": detail},
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        """geo_db_data only reads from the database."""
        return True
