"""Reproducible Python execution for spatial analysis and custom rendering."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import math
from pathlib import Path

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field, field_validator, model_validator

from geoharness.cartography.workflow import map_identity, refund_map_render, reserve_python_render
from geoharness.config.settings import CartographyConfig, ExecutionConfig, GeoConfig
from geoharness.execution import execute_python
from geoharness.invocation import invocation_guide
from geoharness.qgis.artifacts import returned_preview_paths


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
                    returned = [(value, False) for value in returned_preview_paths(payload)]
                    returned.append((payload.get("project_manifest"), True))
                    for value, project_manifest in returned:
                        if not isinstance(value, str) or not value:
                            continue
                        path = Path(value)
                        path = path if path.is_absolute() else job / path
                        path = path.absolute()
                        allowed = (path.name == "project.json" if project_manifest
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
    if "unexpected keyword argument" in stderr:
        hints.append('Inspect the installed callable signature with inspect.signature before '
                     'changing arguments. rasterio.mask.mask uses nodata, not fill_value; '
                     'do not translate keywords from numpy/pandas APIs by analogy.')
    if any(text in stderr for text in ('tuple index out of range', 'Source shape',
                                      'RasterShapeError', 'inconsistent with given indexes')):
        hints.append('Inspect ndim/shape at read, stack and write boundaries. rasterio read(1) '
                     'is 2D; another [0] removes a spatial axis. mask_raster returns '
                     '(array, transform), normally band-first (indexes=int gives 2D). RGB writers '
                     'require 3D band-first arrays and matching count/height/width.')
    if any(text in stderr for text in ('FileNotFoundError', 'does not exist in the file system',
                                      'No such file or directory')):
        hints.append('Use exact DATA_REFS/returned paths, preserving Unicode punctuation. '
                     'For prior outputs set input_refs and read INPUTS[alias]; each call has '
                     'a new working directory. Do not reconstruct filenames or run timestamps.')
    if 'Entity/geometry mismatch' in stderr:
        hints.append('Keep attributes and geometry in one GeoDataFrame while sorting/filtering. '
                     'Join new statistics with entity_join(source, table, key="unique_id"). '
                     'Do not reconstruct geometry by positional .values.')
    if any(text in stderr for text in ('cannot convert float NaN to integer',
                                      'cannot convert float infinity to integer')):
        hints.append('Check empty geometries and finite bounds before window/index conversion. '
                     'For point sampling use sample_raster_points; Point.buffer(0) is empty.')
    if 'invalid/nonfinite geometry' in stderr:
        hints.append('Inspect the returned invalid row samples in the output CRS. Validate again '
                     'after to_crs(); pre-projection validity alone is insufficient. Preserve '
                     'source IDs and diagnose collapsed parts before any explicit repair; do not '
                     'silently drop required features or relabel CRS.')
    if 'Cannot transform naive geometries' in stderr or 'Vector output has no CRS' in stderr:
        hints.append('Recover the verified source CRS from the registered file/GeoDataFrame. '
                     'Bare Shapely objects lose CRS. Do not guess a CRS merely to make to_crs run.')
    if 'ModuleNotFoundError' in stderr:
        hints.append('Use installed libraries or the native QGIS tools; unavailable optional '
                     'packages must not be assumed or installed by the analysis script.')
    if 'KeyError:' in stderr or 'not in index' in stderr:
        hints.append('Inspect the actual schema of this intermediate table, including merge '
                     'suffixes and selected columns. A source field need not survive every step.')
    if 'CRS mismatch' in stderr:
        hints.append('Transform an existing GeoDataFrame with to_crs(target); do not assign a '
                     'different crs to a CRS-bearing GeometryArray. Never relabel projected '
                     'coordinates as longitude/latitude.')
    if "NameError:" in stderr and "is not defined" in stderr:
        hints.append("Each geo_python call starts fresh: define/import every function and "
                     "variable in this script. resolve_data, read_vector, native_layer, "
                     "mask_raster, read_raster, write_raster, raster_backdrop, entity_join, "
                     "score_risk and sample_raster_points are injected; "
                     "earlier scripts do not "
                     "supply definitions to this run.")
        if "name 'print_stats' is not defined" in stderr:
            hints.append("print_stats is not provided. Call geo_raster_data action=stats as "
                         "a separate tool, or compute statistics with rasterio/numpy in Python.")
    if "UnicodeDecodeError" in stderr or "couldn't be converted correctly" in stderr:
        hints.append("Text encoding is per layer. Inspect its encoding/encoding_source or "
                     "use read_vector(name/ref), which uses the registered encoding. "
                     "Do not force UTF-8 or GBK across all shapefiles.")
    if "No vector source selected" in stderr:
        hints.append("No dataset was selected; this is not a filesystem permission failure. "
                     "Use read_vector(name/ref) or resolve_data(name/ref) from DATA_REFS. "
                     "Use the Path returned by load/inspect, not a guessed DATA_DIRS subfolder.")
    if "TypeError:" in stderr and "NoneType" in stderr and "os.PathLike" in stderr:
        hints.append("A path argument is None, possibly because directory discovery found "
                     "no match. DATA_DIRS are allowed read roots, not direct dataset folders. "
                     "Use read_vector(name/ref) or resolve_data(name/ref) from DATA_REFS; "
                     "if manually searching, check for no match before joining/reading paths. "
                     "This error does not establish an encoding or permission failure.")
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
    if 'Too few points in geometry' in stderr:
        hints.append('引线端点可能相同；先按唯一ID核对真实点与标签锚点。'
                     '使用make_leader_lines跳过零长度引线，保留原始点，不猜坐标偏移。')
    if any(text in stderr for text in ('KeyError', 'shape', 'unpack', '__round__',
                                      'truth value of a Series')):
        hints.append('先对实际中间表/数组执行describe_input(value)，核对字段、dtype、CRS或shape；'
                     '不要根据原始表或上一阶段假定字段仍存在，不要重做已成功的分析。')
    if 'Read path is outside allowed' in stderr and '.pdf' in code:
        hints.append('工程PDF通过geo_qgis_project export和geo_deliver处理；'
                     '使用实际返回的产物路径，不自行拼接工程内部文件名。')
    return hints


class InputRequirement(BaseModel):
    """Optional explicit contract for one persisted dependency."""

    model_config = {"extra": "forbid"}
    fields: list[str] = Field(default_factory=list, max_length=64)
    key: str = ''
    finite_fields: list[str] = Field(default_factory=list, max_length=64)
    layer: str = ''
    crs: str = ''
    metric: bool = False
    valid_geometry: bool = False


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
    input_refs: dict[str, str] = Field(default_factory=dict, max_length=64,
        description="Named exact DATA_REFS IDs or returned artifact paths. Injected as "
        "INPUTS[name]: Path; use for CSV/GeoPackage dependencies across fresh runs.")
    input_requirements: dict[str, InputRequirement] = Field(default_factory=dict, max_length=64,
        description="Optional requirements by input_refs alias: fields, key (unique/non-null), "
        "finite_fields, layer, crs, metric, valid_geometry. Actual files are checked before code.")
    expected_outputs: list[str] = Field(default_factory=list, max_length=64,
        description="Relative output files required for stage success; missing files fail.")
    stage_id: str = Field(default='', pattern=r'^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63})?$',
        description="Stable analysis step ID, reused after fixes; stores trusted completion.")
    depends_on: list[str] = Field(default_factory=list, max_length=32,
        description="Prior successful stage IDs whose unchanged artifacts this step requires.")

    @field_validator('expected_outputs')
    @classmethod
    def output_paths(cls, value):
        from geoharness.execution.stages import validate_expected_outputs

        return validate_expected_outputs(value)

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
        "运行完整 Python 脚本，用于自定义 GIS 分析或制图。"
        + invocation_guide("geo_python") + " "
        "可用库：GeoPandas、Shapely、pandas、numpy、rasterio、matplotlib（Agg）。"
        "原生 buffer/clip/intersection/distance/count/impact/area 优先使用 geo_analysis。"
        "注入路径：RUN_DIR（唯一写入根/当前 cwd）、SOURCE_CWD、"
        "DATA_DIRS:list[Path]（直接迭代；[0] 是工作区，不是数据集目录）。"
        "只有 os.environ['DATA_DIRS'] 是用 os.pathsep 分隔的字符串。"
        "DATA_REFS:dict[stable_id,record{name,path,ref}]；名称是值，不是 ID 键。"
        "resolve_data(ref)->Path；read_vector(ref,target_crs=None)->GeoDataFrame，"
        "使用每图层登记编码和仅 Python 可见的 __geoh_id 做稳定连接。"
        "未知、歧义或过期引用会被拒绝；绝不强制统一某个 shapefile 编码。"
        "INPUTS:dict[alias,Path] 来自 input_refs；GEO_DATA 是按路径的元数据；"
        "PREVIOUS_OUTPUTS:list[Path] 是之前产物，最新在前。"
        "助手：read_raster(path,bands=None,out_shape=(rows,columns),resampling='nearest',"
        "geometries=None)->RasterData(array masked band-first,crs,transform,nodata)；"
        "write_raster(path,RasterData or array,crs=...,transform=...,nodata=...) 写 GeoTIFF；"
        "raster_backdrop(source,output,band=1,out_shape=None,geometries=None,rgb=False) "
        "生成仅用于显示的 uint8 拉伸（零 nodata）。标准裁剪/重采样/底图工作优先使用这些助手。"
        "mask_raster(path,CRS-bearing GeoSeries/GeoDataFrame,**options)->(array,transform)；"
        "native_layer(ref,derivation=None,name=None,style=None,label_field=None,"
        "label_style=None,layer_name=None) 保留来源/派生关系；"
        "entity_join(source,table,key=...) 按身份连接；score_risk 使用显式假定权重；"
        "sample_raster_points(path,points,band=1) 处理点 CRS。"
        "extent_geometry(west=...,south=...,east=...,north=...,crs=...) 生成带 CRS 的"
        "裁剪几何。artifact_facts(path,key=...,numeric_fields=[],predicates=[],"
        "metric_crs=...) 与 check_claims(facts,claims) 只度量已落盘的显式事实，"
        "artifact_facts还支持group_by=[字段]；write_analysis_report(path.json,source=落盘文件,"
        "key=唯一ID,numeric_fields=[],group_by=[],predicates=[],method=方法字典,claims=[])"
        "生成同源JSON/中文Markdown，报告和源数据一起交付。"
        "first_coverage(points,stages,key=...,stage_key=...,stage_order=[明确时序ID],"
        "boundary='covers'或'contains')按覆盖返回first_stage_hit和coverage_status，"
        "从未覆盖不填时间，禁止用距离idxmin代替。make_leader_lines(points,labels,"
        "key=...,metric_crs=...)按身份生成非退化引线。同位置无需引线。"
        "score_risk的missing_policy=error/unknown/renormalize必须明确选择，"
        "重新归一化需要用户同意，不能推断缺人口一定低估。"
        "不证明科学有效性或全部叙述。print_stats 未注入。"
        "使用 expected_outputs/stage_id/depends_on 建立可验证阶段。返回的准确 QGIS 诊断、"
        "全部页面预览和 project.json 可读；QGIS 目录/profile 列表不可读。"
        "无网络/子进程；应用层 guard 不是 OS 沙箱。返回脚本/清单/stdout/stderr/生成路径；"
        "交付前检查实际结果并审查真实 PNG 页面。"
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
        from geoharness.data.artifacts import assert_usable_artifact
        from geoharness.data.task_context import resolve_registered_path
        from geoharness.execution.stages import prepare_stage

        inputs, stage_fingerprint = {}, ''
        try:
            if set(arguments.input_requirements) - set(arguments.input_refs):
                raise ValueError('input_requirements must name existing input_refs aliases')
            if arguments.depends_on and not arguments.stage_id:
                raise ValueError('depends_on requires stage_id')
            for alias, value in arguments.input_refs.items():
                value = resolve_registered_path(value, context.metadata,
                                                cwd=Path(context.cwd), config=config)
                path = Path(value)
                path = (path if path.is_absolute() else context.cwd / path).resolve()
                assert_usable_artifact(path)
                inputs[alias] = str(path)
            if arguments.stage_id:
                stage_fingerprint = prepare_stage(context.metadata,
                    stage_id=arguments.stage_id, dependencies=arguments.depends_on,
                    code=arguments.code + json.dumps({k: v.model_dump() for k, v in
                        arguments.input_requirements.items()}, sort_keys=True),
                    inputs=inputs, expected_outputs=arguments.expected_outputs)
        except (OSError, ValueError) as exc:
            return ToolResult(output=f'Precondition failed; no script executed: {exc}',
                              is_error=True, metadata={'script_executed': False})
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
            from geoharness.qgis.prepared import diagnostic_files as prepared_diagnostics

            diagnostic_files = (qgis_diagnostic_files(qgis_root)
                                + prepared_diagnostics(cwd=context.cwd, settings=config.qgis))
            diagnostic_files += registered_review_files(
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
                input_artifacts=inputs,
                input_requirements={k: v.model_dump() for k, v in
                                    arguments.input_requirements.items()},
                expected_outputs=tuple(arguments.expected_outputs),
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
        if arguments.stage_id:
            from geoharness.execution.stages import finish_stage

            result['stage'] = finish_stage(context.metadata, stage_id=arguments.stage_id,
                                           fingerprint=stage_fingerprint, result=result,
                                           dependencies=arguments.depends_on)
        result["execution_contract"] = {
            "call_time_cautions": invocation_guide("geo_python"),
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
            "INPUTS": "dict[alias, Path] from verified input_refs; use across calls",
            "input_requirements": "Actual schema/key/CRS checks before script execution",
            "extent_geometry": "keyword west,south,east,north,crs -> CRS-bearing GeoSeries",
            "artifact_facts": "path,key,numeric_fields,predicates,metric_crs -> measured facts",
            "check_claims": "facts,claims -> compare explicit values; no NLP audit",
            "describe_input": "actual DataFrame/ndarray/open raster -> schema, dtype, CRS, shape",
            "check_leader_lines": "points,labels,lines,key,metric_crs -> verify current anchors",
            "fit_frame_extent": "frame,metric_crs,width_mm,height_mm,padding_ratio -> fit all",
            "first_coverage": "points,stages,key,stage_key,stage_order,boundary -> 最早覆盖/未覆盖",
            "make_leader_lines": "points,labels,key,metric_crs -> 按ID连接，略去零长度引线",
            "write_analysis_report": "path,source,key,method等 -> 同源JSON/中文Markdown事实报告",
            "entity_join": "source, table, key=... -> key-joined GeoDataFrame; never positional",
            "projection": "Reproject GeoDataFrame/GeoSeries, not iterrows() pandas Series rows",
            "native_layer": "ref, derivation=None -> layer retaining original source/provenance",
            "mask_raster": "CRS-aware GeoSeries/GeoDataFrame masking -> (array, Affine transform); "
                           "default array shape=(bands, rows, columns); indexes=int gives 2D; "
                           "source data unchanged",
            "read_raster": "path,bands=None,out_shape=(rows,columns),resampling='nearest',"
                           "geometries=None -> RasterData(array masked 3D,crs,transform,nodata)",
            "write_raster": "path,RasterData or 3D array,crs=None,transform=None,nodata=... "
                            "-> GeoTIFF Path; raw arrays require CRS/transform",
            "raster_backdrop": "source,output,band=1,out_shape=None,geometries=None,"
                               "percentiles=(2,98),rgb=False,resampling='nearest' -> Path; "
                               "display stretch only, not analytical reclassification",
        }
        if status != "succeeded":
            if reserved:
                refund_map_render(identity, context.metadata)
            lines = result["stderr"].strip().splitlines()
            result["error_summary"] = lines[-1][:500] if lines else status
            result["recovery_hints"] = python_recovery_hints(arguments.code, result["stderr"])
            from geoharness.execution.stages import classify_failure

            result['failure'] = classify_failure(result['stderr'])
        # Keep the actual cause visible before long run paths/tracebacks are collapsed.
        visible = result if status == "succeeded" else {
            key: result[key] for key in ("status", "error_summary", "recovery_hints")
        } | {key: value for key, value in result.items()
             if key not in {"status", "error_summary", "recovery_hints"}}
        response = ToolResult(
            output=json.dumps(visible, ensure_ascii=False, indent=2),
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
