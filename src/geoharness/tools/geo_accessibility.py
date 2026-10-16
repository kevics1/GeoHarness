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


def _bounded_receipt(payload: dict, max_chars: int, primary: str) -> str:
    """Keep action/meaning/path evidence readable; full tables stay in the manifest."""

    def encode() -> str:
        return json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))

    omitted = []
    payload["deferred_sections"] = omitted
    for field in ("table_previews", "suggested_layers"):
        if len(encode()) <= max_chars:
            break
        if field in payload:
            payload.pop(field)
            omitted.append(field)
    if len(encode()) > max_chars:
        for label, entry in payload["artifacts"].items():
            if label not in {"result", primary}:
                for field in ("schema", "source_attribute_fields", "meaning", "field_roles",
                              "csv", "schema_scope", "validation_state", "state"):
                    entry.pop(field, None)
        omitted.append("secondary artifact schema samples")
    for field in ("comparison",):
        if len(encode()) <= max_chars:
            break
        payload.pop(field, None)
        omitted.append(field)
    if len(encode()) > max_chars:
        for entry in payload["artifacts"].values():
            for field in ("source_attribute_fields", "meaning", "csv", "schema_scope"):
                entry.pop(field, None)
        omitted.append("source schema samples")
    for field in (
        "statistics",
        "artifacts",
    ):
        if len(encode()) <= max_chars:
            break
        if field in payload:
            payload.pop(field)
            omitted.append(field)
    if len(encode()) > max_chars:
        payload.pop("qgis_inputs", None)
        omitted.append("qgis_inputs")
    if len(encode()) > max_chars:
        # Do not let unusually long filters/field names hide the exact recovery entry.
        core = {key: payload[key] for key in ("status", "manifest_path", "inspect_contract_call",
                                            "quality_scope")}
        core["deferred_sections"] = list(payload.keys())
        core["note"] = "Full contract is in the verified manifest. Use inspect_contract_call; " \
                       "change label/offset to inspect each artifact schema."
        return json.dumps(core, ensure_ascii=False, separators=(",", ":"))
    return encode()


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
            visible_fields = {
                "geojson",
                "csv",
                "feature_count",
                "crs",
                "state",
                "validation_state",
                "role",
                "scenario",
                "meaning",
                "field_roles",
            }
            artifacts = {
                label: {key: value for key, value in entry.items() if key in visible_fields}
                for label, entry in manifest["paths"].items()
            }
            for label, entry in manifest["paths"].items():
                sample = entry["source_attribute_fields"][:12]
                displayed = set(entry["field_roles"].values()) | set(sample)
                artifacts[label].update(
                    schema={
                        field: dtype
                        for field, dtype in entry["schema"].items()
                        if field in displayed
                    },
                    schema_scope="role fields and first 12 source attributes; "
                                 "full schema in manifest",
                    source_attribute_fields=sample,
                    source_attribute_count=len(entry["source_attribute_fields"]),
                    schema_field_count=len(entry["schema"]),
                )
            preview_fields = {
                "gh_resident_id",
                "gh_status",
                "gh_status_label_zh",
                "gh_shelter_id",
                "gh_distance_m",
                "gh_time_min",
                "gh_transition",
                "gh_objective_delta",
                "gh_baseline_time_min",
                "gh_hazard_time_min",
            }
            previews = {
                label: [
                    {key: value for key, value in row.items() if key in preview_fields}
                    for row in manifest["table_previews"].get(label, [])[:6]
                ]
                for label in {"result", primary}
            }
            return ToolResult(
                output=_bounded_receipt(
                    {
                        "status": "succeeded",
                        "manifest_path": manifest["manifest_path"],
                        "method_parameters": manifest["parameters"],
                        "input_selection": manifest["input_selection"],
                        "hazard_scope": manifest["hazard_scope"],
                        "status_definitions": manifest["status_definitions"],
                        "inspect_contract_call": {
                            "tool": "geo_python",
                            "arguments": {
                                "input_refs": {"manifest": manifest["manifest_path"]},
                                "code": (
                                    "import json\n"
                                    "raw = INPUTS['manifest'].read_text(encoding='utf-8')\n"
                                    "contract = json.loads(raw)\n"
                                    "label, offset = 'result', 0\n"
                                    "entry = contract['paths'][label]\n"
                                    "fields = list(entry['schema'])\n"
                                    "print(json.dumps({'role': entry.get('role'), "
                                    "'field_count': len(fields), 'schema': "
                                    "{name: entry['schema'][name] "
                                    "for name in fields[offset:offset+24]}, "
                                    "'next_offset': offset+24 "
                                    "if offset+24 < len(fields) else None}, "
                                    "ensure_ascii=False))"
                                ),
                            },
                        },
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
                    max_chars=getattr(analysis, "max_output_chars", 12000),
                    primary=primary,
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
