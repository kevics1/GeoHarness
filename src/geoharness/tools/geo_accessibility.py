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

    action: Literal["route", "compare"] = Field(
        default="route",
        description="route: one scenario. compare: baseline and hazards on one "
        "frozen original-road access candidate graph, with per-resident transitions.",
    )
    objective: Literal["distance", "time"] = Field(
        default="distance",
        description="Optimize total metres or total estimated minutes, "
        "including road and endpoint connector costs. Keep the same objective across scenarios.",
    )
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
        description="Explicit assumption. field REQUIRES direction_field plus direction_values, "
        "e.g. direction_field='oneway', direction_values={'B':'both','F':'forward'}; "
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
    direction_field: str = Field(
        default="",
        description="REQUIRED nonempty existing road field when direction_mode='field', "
        "e.g. 'oneway'. Empty only in bidirectional mode. Never guess direction codes.",
    )
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
        if self.action == "compare" and self.hazards is None:
            raise ValueError(
                "action=compare requires hazards; baseline uses the same original inputs"
            )
        return self


class GeoAccessibilityTool(BaseTool):
    name: str = "geo_accessibility"
    description: str = (
        "Native road accessibility: one result/status for every resident, nearest safe shelter "
        "routes and estimated times. Requires explicit stable IDs, direction mode, road speed "
        "and maximum snap distance. Uses existing shared road XY vertices as junctions; crossings "
        "without a shared vertex do not connect. Supply correctly noded road data. Snaps points "
        "to all eligible original-road segment projections before hazard filtering; "
        "checks endpoint connectors and removes hazard-intersecting road "
        "segments. Honors explicitly mapped one-way codes. Reports disconnected/too-far/blocked "
        "cases instead of silently dropping residents. Routes include both connectors and "
        "verified geometry lengths. Output is atomic CRS-safe GeoJSON/CSV plus method manifest "
        "under analysis.outputs_dir. Select forecast time/scenario with hazards.filters. "
        "action=compare shares baseline/hazard access candidates and reports transitions, access "
        "changes and deletion-only objective monotonicity. objective=distance|time is explicit. "
        "snap_too_far is a data/threshold limit, not real physical inaccessibility. "
        "Requires explicit 2D geometry. No capacity "
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
            primary = "hazard_result" if arguments.action == "compare" else "result"
            map_names = (
                {"hazard_result", "hazard_routes", "hazard_shelters"}
                if arguments.action == "compare"
                else {"result", "routes", "shelters"}
            )
            qgis_inputs = {
                "layers": [layer for layer in manifest["layers"] if layer["name"] in map_names],
                "analysis_manifests": [manifest["manifest_path"]],
            }
            visible_fields = {"geojson", "csv", "feature_count", "crs", "state", "validation_state"}
            artifacts = {
                label: {key: value for key, value in entry.items() if key in visible_fields}
                for label, entry in manifest["paths"].items()
            }
            preview_fields = {
                "gh_resident_id",
                "gh_status",
                "gh_shelter_id",
                "gh_distance_m",
                "gh_time_min",
                "gh_transition",
                "gh_objective_delta",
            }
            previews = {
                label: [
                    {key: value for key, value in row.items() if key in preview_fields}
                    for row in manifest["table_previews"].get(label, [])[:6]
                ]
                for label in {"result", primary}
            }
            return ToolResult(
                output=json.dumps(
                    {
                        "status": "succeeded",
                        "manifest_path": manifest["manifest_path"],
                        "qgis_inputs": qgis_inputs,
                        "comparison": manifest.get("comparison"),
                        "statistics": manifest["statistics"],
                        "metric_crs": manifest["metric_crs"],
                        "assumptions": manifest["assumptions"],
                        "artifacts": artifacts,
                        "table_previews": previews,
                        "suggested_layers": manifest["layers"],
                        "map_layer_note": "qgis_inputs selects the target scenario only. "
                        "All outputs remain in suggested_layers/manifest; access_candidates "
                        "is audit evidence, not a default map layer.",
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
