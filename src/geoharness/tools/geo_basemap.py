"""Materialize configured remote basemaps; model-supplied URLs are never accepted."""

from __future__ import annotations

import asyncio
import json
from threading import Event
from typing import Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoharness.data.basemaps import fetch_basemap
from geoharness.data.profiles import record_profiles


class GeoBasemapInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["list", "fetch"] = "list"
    provider: str = Field(default="", max_length=256)
    bounds: list[float] | None = Field(default=None, min_length=4, max_length=4)
    crs: str = Field(default="EPSG:4326", max_length=256)
    zoom: int | None = Field(default=None, ge=0, le=22)

    @model_validator(mode="after")
    def required_fetch(self):
        if self.action == "fetch" and (not self.provider or self.bounds is None):
            raise ValueError(
                "fetch requires a configured provider and bounds [xmin,ymin,xmax,ymax].")
        return self


class GeoBasemapTool(BaseTool):
    name = "geo_basemap"
    description = (
        "List configured basemap providers, or fetch a bounded local RGB GeoTIFF. "
        "Pass provider ID and bounds with their CRS; omit zoom for bounded automatic selection. "
        "Use returned layer_spec in QGIS. Downloads use only configured services and credentials; "
        "do not supply URLs or tokens. Tile-aligned output bounds may exceed requested coverage."
    )
    input_model = GeoBasemapInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoBasemapInput)
        config = getattr(context.metadata.get("geoharness_config"), "basemaps", None)
        if config is None:
            return ToolResult(output="Error: basemaps configuration is unavailable.", is_error=True)
        if arguments.action == "list":
            providers = [{"id": key, "label": source.label, "grid": source.grid,
                          "min_zoom": source.min_zoom, "max_zoom": source.max_zoom,
                          "attribution": source.attribution}
                         for key, source in config.providers.items()]
            metadata = {"enabled": config.enabled, "providers": providers}
            return ToolResult(output=json.dumps(metadata, ensure_ascii=False), metadata=metadata)
        cancelled = Event()
        try:
            result = await asyncio.to_thread(fetch_basemap, config, context.cwd,
                                             arguments.provider, arguments.bounds,
                                             arguments.crs, arguments.zoom, cancelled=cancelled)
            record_profiles(context.metadata, result["data_profiles"])
            return ToolResult(output=json.dumps(result, ensure_ascii=False), metadata=result)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except ValueError as error:
            return ToolResult(output=f"Error: basemap fetch failed: {error}", is_error=True)
        except Exception as error:
            # Libraries may embed credential URLs in error messages; expose only the type.
            return ToolResult(output=f"Error: basemap fetch failed ({type(error).__name__}).",
                              is_error=True)

    def is_read_only(self, arguments: BaseModel) -> bool:
        return arguments.action == "list"
