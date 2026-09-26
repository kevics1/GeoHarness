"""GeoVectorDataTool — load local vector data (shapefile/GeoJSON/GPKG/CSV).

Dedicated to the *vector file* source. It resolves and reads local files via
the FileLoader only — it never queries the database or the boundary API, so a
vector lookup is a pure filesystem operation and cannot time out on a socket.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Local file work is I/O-bound but never network-bound; a generous cap still
# prevents a pathological filesystem from wedging the session.
_VECTOR_TIMEOUT_SECONDS = 60.0


class GeoVectorDataInput(BaseModel):
    """Input model for geo_vector_data."""

    action: Literal["list", "inspect", "load"] = Field(
        default="list",
        description=(
            "list: list local vector files; "
            "inspect: show geometry type/CRS/bbox/columns; "
            "load: read features and report count/columns"
        ),
    )
    name: str = Field(
        default="",
        description=(
            "Vector file name with or without extension (e.g. '城际公路' or "
            "'城际公路.shp'). Required for inspect and load."
        ),
    )
    file_path: str = Field(
        default="",
        description="Explicit path to a vector file; overrides 'name'.",
    )

    def target(self) -> str:
        return (self.file_path or self.name).strip()


class GeoVectorDataTool(BaseTool):
    """Load and inspect local vector files."""

    name: str = "geo_vector_data"
    description: str = (
        "Load local vector data (Shapefile/GeoJSON/GPKG/CSV). "
        "Actions: list (vector files), inspect (CRS/bbox/columns), "
        "load (read features). Use this for local vector files, e.g. "
        "shapefiles in a workspace folder."
    )
    input_model: type[BaseModel] = GeoVectorDataInput

    async def execute(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        assert isinstance(arguments, GeoVectorDataInput)
        args: GeoVectorDataInput = arguments

        catalog = context.metadata.get("data_catalog")
        if catalog is None:
            return ToolResult(
                output="Error: DataCatalog not configured. Run `geoh init`.",
                is_error=True,
            )

        try:
            loader = catalog.get_connector("file")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                output=f"Error: file connector unavailable — {exc}", is_error=True
            )
        if loader is None:
            return ToolResult(
                output=(
                    "Error: no vector file source configured. Set data.file_dir "
                    "in ~/.geoharness/config.yaml, or run from the folder that "
                    "holds your vector files."
                ),
                is_error=True,
            )

        if args.action == "list":
            work = partial(self._list, loader)
        elif args.action == "inspect":
            work = partial(self._inspect, loader, args.target())
        elif args.action == "load":
            work = partial(self._load, loader, args.target())
        else:
            return ToolResult(output=f"Unknown action: {args.action}", is_error=True)

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(work), timeout=_VECTOR_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            return ToolResult(
                output=(
                    f"Error: vector lookup timed out after "
                    f"{_VECTOR_TIMEOUT_SECONDS:.0f}s (action={args.action}). "
                    "The file may be very large or the folder unusually slow."
                ),
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("geo_vector_data action failed")
            return ToolResult(
                output=f"Error: {args.action} failed — {exc}", is_error=True
            )

    def _list(self, loader: Any) -> ToolResult:
        sources = loader.list_sources()
        if not sources:
            return ToolResult(output="No local vector files found.")
        lines = []
        for src in sources:
            meta = getattr(src, "metadata", {}) or {}
            lines.append(f"  - {src.name} ({meta.get('format', 'file')})")
        return ToolResult(
            output=f"Found {len(sources)} vector file(s):\n" + "\n".join(lines)
        )

    def _inspect(self, loader: Any, name: str) -> ToolResult:
        if not name:
            return ToolResult(
                output="Error: 'name' or 'file_path' is required for inspect.",
                is_error=True,
            )
        detail = loader.get_source_detail(name)
        if not detail:
            return ToolResult(
                output=f"Vector file '{name}' not found.", is_error=True
            )
        lines = [f"File: {detail.get('name', name)}"]
        lines.append(f"  Path: {detail.get('path', '')}")
        lines.append(f"  Format: {detail.get('format', '')}")
        if detail.get("crs"):
            lines.append(f"  CRS: {detail['crs']}")
        if detail.get("geometry_type"):
            lines.append(f"  Geometry: {detail['geometry_type']}")
        bbox = detail.get("bbox")
        if bbox:
            lines.append(
                f"  BBox: [{bbox[0]:.4f}, {bbox[1]:.4f}, "
                f"{bbox[2]:.4f}, {bbox[3]:.4f}]"
            )
        schema = detail.get("schema") or {}
        if schema:
            lines.append(f"  Columns ({len(schema)}):")
            for col, typ in schema.items():
                lines.append(f"    - {col}: {typ}")
        return ToolResult(output="\n".join(lines), metadata={"source_detail": detail})

    def _load(self, loader: Any, name: str) -> ToolResult:
        if not name:
            return ToolResult(
                output="Error: 'name' or 'file_path' is required for load.",
                is_error=True,
            )
        gdf = loader.load_geodataframe(name)
        cols = ", ".join(str(c) for c in gdf.columns[:20])
        b = gdf.total_bounds
        return ToolResult(
            output=(
                f"Loaded vector file '{name}':\n"
                f"  Features: {len(gdf)}\n"
                f"  Columns ({len(gdf.columns)}): {cols}\n"
                f"  CRS: {gdf.crs}\n"
                f"  Extent: [{b[0]:.4f}, {b[1]:.4f}, {b[2]:.4f}, {b[3]:.4f}]"
            ),
            metadata={"feature_count": len(gdf), "crs": str(gdf.crs)},
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        """geo_vector_data only reads local files."""
        return True
