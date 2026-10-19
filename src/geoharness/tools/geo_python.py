"""Reproducible Python execution for spatial analysis and custom rendering."""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import math
from pathlib import Path
from typing import Any, Literal

from openharness.tools.base import BaseTool, ToolExecutionContext, ToolResult
from pydantic import BaseModel, Field, field_validator, model_validator

from geoharness.cartography.workflow import map_identity, refund_map_render, reserve_python_render
from geoharness.config.settings import CartographyConfig, ExecutionConfig, GeoConfig
from geoharness.execution import execute_python
from geoharness.invocation import invocation_guide
from geoharness.qgis.artifacts import returned_preview_paths
from geoharness.tools.geo_qgis import qgis_read_roots


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
                     "Use geo_qgis_project(action=inspect, project_path=...) only for an "
                     "existing/final editable project; it cannot build a new map. "
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
        hints.append('已有/最终工程的PDF用geo_qgis_project(action=inspect)读取；'
                     '普通制图用geo_cartography原生渲染并geo_deliver交付，'
                     '使用实际返回的产物路径，不自行拼接工程内部文件名。')
    return hints


class InputRequirement(BaseModel):
    """Call-time contract for one persisted input (checked before code runs)."""

    model_config = {"extra": "forbid"}
    fields: list[str] = Field(default_factory=list, max_length=64, description=(
        '必须存在的字段名列表，默认空；从实际数据检查结果选取。'
    ))
    key: str = Field(default="", description=(
        '唯一且非空的业务实体ID字段，默认空表示不检查唯一性。'
    ))
    finite_fields: list[str] = Field(default_factory=list, max_length=64, description=(
        '必须全部为有限数值的字段列表，拒绝空值、NaN及inf。'
    ))
    layer: str = Field(default="", description=(
        '多图层数据的准确图层名，默认空使用驱动默认层。'
    ))
    crs: str = Field(default="", description=(
        '预期CRS标识，例如EPSG:4326；默认空不限制，检查不重标坐标。'
    ))
    metric: bool = Field(default=False, description=(
        '默认false；true要求投影且单位为米，适用于距离/面积/缓冲。'
    ))
    valid_geometry: bool = Field(default=False, description=(
        '默认false；true在脚本运行前检查所有几何非空且有效。'
    ))


