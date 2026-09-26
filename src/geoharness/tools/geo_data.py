"""GeoDataTool — unified geographic data access tool.

Provides cross-source data discovery, file loading, and inspection.
MCP tools (geo-mcp-server, postgres) provide computation, but neither offers:
- Cross-source unified discovery
- File format loading (shp/geojson/gpkg/csv)
- Chinese administrative boundary API access

This tool orchestrates DataCatalog to fill that gap.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Ceiling for a single discovery action. Bounds an unreachable backend so the
# tool reports an error instead of hanging the session.
_CATALOG_TIMEOUT_SECONDS = 60.0


class GeoDataInput(BaseModel):
    """Input model for geo_data tool."""

    action: Literal["list", "load", "inspect"] = Field(
        default="list",
        description=(
            "list: list available data sources; "
            "load: validate a source and get usage guidance; "
            "inspect: view schema/CRS/bbox of a source"
        ),
    )
    source: Literal["postgis", "file", "admin_kg", "all"] = Field(
        default="all",
        description="Data source type to query.",
    )
    name: str = Field(
        default="",
        description=(
            "Source name (table name, file name, or region name). "
            "Required for load and inspect actions."
        ),
    )
    file_path: str = Field(
        default="",
        description="File path for load action (when source='file').",
    )


class GeoDataTool(BaseTool):
    """Unified geographic data access tool."""

    name: str = "geo_data"
    description: str = (
        "Access geographic data from PostGIS, local files, and Chinese "
        "administrative boundaries. Actions: list (discover sources), "
        "load (load data for analysis), inspect (view schema/CRS/bbox)."
    )
    input_model: type[BaseModel] = GeoDataInput

    async def execute(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        """Execute the geo_data tool.

        All catalog work (TCP connects, filesystem walks, network calls) runs
        in a worker thread so the async event loop — and therefore the TUI —
        stays responsive. A blocking connect here used to freeze the whole
        session for minutes.
        """
        assert isinstance(arguments, GeoDataInput)
        args: GeoDataInput = arguments

        # Get DataCatalog from tool_metadata
        catalog = context.metadata.get("data_catalog")
        if catalog is None:
            return ToolResult(
                output="Error: DataCatalog not configured. "
                "Run `geoh init` and configure data sources.",
                is_error=True,
            )

        if args.action == "list":
            work = partial(self._list_sources, catalog, args.source)
        elif args.action == "load":
            work = partial(self._load_source, catalog, args)
        elif args.action == "inspect":
            work = partial(self._inspect_source, catalog, args.name)
        else:
            return ToolResult(
                output=f"Unknown action: {args.action}",
                is_error=True,
            )

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(work),
                timeout=_CATALOG_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return ToolResult(
                output=(
                    f"Error: data source discovery timed out after "
                    f"{_CATALOG_TIMEOUT_SECONDS:.0f}s (action={args.action}). "
                    "A configured backend (PostGIS/DAV API) may be "
                    "unreachable — check its DSN or network access."
                ),
                is_error=True,
            )
        except Exception as exc:
            logger.exception("geo_data action failed")
            return ToolResult(
                output=f"Error: {args.action} failed — {exc}", is_error=True
            )

    def _list_sources(
        self, catalog: Any, source_type: str
    ) -> ToolResult:
        """List available data sources."""
        sources = catalog.list_sources(source_type)

        if not sources:
            return ToolResult(
                output=f"No data sources found for type '{source_type}'."
            )

        lines: list[str] = []
        for src in sources:
            meta = src.metadata if hasattr(src, "metadata") else {}
            fmt = meta.get("format", "unknown")
            crs = meta.get("crs", "")
            crs_str = f" [CRS: {crs}]" if crs else ""
            lines.append(f"  - {src.name} ({src.source_type}, {fmt}){crs_str}")

        return ToolResult(
            output=f"Found {len(sources)} data source(s):\n"
            + "\n".join(lines)
        )

    def _load_source(
        self, catalog: Any, args: GeoDataInput
    ) -> ToolResult:
        """Validate a data source and return usage guidance.

        Loading/rendering is handled natively (geopandas) by
        geo_cartography; analysis is done via MCP tools.
        """
        if not args.name and not args.file_path:
            return ToolResult(
                output="Error: 'name' or 'file_path' is required for load action.",
                is_error=True,
            )

        detail = catalog.get_source_detail(args.name or args.file_path)
        if not detail:
            return ToolResult(
                output=f"Source '{args.name or args.file_path}' not found.",
                is_error=True,
            )

        # Provide usage guidance based on source type
        source_type = detail.get("source_type", "")
        path = detail.get("path", "")
        crs = detail.get("crs", "")
        fmt = detail.get("format", "")

        instructions: list[str] = []
        if source_type == "postgis":
            instructions.append(
                f"PostGIS table '{args.name}' is ready for use.\n"
                f"  - Analyze via postgres MCP tools (SQL queries)\n"
                f"  - Render via geo_cartography with "
                f"source_type='postgis', source_name='{args.name}'"
            )
        elif source_type == "file":
            instructions.append(
                f"File '{path}' ({fmt}) is ready for use.\n"
                f"  - Render via geo_cartography with "
                f"source_type='file', file_path='{path}'"
            )
            if crs == "GCJ-02":
                instructions.append(
                    "  ⚠ WARNING: This data uses GCJ-02 coordinates. "
                    "Convert to WGS84 before precise analysis."
                )
        elif source_type == "admin_kg":
            adcode = detail.get("adcode", "")
            instructions.append(
                f"Administrative boundary '{args.name}' "
                f"(adcode: {adcode}) is available.\n"
                f"  - Render via geo_cartography with "
                f"source_type='admin_kg', source_name='{args.name}'"
            )

        return ToolResult(
            output="\n".join(instructions),
            metadata={"source_detail": detail},
        )

    def _inspect_source(
        self, catalog: Any, name: str
    ) -> ToolResult:
        """Inspect a data source's schema, CRS, and bounding box."""
        if not name:
            return ToolResult(
                output="Error: 'name' is required for inspect action.",
                is_error=True,
            )

        detail = catalog.get_source_detail(name)
        if not detail:
            return ToolResult(
                output=f"Source '{name}' not found.",
                is_error=True,
            )

        lines: list[str] = [f"Source: {name}"]
        lines.append(f"  Type: {detail.get('source_type', 'unknown')}")
        lines.append(f"  Format: {detail.get('format', 'unknown')}")

        crs = detail.get("crs", "")
        if crs:
            lines.append(f"  CRS: {crs}")

        bbox = detail.get("bbox")
        if bbox:
            lines.append(
                f"  BBox: [{bbox[0]:.4f}, {bbox[1]:.4f}, "
                f"{bbox[2]:.4f}, {bbox[3]:.4f}]"
            )

        schema = detail.get("schema")
        if schema:
            lines.append("  Columns:")
            for col_name, col_type in schema.items():
                lines.append(f"    - {col_name}: {col_type}")

        return ToolResult(output="\n".join(lines))

    def is_read_only(self, arguments: BaseModel) -> bool:
        """geo_data is read-only — it doesn't modify data."""
        return True
