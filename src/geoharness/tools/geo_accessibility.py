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
        description='默认route计算单场景；compare使用同一冻结道路图比较基线和危险场景。',
    )
    objective: Literal["distance", "time"] = Field(
        default="distance",
        description='默认distance最小总米数；time最小估计分钟数，均含端点连接段，各场景保持一致。',
    )
    roads: SpatialSource = Field(description='必填，道路线来源，须有稳定唯一业务ID。')
    residents: SpatialSource = Field(description='必填，居民点来源，保留实体ID与原始几何对应。')
    shelters: SpatialSource = Field(description='必填，避难点来源；本工具不分配容量。')
    road_id_field: str = Field(
        min_length=1, description='必填，道路中实际存在、非空唯一的字符串或整数ID字段。'
    )
    resident_id_field: str = Field(min_length=1,
        description='必填，居民点中实际存在、非空唯一的实体ID字段。')
    shelter_id_field: str = Field(
        min_length=1, description='必填，避难点中实际存在、非空唯一的实体ID字段。'
    )
    direction_mode: Literal["bidirectional", "field"] = Field(
        description='必填，明确双向bidirectional或按字段field；field须声明字段和代码映射，正反按源线坐标顺序。',
    )
    speed_kmh: float = Field(gt=0, description='必填，正数，道路恒定速度，单位km/h，用于估计时间。')
    max_snap_distance_m: float = Field(
        ge=0,
        description='必填，非负，端点到道路的最大直线连接距离，单位米；0仅允许路上点。',
    )
    connector_speed_kmh: float = Field(
        default=5,
        gt=0,
        description='默认5，正数，端点连接段速度km/h，与道路速度分开。',
    )
    target_crs: str = Field(
        default="",
        description='默认空；目标投影CRS须以米为单位，源/配置无合适投影时必填。',
    )
    direction_field: str = Field(
        default="",
        description='默认空；direction_mode=field时必须为实际道路方向字段，双向模式省略。',
    )
    direction_values: dict[str, Literal["forward", "reverse", "both", "closed"]] = Field(
        default_factory=dict,
        max_length=64,
        description='默认空；源代码到forward/reverse/both/closed的明确映射，不能猜含义。',
    )
    hazards: SpatialSource | None = Field(
        default=None,
        description='默认null；准确选择的面状危险范围，不自动选择预报时次。',
    )
    hazard_buffer_m: float = Field(default=0, ge=0,
        description='危险区外扩距离，单位米；非0时必须提供hazards；0使用原范围。')
    routes_per_resident: int = Field(default=1, ge=1, le=10,
        description='每个居民点保留的候选避难路线数，1..10；不代表避难场所容量分配。')
    output_name: str = Field(default="",
        description='新分析目录的文件名主体，默认空自动生成，不传目录路径。')

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
        (
        '原生道路可达性：为每个居民点返回结果或失败状态，包含安全避难路线及两端连'
        '接段。需明确道路方向、速度与吸附距离，不猜业务参数，不分配避难容量。co'
        'mpare在同一冻结道路图比较基线/危险场景；distance/time'
        '分别优化总米数/估计分钟。返回持久结果、来源/字段契约、统计及供QGIS'
        '使用的图层；审查失败原因，不能将不可达对象静默丢弃。'
    )
    )
    description += (
        '仅支持明确二维道路，只有共享XY顶点才连接，跨线无共享点不自动连通，须先正确建网。'
        '先在原道路投影建立候选，再检查连接段并移除危险区相交路段；hazards.filters选择时次。'
        '不可达/过远/阻断分别报告；snap_too_far是数据或阈值限制，不能断言物理不可达。'
        '输出原子保存且CRS一致的GeoJSON/CSV/方法清单；路线长度包括两端连接段。'
        '不模拟交通、转向限制、人口估计或经验证的风险分值，先读方法假设。'
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