class GeoPythonInput(BaseModel):
    model_config = {
        "extra": "forbid",
        "json_schema_extra": {
            "title": "geo_python input",
            "examples": [{
                "code": ("import geopandas as gpd\n"
                         "frame = gpd.read_file(INPUTS['source'])\n"
                         "print(frame.crs, len(frame))\n"),
                "name": "inspect_source",
                "input_refs": {"source": "DATA_REF_OR_RETURNED_PATH"},
                "input_requirements": {"source": {"fields": ["id"], "key": "id"}},
                "expected_outputs": [],
            }],
        },
    }

    code: str = Field(min_length=1, description=(
        "必填。本次调用要完整执行的 Python 脚本正文，保存为 analysis.py。"
        "必须自包含全部 import/函数/变量；每次调用都是新进程，上一调用的名称不会保留。"
        "不能只给片段，也不能用省略号或占位路径。读取数据用 INPUTS[alias]、read_vector(...) "
        "或本轮实际返回的路径；写文件只用 RUN_DIR / 相对文件名。"
        "示例：code=\"import geopandas as gpd\npts=read_vector('居民点')\n"
        "print(pts[['id','name']])\"。"
    ))
    name: str = Field(
        default="analysis",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        description=(
            "可选。ASCII 短名，仅用于标识本次可复现运行/产物目录，不参与空间计算。"
            "重复名称不会自动复用旧结果；同一阶段或缺陷修订请使用相同 stage_id。"
        ),
    )
    map_id: str = Field(
        default="",
        description=(
            "可选。逻辑地图的稳定标识，不是 operation_id、run_dir 或文件名。"
            "同一张图的多版本复用同一 map_id；只上传一张新图时才新生成。"
        ),
    )
    timeout_seconds: float | None = Field(
        default=None, gt=0, description=(
            "可选。单次脚本超时秒数，必须不超过 execution.timeout_seconds 配置上限；"
            "它不是整体任务预算，也不会让长分析自动通过。"
        )
    )
    input_refs: dict[str, str] = Field(default_factory=dict, max_length=64,
        description=(
            "可选，跨新进程读取业务数据的唯一推荐契约：{alias: exact_ref}。"
            "alias 是脚本里的 INPUTS['alias'] 键；值是工具实际返回的 data:... 引用、"
            "明确路径或已登记结果路径，不能猜文件名。"
            "脚本内的动态/绝对业务读取必须在此声明，否则执行前会以'未绑定业务输入'拒绝。"
            "示例：{\"source\":\"data:...\",\"roster\":\"<previous file path>\"}。"
        ))
    input_requirements: dict[str, InputRequirement] = Field(default_factory=dict, max_length=64,
        description=(
            "可选但推荐。按 input_refs 的 alias 声明执行前检查：fields（实际列）、"
            "key（唯一且非空）、finite_fields（有限数值）、layer（GPKG 图层）、crs（声明 CRS）、"
            "metric（米制）、valid_geometry（有效几何）。检查失败不会执行脚本。"
            "这些检查只核对 schema/几何，不证明任意方法正确。"
        ))
    expected_outputs: list[str] = Field(default_factory=list, max_length=64,
        description=(
            "可选。脚本必须产生的相对输出文件列表（相对 RUN_DIR），缺一即失败。"
            "只填真正需要的最终文件，不要列出临时缓存；名称必须与脚本实际写入完全一致。"
        ))
    stage_id: str = Field(default='', pattern=r'^(?:[A-Za-z0-9][A-Za-z0-9_-]{0,63})?$',
        description=(
            "可选。稳定分析阶段 ID；修复同一阶段时保留原 ID，未变化的成功阶段可复用。"
            "不要为每次试错生成新 ID，也不要用它表示审图页号或地图版本。"
        ))
    depends_on: list[str] = Field(default_factory=list, max_length=32,
        description=(
            "可选。前置 stage_id 列表，表示本阶段确实依赖其已成功产物；"
            "只有上游指纹不变才复用。不要列出无关地图/报告步骤来堆叠依赖。"
        ))
    policy_ref: str = Field(default='', description=(
        "可选。用户已经确认的评分政策引用（policy_ref），只能来自真实确认流程；"
        "模型不能自签，普通分析不要填。"
    ))
    backend: Literal['python', 'qgis'] = Field(default='python', description=(
        "可选，默认 python。python = GeoPandas/Shapely/pandas/numpy/rasterio/matplotlib 脚本；"
        "qgis = 已初始化的独立 PyQGIS 控制台进程，可直接 import processing/qgis.core 并使用 "
        "Project。标准空间算法仍优先 geo_qgis Processing；qgis 模式适合自定义原生组合。"
        "每次调用都是新进程，不保留隐式全局变量。"
    ))
    console_state: dict[str, Any] = Field(default_factory=dict, max_length=64,
        description=(
            "backend=qgis 时可选。显式作为 CONSOLE_STATE 注入的有界 JSON 对象；"
            "只放简单标量/列表/字典，不传 QGIS 对象、DataFrame 或路径授权。"
            "不传则本次脚本没有跨调用状态。"
        ))
    console_result: str = Field(default="CONSOLE_RESULT", max_length=80, pattern=(
        r"^[A-Za-z_][A-Za-z0-9_]{0,79}$"), description=(
        "两个backend共用的可选返回变量名，默认 CONSOLE_RESULT；脚本可给该变量赋值，"
        "该值必须可 JSON 序列化，不能是 QGIS 原生对象、DataFrame 或不可序列化异常。"
        "业务结果在result.console_result返回，不必创建ok.json；print仅放少量诊断。"
    ))

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
        if isinstance(value, dict) and value.get("console_state") is not None \
                and not isinstance(value.get("console_state"), dict):
            raise ValueError("console_state must be a JSON object")
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
        "运行完整 Python 脚本，用于自定义 GIS 分析或制图。backend=qgis 时在已初始化的"
        "独立 QGIS Python 解释器中运行（PyQGIS 控制台），可直接使用 processing/qgis.core/Project；"
        "console_state 是显式 JSON，不保留隐式跨调用变量。"
        "标准分析优先 geo_qgis 的 Processing 算法。"
        + invocation_guide("geo_python") + " "
        "可用库：GeoPandas、Shapely、pandas、numpy、rasterio、matplotlib（Agg）。"
        "标准空间算法优先geo_qgis，兼容已有geo_analysis入口。"
        "注入路径：RUN_DIR（唯一写入根/当前 cwd）、SOURCE_CWD、"
        "DATA_DIRS:list[Path]（直接迭代；[0] 是工作区，不是数据集目录）。"
        "只有 os.environ['DATA_DIRS'] 是用 os.pathsep 分隔的字符串。"
        "下列GeoPandas/栅格/事实助手仅backend=python注入；backend=qgis使用"
        "Project、processing、INPUTS、RUN_DIR和显式导入的PyQGIS API。"
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
        "nearest_distance(source,reference,key=真实ID,field=新距离字段,metric_crs=米制投影)"
        "返回保留原CRS/索引的完整实体；verify_nearest_distances(source,reference,key=...,"
        "field=...,metric_crs=...,absolute_tolerance=0.11)逐实体独立复核米制最近距离。"
        "禁止将set_index(业务ID)后的最近邻Series直接赋给RangeIndex；"
        "最近邻属性用entity_join按真实ID关联，等距并列需明确选择政策。"
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
            "可选，单次脚本超时秒数；省略使用当前 backend 的配置默认值。"
            f"接口上限为 {self.timeout_limit:g} 秒，QGIS 还受 qgis.timeout_seconds 约束。"
        )
        for option in timeout["anyOf"]:
            if option.get("type") == "number":
                option["maximum"] = self.timeout_limit
        return schema

    async def _execute_console(self, arguments: GeoPythonInput,
                              context: ToolExecutionContext) -> ToolResult:
        """Run an explicit PyQGIS console script in the independent QGIS interpreter."""
        config = context.metadata.get("geoharness_config")
        if config is None or not getattr(config.qgis, "enabled", False):
            return ToolResult(output="PyQGIS console requires enabled QGIS integration.",
                              is_error=True, metadata={"script_executed": False})
        from geoharness.data.artifacts import assert_usable_artifact
        from geoharness.data.task_context import resolve_registered_path
        from geoharness.qgis.runner import execute_qgis

        inputs = {}
        try:
            if arguments.stage_id or arguments.depends_on or arguments.policy_ref:
                raise ValueError('PyQGIS控制台的阶段依赖请用geo_task plan；'
                                 '评分请用确认后的score入口')
            if set(arguments.input_requirements) - set(arguments.input_refs):
                raise ValueError("input_requirements must name existing input_refs aliases")
            for alias, value in arguments.input_refs.items():
                value = resolve_registered_path(value, context.metadata,
                                                cwd=Path(context.cwd), config=config)
                path = Path(value)
                path = (path if path.is_absolute() else context.cwd / path).resolve()
                from geoharness.execution.contracts import validate_input

                assert_usable_artifact(path)
                if alias in arguments.input_requirements:
                    validate_input(path, arguments.input_requirements[alias].model_dump())
                inputs[alias] = str(path)
        except (OSError, ValueError) as exc:
            return ToolResult(output=f"Precondition failed; no console script executed: {exc}",
                              is_error=True, metadata={"script_executed": False})
        timeout = min(arguments.timeout_seconds or config.qgis.timeout_seconds,
                      config.qgis.timeout_seconds)
        try:
            result = await execute_qgis(
                "console", {"code": arguments.code, "inputs": inputs,
                            "console_state": arguments.console_state,
                            "console_result": arguments.console_result},
                cwd=Path(context.cwd), settings=config.qgis,
                read_roots=qgis_read_roots(config, Path(context.cwd)),
                timeout_seconds=timeout,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            return ToolResult(output=f"PyQGIS console could not run: {exc}", is_error=True,
                              metadata={"script_executed": False})
        if result.get("status") != "succeeded":
            return ToolResult(output=json.dumps({
                "status": result.get("status"), "error_summary": result.get("error_summary"),
                "run_dir": result.get("run_dir"), "stderr": result.get("stderr", "")[-4000:],
                "execution_scope": "PyQGIS console; QGIS initialized",
            }, ensure_ascii=False, indent=2), is_error=True, metadata=result)
        payload = result.get("result") or {}
        files = []
        run_dir = Path(result.get("run_dir", "")).resolve()
        for path in sorted(run_dir.rglob("*")) if run_dir.is_dir() else []:
            if (path.is_file() and path.resolve().is_relative_to(run_dir)
                    and path.relative_to(run_dir).parts[0] != ".profile"
                    and path.name not in {"request.json", "result.json", "manifest.json",
                                          "stdout.txt", "stderr.txt", ".start"}):
                files.append({"path": str(path), "size_bytes": path.stat().st_size,
                              "state": "succeeded",
                              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
        missing = [name for name in arguments.expected_outputs
                   if not (run_dir / name).is_file()]
        declared = payload.get('console_result')
        if missing:
            from openharness.utils.fs import atomic_write_text

            manifest_path = Path(result['manifest_path'])
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            manifest.update(status='failed', error_summary=f'Expected outputs missing: {missing}')
            for item in manifest.get('files', []):
                item.update(state='partial', usable=False)
            atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))
            return ToolResult(output=json.dumps({
                "status": "failed", "execution_status": "completed",
                "error_summary": f"console expected outputs missing: {missing}",
                "run_dir": str(run_dir), "files": files,
                "script_executed": True, "console": True,
            }, ensure_ascii=False, indent=2), is_error=True,
                metadata={**result, "execution_status": "completed",
                          "script_executed": True, "console": True,
                          "files": files, "inputs": inputs})
        visible = {key: result[key] for key in (
            'status', 'run_dir', 'manifest_path', 'stdout_path', 'stderr_path', 'stdout', 'stderr')}
        visible.update(result=payload, inputs=inputs, files=files, backend='qgis')
        return ToolResult(output=json.dumps(visible, ensure_ascii=False, indent=2),
                          metadata={**result, "script_executed": True,
                                    "console": True, "inputs": inputs, "files": files,
                                    "console_result_value": declared,
                                    "console_result_name": arguments.console_result})

    async def execute(self, arguments: BaseModel, context: ToolExecutionContext) -> ToolResult:
        assert isinstance(arguments, GeoPythonInput)
        config = context.metadata.get("geoharness_config")
        execution = getattr(config, "execution", None)
        if execution is None or not execution.enabled:
            return ToolResult(
                output="Error: Python execution is disabled or unconfigured.", is_error=True
            )
        if arguments.backend == 'qgis':
            return await self._execute_console(arguments, context)
        if arguments.console_state:
            return ToolResult(output="console_state requires backend=qgis", is_error=True,
                              metadata={"script_executed": False})
        from geoharness.data.artifacts import assert_usable_artifact
        from geoharness.data.task_context import resolve_registered_path
        from geoharness.execution.stages import prepare_stage

        inputs, stage_fingerprint = {}, ''
        input_evidence_files = []
        approved_policy = None
        try:
            if arguments.policy_ref:
                from geoharness.analysis.policy import approved_score_policy

                approved_policy = approved_score_policy(context.metadata, arguments.policy_ref)
            if context.metadata.get('geo_task_record'):
                from geoharness.execution.managed_inputs import bind_managed_inputs

                arguments = bind_managed_inputs(arguments, context)
            if set(arguments.input_requirements) - set(arguments.input_refs):
                raise ValueError('input_requirements must name existing input_refs aliases')
            if arguments.depends_on and not arguments.stage_id:
                raise ValueError('depends_on requires stage_id')
            for alias, value in arguments.input_refs.items():
                value = resolve_registered_path(value, context.metadata,
                                                cwd=Path(context.cwd), config=config)
                path = Path(value)
                path = (path if path.is_absolute() else context.cwd / path).resolve()
                producer = assert_usable_artifact(path)
                if producer and producer.get('manifest'):
                    input_evidence_files.append(Path(producer['manifest']))
                inputs[alias] = str(path)
            if arguments.stage_id:
                stage_fingerprint = prepare_stage(context.metadata,
                    stage_id=arguments.stage_id, dependencies=arguments.depends_on,
                    code=arguments.code + json.dumps({k: v.model_dump() for k, v in
                        arguments.input_requirements.items()}, sort_keys=True),
                    inputs=inputs, expected_outputs=arguments.expected_outputs)
        except (OSError, ValueError, SyntaxError) as exc:
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
                read_files=(*diagnostic_files, *input_evidence_files),
                data_profiles=profiles,
                data_refs=references,
                previous_outputs=tuple(Path(item["path"]) for item in outputs),
                input_artifacts=inputs,
                managed_inputs=bool(context.metadata.get('geo_task_record')),
                approved_score_policy=approved_policy,
                result_variable=arguments.console_result,
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
            "nearest_distance": "source,reference,key,field,metric_crs -> 按ID关联的米制最近距离",
            "verify_nearest_distances": "source,reference,key,field,metric_crs,"
                "absolute_tolerance=0.11 -> 独立几何复核，含错值/空值数量和至多5个实体诊断",
            "projection": "Reproject GeoDataFrame/GeoSeries, not iterrows() pandas Series rows",
            "native_layer": "ref, derivation=None -> layer retaining original source/provenance",
            "mask_raster": "CRS-aware GeoSeries/GeoDataFrame masking -> (array, Affine transform); "
                           "default array shape=(bands, rows, columns); indexes=int gives 2D; "
                           "source data unchanged",
            "read_raster": "path,bands=None,out_shape=(rows,columns),resampling='nearest',"
                           "geometries=None -> RasterData(array masked 3D,crs,transform,nodata)",
            "write_raster": "path,RasterData or 3D array,crs=None,transform=None,nodata=... "
                            "-> GeoTIFF Path; raw arrays require CRS/transform",
            "backend": "qgis uses PyQGIS interpreter; console_result names JSON return",
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
