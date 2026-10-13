"""Structured native GIS operations with persistent, reproducible outputs."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from threading import Event
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

logger = logging.getLogger(__name__)


class SpatialSource(BaseModel):
    """Exactly one source locator, with an optional explicit CRS declaration."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    source_type: Literal["", "file", "postgis"] = ""
    file_path: str = ""
    source_name: str = ""
    geojson: str | dict[str, Any] = ""
    crs: str = Field(default="", description="Declare unknown CRS, or inline coordinate CRS.")
    filters: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict, max_length=16, description=(
            "Exact existing attribute selections, e.g. a forecast time. No SQL/expressions. "
            "An unmatched selection stays empty; it never falls back to all scenarios."
        ),
    )

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

    action: Literal["buffer", "clip", "intersection", "distance", "count", "impact", "area"]
    source: SpatialSource = Field(
        description="Assets/features to analyze; original data is read-only."
    )
    reference: SpatialSource | None = Field(
        default=None, description="Hazards or overlay/mask layer."
    )
    zones: SpatialSource | None = Field(default=None, description="Optional counting zones.")
    distance_m: float = Field(default=0, ge=0, allow_inf_nan=False)
    distance_breaks_m: list[float] = Field(default_factory=list, max_length=32, description=(
        "distance/impact only: positive ascending thresholds for native distance_class. "
        "Containment/intersection are separate from external distance bands."
    ))

    @field_validator("distance_breaks_m")
    @classmethod
    def validate_breaks(cls, values):
        import math

        if any(not math.isfinite(v) or v <= 0 for v in values) or any(
            a >= b for a, b in zip(values, values[1:])
        ):
            raise ValueError("distance_breaks_m must be positive, finite and strictly ascending")
        return values
    group_field: str = Field(default="", max_length=128, description=(
        "area only: optional existing attribute, e.g. a time field. Reports each group's "
        "union and sum separately; cumulative/overlapping time ranges are not additive coverage."
    ))
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
        if self.action in {"buffer", "area"} and self.reference is not None:
            raise ValueError(f"{self.action} uses source only; reference is not applicable.")
        if self.group_field and self.action != "area":
            raise ValueError("group_field applies only to area.")
        if self.action not in ("buffer", "impact") and self.distance_m != 0:
            raise ValueError("distance_m only applies to buffer and impact.")
        if self.distance_breaks_m and self.action not in {"distance", "impact"}:
            raise ValueError("distance_breaks_m only applies to distance and impact")
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
        "matching zone. impact distance_m=0 means exact intersection. "
        "area measures polygon area_m2, union coverage, feature-area sum and overlap in a "
        "metre CRS; group_field optionally groups time/category ranges. Overlapping ranges "
        "must not be summed as total coverage. Use its result layer and analysis_manifests "
        "for automatically verified map notes rather than handwritten area totals. "
        "source/reference.filters selects exact time/scenario attributes before analysis. "
        "distance/impact return reference_relation (covered_by/intersects/outside); "
        "distance_breaks_m adds distance_class with external bands. Never label a positive "
        "distance threshold as inside; use these fields and their reference_scope."
    )
    input_model: type[BaseModel] = GeoAnalysisInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoAnalysisInput)
        from geoharness.analysis import analyze_vectors

        config = context.metadata.get("geoharness_config")
        from geoharness.data.task_context import resolve_registered_path

        try:
            updates = {}
            for name in ("source", "reference", "zones"):
                source = getattr(arguments, name)
                if source and source.file_path:
                    updates[name] = source.model_copy(update={"file_path": resolve_registered_path(
                        source.file_path, context.metadata, cwd=Path(context.cwd), config=config)})
            arguments = arguments.model_copy(update=updates)
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Error: data reference failed: {exc}", is_error=True)
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
                    + ("Reference scope: " + json.dumps(manifest["reference_scope"],
                                                        ensure_ascii=False) + "\n"
                       if "reference_scope" in manifest else "")
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
