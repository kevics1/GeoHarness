"""A small dynamic interface to the installed QGIS Processing registry."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoharness.config.settings import GeoConfig


class GeoQgisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["doctor", "search", "help", "run", "model"]
    query: str = Field(default="", description="搜索算法名称、ID、分组；支持中文与英文关键词。")
    algorithm_id: str = Field(default="", description="search 返回的实际已安装算法 ID，不能猜测。")
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="help 返回的准确参数；输出只写入本次独立任务目录。",
    )
    model_path: str = Field(default="", description="现有的本地 QGIS .model3 处理模型。")
    limit: int | None = Field(default=None, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_action(self) -> GeoQgisInput:
        if self.action in {"help", "run"} and not self.algorithm_id.strip():
            raise ValueError("help/run 需要已安装的 algorithm_id；请先 search")
        if self.action == "model" and not self.model_path:
            raise ValueError("model 需要本地 model_path")
        if self.action not in {"run", "model"} and self.parameters:
            raise ValueError("parameters 仅适用于 run/model")
        return self

def qgis_read_roots(config: GeoConfig, cwd: Path) -> list[Path]:
    """Existing data/output roots may be read; the worker owns all new output paths."""
    values = [config.data.file_dir]
    values.extend(
        getattr(config, section).outputs_dir
        for section in ("analysis", "execution", "cartography", "qgis")
    )
    roots = [cwd.resolve()]
    for value in values:
        if value:
            path = Path(value).expanduser()
            path = (path if path.is_absolute() else cwd / path).resolve()
            if path not in roots:
                roots.append(path)
    return roots

class GeoQgisTool(BaseTool):
    name = "geo_qgis"
    description = (
        "QGIS Processing 是本系统标准空间分析入口（相当于 QGIS 工具箱），不是制图工具。"
        "doctor 检查实际运行时/提供者；search 发现算法；help 返回准确参数；run 执行一个算法；"
        "model 执行本地 .model3 工作流。执行前必须 search/help，禁止猜 ID、枚举索引或单位。"
        "输入是本地数据文件，原文件只读；输出、日志、来源写入独立任务目录。"
        "run/model 成功只证明算法执行；用 geo_task(action=publish_analysis) 发布统一结果包，"
        "从实际输出复算实体、选集与统计，再用于正式制图和交付。"
        "需要 PyQGIS 控制台/自定义处理时使用 geo_python(backend=qgis)。"
    )
    input_model = GeoQgisInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoQgisInput)
        config = context.metadata.get("geoharness_config")
        if config is None:
            return ToolResult(output="Error: GeoHarness config is missing.", is_error=True)
        if not config.qgis.enabled:
            return ToolResult(output="Error: QGIS integration is disabled.", is_error=True)
        from geoharness.qgis.runner import execute_qgis

        payload = arguments.model_dump(
            exclude={"action"}, exclude_none=True
        )
        payload.setdefault("limit", config.qgis.max_search_results)
        try:
            result = await execute_qgis(
                arguments.action, payload, cwd=Path(context.cwd), settings=config.qgis,
                read_roots=qgis_read_roots(config, Path(context.cwd)),
            )
            if arguments.action in {"run", "model"}:
                result["analysis_status"] = "execution_only"
                if result.get("status") == "succeeded":
                    result["next_action"] = _publication_hint(result)
        except (OSError, ValueError, RuntimeError) as exc:
            return ToolResult(output=f"Error: QGIS task could not run: {exc}", is_error=True)
        visible = _visible_result(result, action=arguments.action)
        return ToolResult(
            output=json.dumps(visible, ensure_ascii=False, indent=2),
            is_error=result.get("status") != "succeeded",
            metadata={**result, **(result.get("result") or {})},
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        return (
            isinstance(arguments, GeoQgisInput)
            and arguments.action in {"doctor", "search", "help"}
        )

def _publication_hint(result: dict[str, Any]) -> dict[str, Any]:
    """Return exact persisted outputs a model can publish, without guessing paths."""
    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    candidates = []
    for name, value in (payload.get("outputs") or {}).items():
        values = value if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, str):
                continue
            path = Path(item)
            if path.is_file() and path.suffix.lower() in {
                    ".csv", ".geojson", ".gpkg", ".shp", ".json"}:
                candidates.append({"name": str(name), "source_ref": str(path)})
    return {
        "required": "publish_analysis",
        "source_refs": candidates,
        "next_tool": "geo_task",
        "next_arguments": {"action": "publish_analysis", "source_ref": "<source_ref>",
            "facts": {"key": "<actual unique non-null entity field>",
                      "numeric_fields": ["<actual numeric fields>"],
                      "scope_name": "<this exact selection>", "units": {},
                      "method": {"operation": "<actual QGIS algorithm>",
                                 "algorithm_id": payload.get("algorithm_id", "")}}},
        "note": "run/model 成功仅代表算法执行；发布时系统会从落盘输出复算实体集合与统计。"
                "不要从 stdout 解读数值，也不要用未登记副本冒充结果。"}

def _visible_result(result: dict[str, Any], *, action: str) -> dict[str, Any]:
    """Keep discovery complete; shorten only persisted execution details."""
    if action not in {"run", "model"}:
        return deepcopy(result)
    compact = {key: deepcopy(result[key]) for key in (
        "status", "error_summary", "run_dir", "manifest_path", "result_path",
        "stdout_path", "stderr_path", "exit_code", "analysis_status", "next_action",
    ) if key in result}
    payload = result.get("result")
    if isinstance(payload, dict):
        compact["result"] = {
            key: deepcopy(payload[key]) for key in (
                "algorithm_id", "parameters", "outputs", "layers", "feedback",
            ) if key in payload
        }
    for key in ("stdout", "stderr"):
        if isinstance(result.get(key), str) and result[key]:
            compact[key] = result[key][-2000:]
            compact[key + "_truncated"] = (
                bool(result.get(key + "_truncated")) or len(result[key]) > 2000)
    return compact
