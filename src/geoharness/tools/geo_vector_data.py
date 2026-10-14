"""GeoVectorDataTool — load local vector data (shapefile/GeoJSON/GPKG/CSV).

Dedicated to the *vector file* source. It resolves and reads local files via
the FileLoader only — it never queries the database or the boundary API, so a
vector lookup is a pure filesystem operation and cannot time out on a socket.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from geoharness.data.description import describe_source
from geoharness.data.encoding import source_encoding
from geoharness.data.profiles import (
    CRS_OPERATION_HINT,
    file_signature,
    local_source_listing,
    make_profile,
    record_profiles,
)

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
            result = await asyncio.wait_for(
                asyncio.to_thread(work), timeout=_VECTOR_TIMEOUT_SECONDS
            )
            if not result.is_error:
                record_profiles(context.metadata, result.metadata.get("data_profiles", []))
            return result
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
        output, metadata = local_source_listing(loader, kind="vector")
        return ToolResult(output=output, metadata=metadata)

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
        if detail.get("error"):
            return ToolResult(output=f"Error: inspect failed — {detail['error']}", is_error=True)
        profile = make_profile(detail, kind="vector")
        if "_measured_file_stat" in detail and profile is None:
            return ToolResult(
                output="Error: vector file changed during inspection; inspect again.", is_error=True
            )
        lines = [f"File: {detail.get('name', name)}", describe_source(detail, kind="vector")]
        lines.append(CRS_OPERATION_HINT)
        return ToolResult(
            output="\n".join(lines),
            metadata={"source_detail": detail, "data_profiles": [profile] if profile else []},
        )

    def _load(self, loader: Any, name: str) -> ToolResult:
        if not name:
            return ToolResult(
                output="Error: 'name' or 'file_path' is required for load.",
                is_error=True,
            )
        resolver = getattr(loader, "resolve_path", None)
        try:
            path = resolver(name) if callable(resolver) else None
        except Exception:  # noqa: BLE001 — optional adapter method must not invalidate a load
            path = None
        if path is None:
            try:
                path = loader.get_source_detail(name).get("path")
            except Exception:  # noqa: BLE001 — profile availability does not invalidate a load
                path = None
        signature = file_signature(path) if path is not None else None
        gdf = loader.load_geodataframe(name)
        b = gdf.total_bounds
        # Measure the loaded frame itself; no GeoDataFrame survives in shared metadata.
        detail = {
            "name": name,
            "source_type": "file",
            "path": str(Path(path).resolve()) if path is not None else None,
            "format": Path(path).suffix.lower().lstrip(".") if path is not None else "unknown",
            "crs": str(gdf.crs) if gdf.crs is not None else None,
            "bbox": b.tolist() if len(gdf) else None,
            "feature_count": len(gdf),
            "row_count": len(gdf),
            "schema": {str(col): str(dtype) for col, dtype in gdf.dtypes.items()},
            "null_counts": {str(col): int(series.isna().sum())
                            for col, series in gdf.items()},
            "geometry_type": ", ".join(str(t) for t in gdf.geom_type.dropna().unique()[:8]),
            "_measured_file_stat": signature,
        }
        if path is not None:
            detail.update(source_encoding(path))
        profile = make_profile(detail, kind="vector")
        return ToolResult(
            output=(
                f"Loaded vector file '{name}':\n"
                f"{describe_source(detail, kind='vector')}\n"
                "Read the registered source with read_vector(name/ref) or resolve_data(name/ref); "
                "DATA_DIRS are read roots, not necessarily dataset directories.\n"
                f"{CRS_OPERATION_HINT}"
            ),
            metadata={
                "feature_count": len(gdf), "crs": str(gdf.crs),
                "source_detail": detail,
                "data_profiles": [profile] if profile else [],
            },
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        """geo_vector_data only reads local files."""
        return True
