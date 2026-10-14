"""A small dynamic interface to the installed QGIS Processing registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoharness.config.settings import GeoConfig


class GeoQgisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["doctor", "search", "help", "run", "model"]
    query: str = Field(default="", description="Search names, IDs, groups; English terms work.")
    algorithm_id: str = Field(default="", description="Actual installed ID returned by search.")
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description="Exact parameters returned by help; outputs stay in the new task directory.",
    )
    model_path: str = Field(default="", description="Existing local QGIS .model3 processing model.")
    limit: int | None = Field(default=None, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_action(self) -> GeoQgisInput:
        if self.action in {"help", "run"} and not self.algorithm_id.strip():
            raise ValueError("help/run requires an installed algorithm_id; search first")
        if self.action == "model" and not self.model_path:
            raise ValueError("model requires a local model_path")
        if self.action not in {"run", "model"} and self.parameters:
            raise ValueError("parameters apply only to run/model")
        return self


def qgis_read_roots(config: GeoConfig, cwd: Path) -> list[Path]:
    """Existing data/output roots may be read; the worker owns all new output paths."""
    values = [config.data.file_dir]
    values.extend(
        getattr(config, section).outputs_dir
        for section in ("analysis", "execution", "cartography", "qgis")
    )
    if config.basemaps.enabled:
        cache = Path(config.basemaps.cache_dir).expanduser()
        cache = (cache if cache.is_absolute() else cwd / cache).resolve()
        if cache.is_relative_to(cwd.resolve()) and cache != cwd.resolve():
            values.append(str(cache))
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
        "Use installed QGIS Processing without opening the desktop. doctor reports actual "
        "runtime/providers; search discovers algorithms; help returns exact input/output "
        "parameters; run executes one algorithm; model runs a local .model3 workflow. "
        "Search/help before execution: never guess IDs or enum values. Inputs are local "
        "data files; originals remain read-only. All outputs, logs and provenance are "
        "persisted in a new workspace task directory. Use a suitable declared CRS and "
        "units: algorithm success does not validate the analytical method or map quality."
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

        payload = arguments.model_dump(exclude={"action"}, exclude_none=True)
        payload.setdefault("limit", config.qgis.max_search_results)
        try:
            result = await execute_qgis(
                arguments.action, payload, cwd=Path(context.cwd), settings=config.qgis,
                read_roots=qgis_read_roots(config, Path(context.cwd)),
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return ToolResult(output=f"Error: QGIS task could not run: {exc}", is_error=True)
        return ToolResult(
            output=json.dumps(result, ensure_ascii=False, indent=2),
            is_error=result.get("status") != "succeeded",
            metadata={**result, **(result.get("result") or {})},
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        return (
            isinstance(arguments, GeoQgisInput)
            and arguments.action in {"doctor", "search", "help"}
        )
