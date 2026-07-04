"""GeoDataTool — unified geographic data access tool.

Provides cross-source data discovery, file loading, and inspection.
MCP tools (postgres, qgis) provide computation, but neither offers:
- Cross-source unified discovery
- File format loading (shp/geojson/gpkg/csv)
- Chinese administrative boundary API access

This tool orchestrates DataCatalog to fill that gap.
"""

from __future__ import annotations

from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field


class GeoDataInput(BaseModel):
    """Input model for geo_data tool."""

    action: Literal["list", "load", "inspect"] = Field(
        default="list",
        description=(
            "list: list available data sources; "
            "load: load data into QGIS layer; "
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
        """Execute the geo_data tool."""
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
            return self._list_sources(catalog, args.source)
        elif args.action == "load":
            return self._load_source(catalog, args)
        elif args.action == "inspect":
            return self._inspect_source(catalog, args.name)
        else:
            return ToolResult(
                output=f"Unknown action: {args.action}",
                is_error=True,
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
        """Load a data source for analysis.

        This tool provides instructions for loading — actual QGIS layer
        creation is done via qgis MCP tools (add_vector_layer, etc.).
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

        # Provide loading instructions based on source type
        source_type = detail.get("source_type", "")
        path = detail.get("path", "")
        crs = detail.get("crs", "")
        fmt = detail.get("format", "")

        instructions: list[str] = []
        if source_type == "postgis":
            instructions.append(
                f"To load PostGIS table '{args.name}':\n"
                f"  Use mcp__qgis__add_vector_layer with "
                f"provider='postgres' and connection string."
            )
        elif source_type == "file":
            instructions.append(
                f"To load {fmt} file '{path}':\n"
                f"  Use mcp__qgis__add_vector_layer with path='{path}'."
            )
            if crs == "GCJ-02":
                instructions.append(
                    "  ⚠ WARNING: This data uses GCJ-02 coordinates. "
                    "Use geo_transform_crs to convert to WGS84 before analysis."
                )
        elif source_type == "admin_kg":
            adcode = detail.get("adcode", "")
            instructions.append(
                f"To load administrative boundary '{args.name}' "
                f"(adcode: {adcode}):\n"
                f"  Use mcp__qgis__add_vector_layer with the GeoJSON URL."
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
