"""Reproducible Python execution for spatial analysis and custom rendering."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import math
from pathlib import Path

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field, model_validator

from geoharness.cartography.workflow import map_identity, refund_map_render, reserve_python_render
from geoharness.config.settings import CartographyConfig, ExecutionConfig, GeoConfig
from geoharness.execution import execute_python


def qgis_diagnostic_files(root: Path) -> tuple[Path, ...]:
    """Grant only fixed diagnostics of recent completed jobs, without following links."""
    root = root.resolve()
    if not root.is_dir():
        return ()
    files = []
    try:
        jobs = heapq.nlargest(64, (path for path in root.iterdir()
            if path.is_dir() and path.absolute() == path.resolve()),
            key=lambda path: path.stat().st_mtime_ns)
    except OSError:
        return ()
    for job in jobs:
        manifest = job / "manifest.json"
        try:
            if manifest.resolve() != manifest or manifest.stat().st_size > 1024 * 1024:
                continue
            record = json.loads(manifest.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or Path(record.get("run_dir", "")).resolve() != job:
                continue
            for name in (
                "request.json", "result.json", "manifest.json", "stdout.txt", "stderr.txt",
            ):
                path = job / name
                if path.is_file() and path.resolve() == path:
                    files.append(path)
            result_file = job / "result.json"
            if result_file in files and result_file.stat().st_size <= 1024 * 1024:
                result = json.loads(result_file.read_text(encoding="utf-8"))
                payload = result.get("result", {}) if isinstance(result, dict) else {}
                if isinstance(result, dict) and result.get("status") == "succeeded" \
                        and isinstance(payload, dict):
                    # Only exact files returned by a successful job, inside that job.
                    # Never grant its data directory, project archive, or profile.
                    for key in ("preview_path", "output_path", "project_manifest"):
                        value = payload.get(key)
                        if not isinstance(value, str) or not value:
                            continue
                        path = Path(value)
                        path = path if path.is_absolute() else job / path
                        path = path.absolute()
                        allowed = (path.name == "project.json" if key == "project_manifest"
                                   else path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
                        if allowed and path.is_relative_to(job) and path.is_file() \
                                and path.resolve() == path and path not in files:
                            files.append(path)
        except (OSError, ValueError, TypeError):
            continue
    return tuple(files)


def registered_review_files(
    root: Path, context: ToolExecutionContext, config: GeoConfig,
) -> tuple[Path, ...]:
    """Read exact, unchanged review reports recorded for this active map job."""
    from geoharness.cartography.jobs import map_job_receipt

    root = root.resolve()
    try:
        receipt = map_job_receipt(context.metadata, cwd=context.cwd, config=config)
        files = []
        for version in receipt.get("versions", [])[-8:]:
            reference = version.get("stages", {}).get("visual_checked", {}).get(
                "report_reference", {})
            value = reference.get("path")
            if not isinstance(value, str) or not isinstance(reference.get("sha256"), str):
                continue
            path = Path(value).absolute()
            if path.name != "report.json" or not path.is_relative_to(root) \
                    or path.resolve() != path or not path.is_file() \
                    or path.stat().st_size > 1024 * 1024:
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]:
                files.append(path)
        return tuple(dict.fromkeys(files))
    except (OSError, ValueError, TypeError):
        return ()


def python_recovery_hints(code: str, stderr: str) -> list[str]:
    hints = []
    if "DATA_DIRS" in code and "'list' object has no attribute 'split'" in stderr:
        hints.append("DATA_DIRS is list[Path]: iterate it directly. Only "
                     "os.environ['DATA_DIRS'].split(os.pathsep) returns environment paths.")
    if "DATA_DIRS[0]" in code and any(text in stderr for text in (
        "No such file", "does not exist", "not found", "系统找不到",
    )):
        hints.append("DATA_DIRS[0] is SOURCE_CWD, a workspace read root, not necessarily a "
                     "dataset subfolder. Use DATA_REFS and resolve_data(ref) or read_vector(ref).")
    if "'Series' object has no attribute 'to_crs'" in stderr:
        hints.append("A pandas Series (for example gdf.loc[index]) is an attribute row, not a "
                     "CRS-bearing geometry collection. Transform the GeoDataFrame or "
                     "gdf.geometry GeoSeries with to_crs(), or wrap the selected geometry in "
                     "gpd.GeoSeries([geometry], crs=gdf.crs) after verifying gdf.crs.")
    elif "object has no attribute 'to_crs'" in stderr:
        hints.append("Shapely geometry has no CRS: use "
                     "gpd.GeoSeries([geometry], crs=verified_source_crs)"
                     ".to_crs(target_crs).iloc[0]. "
                     "Verify the actual source CRS before declaring it.")
    if "Analysis may write only inside its run directory" in stderr:
        hints.append("Write to RUN_DIR / filename or a relative filename. "
                     "Use geo_deliver for workspace delivery; do not copy to workspace/fonts.")
    if "Read path is outside allowed" in stderr:
        hints.append("Read only returned QGIS diagnostic files, previews and project.json, "
                     "not the QGIS directory/profile. "
                     "Use geo_qgis_project inspect to list layouts. "
                     "For QGIS fonts prefer font_family; explicit font_file must be in authorized "
                     "data/workspace. Do not inspect system directories or tool source code.")
    if "Input shapes do not overlap raster" in stderr or "WindowError" in stderr:
        hints.append("Inspect actual raster CRS/bounds in GEO_DATA. Use "
                     "mask_raster(path, crs_bearing_geodataframe, crop=True), which transforms "
                     "geometry to the raster CRS. Never pass raw analysis-CRS Shapely shapes "
                     "to rasterio.mask on a different-CRS raster.")
    if "RUN_DIR" in code and any(text in stderr for text in (
        "No such file", "does not exist", "not found", "系统找不到",
    )):
        hints.append("RUN_DIR is a NEW output directory on every call. Use a returned actual "
                     "path or PREVIOUS_OUTPUTS for earlier artifacts; do not invent timestamps "
                     "or read earlier files from the current RUN_DIR.")
    return hints


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
        "Injected: SOURCE_CWD: Path, RUN_DIR: Path, DATA_DIRS: list[Path] (iterate directly). "
        "DATA_DIRS[0] is SOURCE_CWD, a workspace read root; do not assume a dataset subfolder. "
        "Only os.environ['DATA_DIRS'] is an os.pathsep-separated string. "
        "DATA_REFS is dict[stable_id, record], where each record has name, path and ref; "
        "names are values, not substrings of the opaque ID keys. "
        "Use read_vector('a unique registered name') directly, without a custom name lookup. "
        "Stable IDs bind measured sources and actual producer outputs; "
        "resolve_data(ref) returns their original Path and read_vector(ref, target_crs=None) "
        "reads vectors with optional CRS transformation. Unique registered names/paths work; "
        "unknown, ambiguous, changed or deleted refs are rejected. native_layer(ref, "
        "derivation=None, name=None, style=None, label_field=None, label_style=None, "
        "layer_name=None) creates a native layer specification retaining its original source "
        "path; use derivation for subsets so analysis provenance can still bind. "
        "GEO_DATA: dict[path, verified metadata]; PREVIOUS_OUTPUTS: list[Path] contains "
        "existing artifacts, newest jobs first. Each call creates a NEW RUN_DIR; earlier "
        "files must use their returned actual paths, never a guessed dated directory. "
        "mask_raster(path, GeoSeries/GeoDataFrame, crop=True) checks and transforms geometry "
        "to the actual raster CRS before masking; bare Shapely lists are rejected. "
        "It returns (array, transform), like rasterio.mask: unpack both values; array has "
        "shape (bands, rows, columns), not a raster dataset with .crs/.profile. "
        "Reproject a GeoDataFrame/GeoSeries before iterrows(); a pandas Series row has no "
        "to_crs(). Use geo_vector_data/geo_raster_data list and inspect to establish refs. "
        "Configured analysis, map and previous Python run outputs "
        "are read-only. Returned QGIS diagnostics, previews and project.json are readable; "
        "QGIS directory listing/profile access is not allowed. Shapely geometries have no CRS; "
        "transform them via gpd.GeoSeries([geometry], crs=verified_source_crs).to_crs(target_crs). "
        "Prefer geo_analysis for buffer/clip/intersection/distance/count/impact/area; "
        "for other installed GIS operations use geo_qgis search then help then run. "
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
        reserve_python_render(
            code=arguments.code,
            name=arguments.name,
            map_id=arguments.map_id,
            metadata=context.metadata,
            config=config,
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
        qgis_directory = getattr(getattr(config, "qgis", None), "outputs_dir", "")
        diagnostic_files = ()
        if qgis_directory:
            qgis_root = Path(qgis_directory).expanduser()
            qgis_root = qgis_root if qgis_root.is_absolute() else context.cwd / qgis_root
            diagnostic_files = qgis_diagnostic_files(qgis_root) + registered_review_files(
                qgis_root, context, config)
        try:
            from geoharness.data.profiles import current_profiles
            from geoharness.data.task_context import data_ref_snapshot, recent_outputs

            profiles = current_profiles(context.metadata)
            outputs = recent_outputs(context.cwd, config)
            references = data_ref_snapshot(context.metadata, cwd=context.cwd, config=config)
            result = await execute_python(
                code=arguments.code,
                name=arguments.name,
                cwd=Path(context.cwd),
                data_dir=Path(file_dir).expanduser() if file_dir else None,
                outputs_dir=Path(execution.outputs_dir).expanduser(),
                timeout_seconds=timeout,
                max_output_chars=execution.max_output_chars,
                read_dirs=read_dirs,
                read_files=diagnostic_files,
                data_profiles=profiles,
                data_refs=references,
                previous_outputs=tuple(Path(item["path"]) for item in outputs),
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
        result["execution_contract"] = {
            "SOURCE_CWD": "Path", "RUN_DIR": "Path", "DATA_DIRS": "list[Path]",
            "DATA_DIRS_environment": "str separated by os.pathsep",
            "DATA_DIRS_first": "SOURCE_CWD workspace read root; not necessarily a dataset folder",
            "data_root": str((Path(file_dir).expanduser() if Path(file_dir).expanduser()
                              .is_absolute() else context.cwd / (file_dir or ".")).resolve()),
            "output_directory": result.get("run_dir"),
            "qgis_diagnostics": "Exact returned diagnostics, preview images and project.json",
            "GEO_DATA": "dict[path, verified metadata]", "PREVIOUS_OUTPUTS": "list[Path]",
            "DATA_REFS": "Same frozen ref records provided to the model; stale refs rejected",
            "resolve_data": "ref -> original Path; stable ID or unique registered name/path",
            "read_vector": "ref, target_crs=None -> GeoDataFrame; no guessed source CRS",
            "projection": "Reproject GeoDataFrame/GeoSeries, not iterrows() pandas Series rows",
            "native_layer": "ref, derivation=None -> layer retaining original source/provenance",
            "mask_raster": "CRS-aware GeoSeries/GeoDataFrame masking -> (array, Affine transform); "
                           "array shape=(bands, rows, columns); source data unchanged",
        }
        if status != "succeeded":
            if reserved:
                refund_map_render(identity, context.metadata)
            lines = result["stderr"].strip().splitlines()
            result["error_summary"] = lines[-1][:500] if lines else status
            result["recovery_hints"] = python_recovery_hints(arguments.code, result["stderr"])
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
