"""Reproducible Python execution for spatial analysis and custom rendering."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field, model_validator

from geoharness.cartography.workflow import map_identity, refund_map_render, reserve_python_render
from geoharness.config.settings import CartographyConfig, ExecutionConfig
from geoharness.execution import execute_python


class GeoPythonInput(BaseModel):
    code: str = Field(min_length=1, description="Complete Python script saved as analysis.py.")
    name: str = Field(
        default="analysis",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        description="Short ASCII name for this reproducible analysis run.",
    )
    map_id: str = Field(
        default="",
        description="Stable map identity shared by all versions of the same map.",
    )
    timeout_seconds: float | None = Field(
        default=None, gt=0, description="Optional timeout no greater than the configured maximum."
    )

    @model_validator(mode="before")
    @classmethod
    def require_complete_code(cls, value):
        if isinstance(value, dict) and value.get("_geoharness_argument_error"):
            diagnostic = value["_geoharness_argument_error"]
            raise ValueError(
                "geo_python arguments rejected; no script was executed. "
                + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True)
                + ". Send a shorter complete script using the configured timeout."
            )
        if isinstance(value, dict) and (
            not value.get("code")
            or isinstance(value.get("code"), str)
            and not value["code"].strip()
        ):
            raise ValueError(
                "geo_python requires complete 'code' arguments; no script was executed. "
                "Empty arguments can result from an incomplete/invalid provider tool call. "
                "Send a shorter complete script, or use the native GIS/cartography tools."
            )
        return value


class GeoPythonTool(BaseTool):
    name: str = "geo_python"
    description: str = (
        "Write and run Python for combined spatial analysis or custom maps. "
        "GeoPandas, Shapely, pandas, matplotlib (Agg) and rasterio are available. "
        "SOURCE_CWD and DATA_DIRS identify read-only source folders; RUN_DIR is the only "
        "allowed output folder and is also the working directory. "
        "These paths are injected as Python Path globals and environment variables; "
        "the DATA_DIRS environment variable uses os.pathsep-separated paths. "
        "Configured analysis, map and previous Python run outputs "
        "are available as read-only inputs. "
        "Use relative output filenames or RUN_DIR / filename. Network and subprocesses "
        "are unavailable. Returns script, manifest, stdout/stderr and generated file paths. "
        "Inspect results and send generated PNG maps to geo_map_review before delivery. "
        "Application guards are not an OS sandbox; run only trusted analysis code."
    )
    input_model: type[BaseModel] = GeoPythonInput

    def __init__(self, *, timeout_limit: float | None = None) -> None:
        self.timeout_limit = (
            ExecutionConfig().timeout_seconds if timeout_limit is None else timeout_limit
        )
        if not math.isfinite(self.timeout_limit) or self.timeout_limit <= 0:
            raise ValueError("timeout_limit must be finite and positive")

    def to_api_schema(self) -> dict:
        schema = super().to_api_schema()
        timeout = schema["input_schema"]["properties"]["timeout_seconds"]
        timeout["description"] = (
            "Optional timeout; omit it to use the configured default. "
            f"Maximum allowed value is {self.timeout_limit:g} seconds."
        )
        for option in timeout["anyOf"]:
            if option.get("type") == "number":
                option["maximum"] = self.timeout_limit
        return schema

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
                output=(
                    "Error: timeout exceeds configured maximum "
                    f"(requested={timeout:g}s, allowed={execution.timeout_seconds:g}s); "
                    "no script was executed. Omit timeout_seconds to use the default."
                ),
                is_error=True,
                metadata={
                    "requested_timeout_seconds": timeout,
                    "allowed_timeout_seconds": execution.timeout_seconds,
                    "script_executed": False,
                },
            )
        identity = arguments.map_id or map_identity(arguments.name)
        state = context.metadata.get("map_workflow_state", {})
        previous_renders = state.get("maps", {}).get(identity, {}).get("renders", 0)
        budget_stop = reserve_python_render(
            code=arguments.code,
            name=arguments.name,
            map_id=arguments.map_id,
            metadata=context.metadata,
            config=config,
        )
        if budget_stop is not None:
            return ToolResult(
                output=budget_stop,
                is_error=False,
                metadata={"render_budget_exhausted": True},
            )
        reserved = (
            context.metadata.get("map_workflow_state", {})
            .get("maps", {})
            .get(identity, {})
            .get("renders", 0)
            > previous_renders
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
            if reserved:
                refund_map_render(identity, context.metadata)
            return ToolResult(output=f"Error: Python analysis could not run: {exc}", is_error=True)
        except asyncio.CancelledError:
            if reserved:
                refund_map_render(identity, context.metadata)
            raise
        status = (
            "timed_out"
            if result["timed_out"]
            else ("succeeded" if result["exit_code"] == 0 else "failed")
        )
        result = {"status": status, **result}
        if status != "succeeded":
            if reserved:
                refund_map_render(identity, context.metadata)
            lines = result["stderr"].strip().splitlines()
            result["error_summary"] = lines[-1][:500] if lines else status
        response = ToolResult(
            output=json.dumps(result, ensure_ascii=False, indent=2),
            is_error=result["timed_out"] or result["exit_code"] != 0,
            metadata=result,
        )
        if status == "succeeded" and reserved:
            from geoharness.cartography.tracking import bind_map_job, track_render_result

            images = [
                Path(item["path"])
                for item in result.get("files", [])
                if isinstance(item, dict)
                and Path(item.get("path", "")).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            ]
            if images:
                # One execution can include independent figures/attachments.
                # Register its final candidate once; selecting another image
                # for review remains explicit, rather than inventing revisions.
                image = max(images, key=lambda path: path.stat().st_mtime_ns)
                try:
                    job_id = bind_map_job(context, identity, image)
                except (OSError, ValueError) as exc:
                    return ToolResult(
                        output=response.output + f"\nMap task registration failed: {exc}",
                        is_error=True,
                        metadata=result,
                    )
                tracked = await asyncio.to_thread(
                    track_render_result,
                    ToolResult(output="", metadata={"output_path": str(image)}),
                    context,
                    job_id,
                )
                if tracked.is_error:
                    return tracked
                response = ToolResult(
                    output=response.output + tracked.output,
                    metadata={
                        **result,
                        "map_id": job_id,
                        "primary_map": str(image),
                        "map_candidates": [str(path) for path in images],
                        "map_receipt": tracked.metadata["map_receipt"],
                    },
                )
        return response

    def is_read_only(self, arguments: BaseModel) -> bool:
        return False
