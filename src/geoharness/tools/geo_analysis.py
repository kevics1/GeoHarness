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

    source_type: Literal["", "file", "postgis"] = Field(default='',
        description='数据来源：file本地文件、postgis数据库表；空值按唯一填写的定位字段识别。')
    file_path: str = Field(default='',
        description='本地文件的实际返回路径或data:引用；与source_name/geojson三选一。')
    source_name: str = Field(default='',
        description='已发现的数据集名称或PostGIS表名；与file_path/geojson三选一，不猜名称。')
    geojson: str | dict[str, Any] = Field(default='',
        description='完整GeoJSON对象或一次JSON编码字符串；与其他定位字段三选一，注明真实坐标CRS。')
    crs: str = Field(default="",
        description='未知来源CRS或内联坐标的明确声明，默认空；不改变已有坐标。')
    filters: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict, max_length=16, description=(
            '默认空；字段名到准确值的等值筛选对象，不是SQL。未匹配保持空选集。'
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def decode_json_object(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (ValueError, RecursionError) as exc:
                raise ValueError("Spatial source must be an object or one JSON-encoded object") \
                    from exc
            if not isinstance(decoded, dict):
                raise ValueError("Spatial source JSON must decode to an object in one step")
            return decoded
        return value

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

    action: Literal[
        "buffer", "clip", "intersection", "distance", "count", "impact", "area"
    ] = Field(
        description='buffer缓冲、clip裁剪、intersection相交、distance最近距离、count空间计数、'
        'impact影响筛选、area面积统计；标准复杂分析优先geo_qgis已安装算法。')
    source: SpatialSource = Field(
        description='必填，资产/要素的数据来源对象；原始数据只读，建议直接传对象。'
    )
    reference: SpatialSource | None = Field(
        default=None, description='叠加/掩膜/危险区域的来源对象；默认null，按action要求提供。'
    )
    zones: SpatialSource | None = Field(default=None, description='计数分区的来源对象，默认null。')
    distance_m: float = Field(default=0, ge=0, allow_inf_nan=False,
        description='缓冲或影响距离，单位米；0表示不增加外部缓冲，正距离不能被解释为包含。')
    distance_breaks_m: list[float] = Field(default_factory=list, max_length=32, description=(
        '默认空；distance/impact的正数严格升序距离阈值，单位米，不代替包含判定。'
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
        '默认空；仅area使用的真实分组字段，例如时次；重叠时次面积不可直接相加。'
    ))
    output_name: str = Field(default="",
        description='新输出目录的文件名主体，默认空自动生成，不传目录路径。')

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
        (
        '原生矢量分析兼容入口：缓冲、裁剪、相交、最近距离、分区计数、影响与面积；'
        '标准复杂分析优先geo_qgis。读取真实来源，原数据只读，返回新结果/'
        '清单/字段与统计；距离以米计。source/reference.filt'
        'ers明确选择时次场景；distance/impact返回referen'
        'ce_relation区分covered_by/intersects/o'
        'utside，distance_breaks_m生成外部distance'
        '_class。正距离阈值不等于落在危险区内，按reference_sco'
        'pe解释结果。'
    )
    )
    description += (
        '支持file/PostGIS/内联GeoJSON；按配置米制CRS或本地UTM重投影，输出GeoJSON/CSV和来源清单。'
        'count按intersects含边界计数，一对象可计入多个匹配区；impact距离0为精确相交。'
        'area区分area_m2、合并覆盖、要素面积和与重叠；重叠分组不可相加冒充总覆盖。'
        '使用结果图层和analysis_manifests绑定图注，不能手写面积统计代替验证。'
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
            qgis_inputs = {"layers": [{"file_path": entry["geojson"], "kind": "vector",
                "name": label, "expected_feature_count": entry["feature_count"],
                "required": entry["feature_count"] > 0}
                for label, entry in manifest["paths"].items() if entry.get("geojson")],
                "analysis_manifests": [manifest["manifest_path"]]}
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
                    f"QGIS inputs: {json.dumps(qgis_inputs, ensure_ascii=False)}\n"
                    "Merge QGIS input layers/manifests from analyses actually mapped. Keep these "
                    "original paths; use layer.derivation for subsets/fields/annotations, "
                    "not Python rewrites merely for styling.\n"
                    + ("Empty result persisted successfully.\n" if statistics["feature_count"] == 0
                       else "")
                    + ("Reference scope: " + json.dumps(manifest["reference_scope"],
                                                        ensure_ascii=False) + "\n"
                       if "reference_scope" in manifest else "")
                    + (f"Counting: {manifest['count_semantics']}"
                       if "count_semantics" in manifest else "")
                ),
                metadata={**manifest, "output_path": output_path, "qgis_inputs": qgis_inputs},
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
