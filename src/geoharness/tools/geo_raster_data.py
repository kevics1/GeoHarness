"""GeoRasterDataTool — load local raster data (GeoTIFF/IMG/ASC/VRT/…).

Dedicated to the *raster file* source, backed by ``RasterLoader`` (rasterio).
Rasters are gridded continuous surfaces, not features, so this tool exposes
band statistics and clipping rather than vector-style discovery — and it never
touches the database or the network.
"""

from __future__ import annotations

import asyncio
import logging
from functools import partial
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from geoharness.data.profiles import (
    CRS_OPERATION_HINT,
    local_source_listing,
    make_profile,
    record_profiles,
)

logger = logging.getLogger(__name__)

_RASTER_TIMEOUT_SECONDS = 60.0


class GeoRasterDataInput(BaseModel):
    """Input model for geo_raster_data."""

    action: Literal["list", "inspect", "stats"] = Field(
        default="list",
        description=(
            "list: list local raster files; "
            "inspect: show size/bands/CRS/bounds/resolution; "
            "stats: compute min/max/mean/percentiles for a band"
        ),
    )
    name: str = Field(
        default="",
        description=(
            "Raster file name with or without extension (e.g. '西昌市高程' or "
            "'西昌市高程.tif'). Required for inspect and stats."
        ),
    )
    file_path: str = Field(
        default="",
        description="Explicit path to a raster file; overrides 'name'.",
    )
    band: int = Field(
        default=1,
        ge=1,
        description="1-based band index (rasters are usually single-band).",
    )

    def target(self) -> str:
        return (self.file_path or self.name).strip()


class GeoRasterDataTool(BaseTool):
    """Load and inspect local raster files."""

    name: str = "geo_raster_data"
    description: str = (
        "Load local raster data (GeoTIFF/IMG/ASC/VRT). "
        "Actions: list (raster files), inspect (dimensions/CRS/bounds), "
        "stats (band min/max/mean). Use this for gridded surfaces such as "
        "elevation or fuel-load rasters."
    )
    input_model: type[BaseModel] = GeoRasterDataInput

    async def execute(
        self, arguments: BaseModel, context: ToolExecutionContext
    ) -> ToolResult:
        assert isinstance(arguments, GeoRasterDataInput)
        args: GeoRasterDataInput = arguments

        catalog = context.metadata.get("data_catalog")
        if catalog is None:
            return ToolResult(
                output="Error: DataCatalog not configured. Run `geoh init`.",
                is_error=True,
            )

        try:
            loader = catalog.get_raster_connector()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                output=f"Error: raster connector unavailable — {exc}",
                is_error=True,
            )
        if loader is None:
            return ToolResult(
                output=(
                    "Error: no raster source configured. Set data.file_dir in "
                    "~/.geoharness/config.yaml, or run from the folder holding "
                    "your rasters."
                ),
                is_error=True,
            )

        if args.action == "list":
            work = partial(self._list, loader)
        elif args.action == "inspect":
            work = partial(self._inspect, loader, args.target())
        elif args.action == "stats":
            work = partial(self._stats, loader, args.target(), args.band)
        else:
            return ToolResult(output=f"Unknown action: {args.action}", is_error=True)

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(work), timeout=_RASTER_TIMEOUT_SECONDS
            )
            if not result.is_error:
                record_profiles(context.metadata, result.metadata.get("data_profiles", []))
            return result
        except asyncio.TimeoutError:
            return ToolResult(
                output=(
                    f"Error: raster operation timed out after "
                    f"{_RASTER_TIMEOUT_SECONDS:.0f}s (action={args.action}). "
                    "The raster may be very large."
                ),
                is_error=True,
            )
        except ImportError:
            return ToolResult(
                output=(
                    "Error: rasterio is not installed. Install raster support "
                    "with: uv pip install rasterio"
                ),
                is_error=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("geo_raster_data action failed")
            return ToolResult(
                output=f"Error: {args.action} failed — {exc}", is_error=True
            )

    def _list(self, loader: Any) -> ToolResult:
        output, metadata = local_source_listing(loader, kind="raster")
        return ToolResult(output=output, metadata=metadata)

    def _inspect(self, loader: Any, name: str) -> ToolResult:
        if not name:
            return ToolResult(
                output="Error: 'name' or 'file_path' is required for inspect.",
                is_error=True,
            )
        detail = loader.get_source_detail(name)
        if not detail or detail.get("error"):
            return ToolResult(
                output=f"Raster file '{name}' not found or not readable.",
                is_error=True,
            )
        profile = make_profile(detail, kind="raster")
        if "_measured_file_stat" in detail and profile is None:
            return ToolResult(
                output="Error: raster file changed during inspection; inspect again.", is_error=True
            )
        lines = [f"Raster: {detail.get('name', name)}"]
        lines.append(f"  Path: {detail.get('path', '')}")
        lines.append(f"  Driver: {detail.get('format', '')}")
        lines.append(
            f"  Size: {detail.get('width')} x {detail.get('height')} px, "
            f"{detail.get('band_count')} band(s), dtype={detail.get('dtype')}"
        )
        if detail.get("crs"):
            lines.append(f"  CRS: {detail['crs']}")
        res = detail.get("resolution")
        if res:
            lines.append(f"  Resolution: {res[0]:.6g} x {res[1]:.6g}")
        bbox = detail.get("bbox")
        if bbox:
            lines.append(
                f"  Bounds: [{bbox[0]:.4f}, {bbox[1]:.4f}, "
                f"{bbox[2]:.4f}, {bbox[3]:.4f}]"
            )
        if detail.get("nodata") is not None:
            lines.append(f"  NoData: {detail['nodata']}")
        lines.append(CRS_OPERATION_HINT)
        return ToolResult(
            output="\n".join(lines),
            metadata={"source_detail": detail, "data_profiles": [profile] if profile else []},
        )

    def _stats(self, loader: Any, name: str, band: int) -> ToolResult:
        if not name:
            return ToolResult(
                output="Error: 'name' or 'file_path' is required for stats.",
                is_error=True,
            )
        try:
            detail = loader.get_source_detail(name)
        except Exception:  # noqa: BLE001 — optional header metadata must not discard valid stats
            detail = {}
        # The header and pixels must refer to the same measured file version.
        # A replacement during band_stats is rejected by make_profile below.
        stats = loader.band_stats(name, band=band)
        profile = make_profile(detail, kind="raster") if not detail.get("error") else None
        return ToolResult(
            output=(
                f"Band {stats['band']} statistics for '{name}':\n"
                f"  Valid pixels: {stats['count']}\n"
                f"  Min / Max: {stats['min']:.4f} / {stats['max']:.4f}\n"
                f"  Mean / Std: {stats['mean']:.4f} / {stats['std']:.4f}\n"
                f"  P5 / P50 / P95: {stats['p5']:.4f} / {stats['p50']:.4f} / "
                f"{stats['p95']:.4f}\n{CRS_OPERATION_HINT}"
            ),
            metadata={**stats, "data_profiles": [profile] if profile else []},
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        """geo_raster_data only reads local rasters."""
        return True
