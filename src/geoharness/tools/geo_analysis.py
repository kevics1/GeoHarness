"""Structured native GIS operations with persistent, reproducible outputs."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from threading import Event
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)


class SpatialSource(BaseModel):
    """Exactly one source locator, with an optional explicit CRS declaration."""

    model_config = ConfigDict(extra="forbid")

    source_type: Literal["", "file", "postgis"] = ""
    file_path: str = ""
    source_name: str = ""
    geojson: str | dict[str, Any] = ""
    crs: str = Field(default="", description="Declare unknown CRS, or inline coordinate CRS.")

    @model_validator(mode="after")
    def validate_locator(self) -> SpatialSource:
        if sum(bool(value) for value in (self.file_path, self.source_name, self.geojson)) != 1:
            raise ValueError("Provide exactly one of file_path, source_name, or geojson.")
        if self.source_type == "postgis" and not self.source_name:
            raise ValueError("PostGIS requires source_name (table name).")
        return self


class GeoAnalysisInput(BaseModel):
    """Multi-layer vector analysis. All distances are metres."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["buffer", "clip", "intersection", "distance", "count", "impact"]
    source: SpatialSource = Field(
        description="Assets/features to analyze; original data is read-only."
    )
    reference: SpatialSource | None = Field(
        default=None, description="Hazards or overlay/mask layer."
    )
    zones: SpatialSource | None = Field(default=None, description="Optional counting zones.")
    distance_m: float = Field(default=0, ge=0, allow_inf_nan=False)
    output_name: str = Field(default="", description="Plain stem for a new output directory.")

    @model_validator(mode="after")
    def validate_operation(self) -> GeoAnalysisInput:
        if self.action == "buffer" and self.distance_m <= 0:
            raise ValueError("buffer requires distance_m > 0.")
        if self.action in ("clip", "intersection", "distance", "impact") and self.reference is None:
            raise ValueError(f"{self.action} requires reference.")
        if self.action == "count" and self.reference is None and self.zones is None:
            raise ValueError("count requires zones or reference.")
        if self.action == "count" and self.reference is not None and self.zones is not None:
            raise ValueError("count accepts one zone layer: zones or reference, not both.")
        if self.zones is not None and self.action not in ("count", "impact"):
            raise ValueError("zones is only used by count or impact.")
        if self.action == "buffer" and self.reference is not None:
            raise ValueError("buffer uses source only; reference is not applicable.")
        if self.action not in ("buffer", "impact") and self.distance_m != 0:
            raise ValueError("distance_m only applies to buffer and impact.")
        return self


class GeoAnalysisTool(BaseTool):
    name: str = "geo_analysis"
    description: str = (
        "Native vector GIS: buffer, clip, intersection, nearest distance, zone counts, "
        "and impact (asset distance to hazards, affected flag, optional zone totals). "
        "Accept file/PostGIS/inline GeoJSON sources. Metric analysis reprojects input CRS "
        "to configured metre CRS or local UTM. Writes GeoJSON/CSV plus provenance manifest "
        "and layer specs for geo_cartography; does not alter original data. "
        "Zone counts use intersects including boundaries and count each asset in every "
        "matching zone. impact distance_m=0 means exact intersection."
    )
    input_model: type[BaseModel] = GeoAnalysisInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoAnalysisInput)
        from geoharness.analysis import analyze_vectors

        config = context.metadata.get("geoharness_config")
        analysis = getattr(config, "analysis", None)
        outputs_dir = getattr(analysis, "outputs_dir", ".geoharness/analysis")
        metric_crs = getattr(analysis, "metric_crs", "")
        timeout = getattr(analysis, "timeout_seconds", 120)
        cancelled = Event()
        try:
            manifest = await asyncio.wait_for(
                asyncio.to_thread(
                    analyze_vectors,
                    arguments,
                    cwd=Path(context.cwd).resolve(),
                    catalog=context.metadata.get("data_catalog"),
                    publication_settings=getattr(config, "publication", None),
                    outputs_dir=outputs_dir,
                    metric_crs=metric_crs,
                    timeout_seconds=timeout,
                    cancelled=cancelled,
                ),
                timeout=timeout,
            )
            statistics = manifest["statistics"]
            output_path = manifest["paths"]["result"]["geojson"]
            return ToolResult(
                output=(
                    f"{arguments.action}: {statistics['feature_count']} result feature(s); "
                    f"metric CRS {manifest['metric_crs']}.\n"
                    f"Statistics: {json.dumps(statistics)}\n"
                    f"Result: {output_path}\nManifest: {manifest['manifest_path']}\n"
                    f"Artifacts: {json.dumps(manifest['paths'], ensure_ascii=False)}\n"
                    f"Table previews (first {manifest['preview_row_limit']} rows; no geometry): "
                    f"{json.dumps(manifest['table_previews'], ensure_ascii=False)}\n"
                    f"Suggested layers: {json.dumps(manifest['layers'], ensure_ascii=False)}\n"
                    + ("Empty result persisted successfully.\n" if statistics["feature_count"] == 0
                       else "")
                    + (f"Counting: {manifest['count_semantics']}"
                       if "count_semantics" in manifest else "")
                ),
                metadata={**manifest, "output_path": output_path},
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            cancelled.set()
            diagnostic = getattr(exc, "diagnostics", None)
            retained = (
                f" Prepared results retained at {diagnostic['source']}; publication interrupted."
                if diagnostic else ""
            )
            return ToolResult(
                output=(
                    f"Error: spatial analysis timed out after {timeout}s; "
                    "no published result returned. Worker cancellation requested." + retained
                ),
                is_error=True,
                metadata={"publication": diagnostic} if diagnostic else {},
            )
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except Exception as exc:
            logger.exception("geo_analysis failed")
            return ToolResult(
                output=f"Error: {arguments.action} failed — {exc}", is_error=True,
                metadata=({"publication": exc.diagnostics} if hasattr(exc, "diagnostics") else {}),
            )

    def is_read_only(self, arguments: BaseModel) -> bool:
        return False
