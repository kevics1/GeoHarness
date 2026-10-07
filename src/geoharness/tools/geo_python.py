"""Reproducible Python execution for spatial analysis and custom rendering."""

from __future__ import annotations

import json
from pathlib import Path

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field

from geoharness.config.settings import CartographyConfig
from geoharness.execution import execute_python


class GeoPythonInput(BaseModel):
    code: str = Field(min_length=1, description="Complete Python script saved as analysis.py.")
    name: str = Field(
        default="analysis",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        description="Short ASCII name for this reproducible analysis run.",
    )
    timeout_seconds: float | None = Field(
        default=None, gt=0, description="Optional timeout no greater than the configured maximum."
    )


class GeoPythonTool(BaseTool):
    name: str = "geo_python"
    description: str = (
        "Write and run Python for combined spatial analysis or custom maps. "
        "GeoPandas, Shapely, pandas, matplotlib (Agg) and rasterio are available. "
        "SOURCE_CWD and DATA_DIRS identify read-only source folders; RUN_DIR is the only "
        "allowed output folder and is also the working directory. "
        "Configured analysis, map and previous Python run outputs "
        "are available as read-only inputs. "
        "Use relative output filenames or RUN_DIR / filename. Network and subprocesses "
        "are unavailable. Returns script, manifest, stdout/stderr and generated file paths. "
        "Inspect results and send generated PNG maps to geo_map_review before delivery. "
        "Application guards are not an OS sandbox; run only trusted analysis code."
    )
    input_model: type[BaseModel] = GeoPythonInput

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoPythonInput)
        config = context.metadata.get("geoharness_config")
        execution = getattr(config, "execution", None)
        if execution is None or not execution.enabled:
            return ToolResult(
                output="Error: Python execution is disabled or unconfigured.", is_error=True
            )
        timeout = arguments.timeout_seconds or execution.timeout_seconds
        if timeout > execution.timeout_seconds:
            return ToolResult(
                output=f"Error: timeout exceeds configured maximum ({execution.timeout_seconds}s).",
                is_error=True,
            )
        file_dir = getattr(getattr(config, "data", None), "file_dir", "")
        directories = []
        for section in ("analysis", "cartography", "execution"):
            directory = getattr(getattr(config, section, None), "outputs_dir", "")
            if section == "cartography":
                directory = directory or CartographyConfig().outputs_dir
            if directory:
                directories.append(Path(directory).expanduser())
        read_dirs = tuple(directories)
        try:
            result = await execute_python(
                code=arguments.code,
                name=arguments.name,
                cwd=Path(context.cwd),
                data_dir=Path(file_dir).expanduser() if file_dir else None,
                outputs_dir=Path(execution.outputs_dir).expanduser(),
                timeout_seconds=timeout,
                max_output_chars=execution.max_output_chars,
                read_dirs=read_dirs,
            )
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Error: Python analysis could not run: {exc}", is_error=True)
        return ToolResult(
            output=json.dumps(result, ensure_ascii=False, indent=2),
            is_error=result["timed_out"] or result["exit_code"] != 0,
            metadata=result,
        )

    def is_read_only(self, arguments: BaseModel) -> bool:
        return False
