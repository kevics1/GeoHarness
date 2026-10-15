"""A small method-contract tool for hazard-aware road accessibility."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from threading import Event
from typing import Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoharness.tools.geo_analysis import SpatialSource


class GeoAccessibilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    roads: SpatialSource = Field(description="Existing road lines with stable unique IDs.")
    residents: SpatialSource = Field(description="Point assets; keep original ID/geometry binding.")
    shelters: SpatialSource = Field(description="Point destinations; capacity is not allocated.")
    road_id_field: str = Field(
        min_length=1, description="Existing unique non-null string/integer ID."
    )
    resident_id_field: str = Field(min_length=1, description="Existing unique non-null asset ID.")
    shelter_id_field: str = Field(
        min_length=1, description="Existing unique non-null destination ID."
    )
    direction_mode: Literal["bidirectional", "field"] = Field(
        description="Explicit assumption. field requires a mapping of all direction codes; "
        "forward/reverse follow each source line's coordinate order.",
    )
    speed_kmh: float = Field(gt=0, description="Explicit constant road speed for estimated time.")
    max_snap_distance_m: float = Field(
        ge=0,
        description="Maximum safe straight connector to a road segment; 0 means on-road only.",
    )
    connector_speed_kmh: float = Field(
        default=5,
        gt=0,
        description="Constant endpoint connector speed, distinct from road speed.",
    )
    target_crs: str = Field(
        default="",
        description="Projected metre CRS. Required when neither roads nor the configured "
        "analysis.metric_crs provides a metre projection.",
    )
    direction_field: str = ""
    direction_values: dict[str, Literal["forward", "reverse", "both", "closed"]] = Field(
        default_factory=dict,
        max_length=64,
        description='Exact source codes, e.g. {"B":"both","F":"forward"}; never infer meanings.',
    )
    hazards: SpatialSource | None = Field(
        default=None,
        description="Exact selected polygon hazards; no automatic forecast-time choice.",
    )
    hazard_buffer_m: float = Field(default=0, ge=0)
    routes_per_resident: int = Field(default=1, ge=1, le=10)
    output_name: str = Field(default="", description="Stem for a new analysis job directory.")

    @model_validator(mode="after")
    def method_contract(self) -> GeoAccessibilityInput:
        if self.direction_mode == "field" and not (self.direction_field and self.direction_values):
            raise ValueError("field mode requires direction_field and complete direction_values")
        if self.direction_mode == "bidirectional" and (
            self.direction_field or self.direction_values
        ):
            raise ValueError("bidirectional mode does not accept direction_field/direction_values")
        if self.hazard_buffer_m and self.hazards is None:
            raise ValueError("hazard_buffer_m requires hazards")
        return self


class GeoAccessibilityTool(BaseTool):
    name: str = "geo_accessibility"
    description: str = (
        "Native road accessibility: one result/status for every resident, nearest safe shelter "
        "routes and estimated times. Requires explicit stable IDs, direction mode, road speed "
        "and maximum snap distance. Uses existing shared road XY vertices as junctions; crossings "
        "without a shared vertex do not connect. Supply correctly noded road data. Snaps points "
        "to safe road segments, checks endpoint connectors and removes hazard-intersecting road "
        "segments. Honors explicitly mapped one-way codes. Reports disconnected/too-far/blocked "
        "cases instead of silently dropping residents. Routes include both connectors and "
        "verified geometry lengths. Output is atomic CRS-safe GeoJSON/CSV plus method manifest "
        "under analysis.outputs_dir. Select forecast time/scenario with hazards.filters. "
        "Requires explicit 2D geometry. Minimizes distance; no time optimization, capacity "
        "allocation, traffic or turn restrictions; no "
        "population estimation or validated risk score is provided. Read method assumptions."
    )
    input_model: type[BaseModel] = GeoAccessibilityInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoAccessibilityInput)
        from geoharness.analysis.accessibility import analyze_accessibility
        from geoharness.data.task_context import resolve_registered_path

        config = context.metadata.get("geoharness_config")
        analysis = getattr(config, "analysis", None)
        timeout = getattr(analysis, "timeout_seconds", 120)
        cancelled = Event()
        try:
            updates = {}
            for label in ("roads", "residents", "shelters", "hazards"):
                source = getattr(arguments, label)
                if source and source.file_path:
                    updates[label] = source.model_copy(
                        update={
                            "file_path": resolve_registered_path(
                                source.file_path,
                                context.metadata,
                                cwd=Path(context.cwd),
                                config=config,
                            )
                        }
                    )
            arguments = arguments.model_copy(update=updates)
            manifest = await asyncio.wait_for(
                asyncio.to_thread(
                    analyze_accessibility,
                    arguments,
                    cwd=Path(context.cwd).resolve(),
                    outputs_dir=getattr(analysis, "outputs_dir", ".geoharness/analysis"),
                    metric_crs=getattr(analysis, "metric_crs", ""),
                    catalog=context.metadata.get("data_catalog"),
                    timeout_seconds=timeout,
                    publication_settings=getattr(config, "publication", None),
                    cancelled=cancelled,
                ),
                timeout=timeout,
            )
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "succeeded",
                        "manifest_path": manifest["manifest_path"],
                        "statistics": manifest["statistics"],
                        "metric_crs": manifest["metric_crs"],
                        "assumptions": manifest["assumptions"],
                        "artifacts": manifest["paths"],
                        "table_previews": manifest["table_previews"],
                        "suggested_layers": manifest["layers"],
                        "quality_scope": "Method/data integrity checks passed; scientific "
                        "analysis_correctness remains unverified.",
                    },
                    ensure_ascii=False,
                    allow_nan=False,
                ),
                metadata=manifest,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            cancelled.set()
            return ToolResult(
                output=f"Accessibility timed out after {timeout}s; "
                "no published result returned; worker cancellation requested.",
                is_error=True,
                metadata={"diagnostics": getattr(exc, "diagnostics", {})},
            )
        except asyncio.CancelledError:
            cancelled.set()
            raise
        except Exception as exc:
            diagnostic = getattr(exc, "diagnostics", {})
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "failed",
                        "error_summary": f"{type(exc).__name__}: {exc}",
                        "diagnostics": diagnostic,
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
                metadata={"diagnostics": diagnostic},
            )

    def is_read_only(self, arguments: BaseModel) -> bool:
        return False
