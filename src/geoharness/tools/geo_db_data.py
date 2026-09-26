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

logger = logging.getLogger(__name__)

# A single database action is bounded so a hung socket cannot freeze the TUI.
_DB_TIMEOUT_SECONDS = 30.0


class GeoDBDataInput(BaseModel):
    """Input model for geo_db_data."""

    action: Literal["list", "inspect", "load"] = Field(
        default="list",
        description=(
            "list: list PostGIS tables; "
            "inspect: show a table's columns/CRS/extent; "
            "load: read rows and report shape/columns"
        ),
    )
    name: str = Field(
        default="",
        description="Table name (required for inspect and load).",
    )
    limit: int = Field(
        default=5000,
        ge=1,
        le=200_000,
        description="Maximum rows to read for the load action.",
    )


class GeoDBDataTool(BaseTool):
    """Load and inspect data held in the PostGIS database."""

    name: str = "geo_db_data"
    description: str = (
        "Load geographic data from the PostGIS database. "
        "Actions: list (tables), inspect (schema/CRS/extent), "
        "load (read rows). Use this for anything stored as a database table."
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
        lines = [f"Table: {detail.get('name', name)}"]
        lines.append(f"  Source: {detail.get('source_type', 'postgis')}")
        lines.append(f"  Format: {detail.get('format', 'PostGIS table')}")
        if detail.get("crs"):
            lines.append(f"  CRS: {detail['crs']}")
        bbox = detail.get("bbox")
        if bbox:
            lines.append(
                f"  Extent: [{bbox[0]:.4f}, {bbox[1]:.4f}, "
                f"{bbox[2]:.4f}, {bbox[3]:.4f}]"
            )
        schema = detail.get("schema") or {}
        if schema:
            lines.append(f"  Columns ({len(schema)}):")
            for col, typ in schema.items():
                lines.append(f"    - {col}: {typ}")
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
        gdf = loader(name, limit=limit)
        cols = ", ".join(str(c) for c in gdf.columns[:20])
        b = gdf.total_bounds
        return ToolResult(
            output=(
                f"Loaded table '{name}':\n"
                f"  Features: {len(gdf)}\n"
                f"  Columns ({len(gdf.columns)}): {cols}\n"
                f"  CRS: {gdf.crs}\n"
                f"  Extent: [{b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f}]"
            ),
            metadata={"feature_count": len(gdf), "crs": str(gdf.crs)},
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        """geo_db_data only reads from the database."""
        return True
