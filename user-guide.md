# GeoHarness 使用指南

当前优先完成多图层叠加与影响范围分析，支持原生 GIS 和自主 Python 分析，并读取成图进行检查、修正和重绘。空间热点与栅格/地形是后续重点。

## 配置与启动

`config.example.yaml` 使用本轮已验证的服务：

```yaml
base_url: http://localhost:20128/v1
model: cbcn/deepseek-v4.1-flash(max)
api_key: local
```

local 是无鉴权本地网关的占位值。启用鉴权时改成 `${GEOH_API_KEY}`，在配置同目录 `.env` 设置密钥，不提交 `.env`。本轮用合成图片验证了完整模型名支持图片输入；更换服务或模型后需要重新核实能力。

安装后，在任意文件夹的终端运行：

```powershell
geoh run
# 使用纯终端交互
geoh run --print
# 指定数据工作目录
geoh run --cwd '你的数据目录'
```

`geoharness run` 是同一入口。默认使用当前终端目录作为工作目录；无需进入源码目录或设置 PYTHONPATH。默认界面为 React TUI，--print 使用打印模式。未指定 --config 或 GEOH_CONFIG_PATH 时，读取 `~/.geoharness/config.yaml` 及同目录 `.env`；--config 可指定其他 YAML，且会传到后端。相对数据路径和输出目录按工作目录解析；已有绝对路径仍按配置使用。`geoh dry-run` 验证组装，不连接 MCP，也不证明模型可用。

首次安装或更新时，在源码目录构建并安装 wheel（使用 uv，安装目标为当前 python 对应环境）：

```powershell
uv build --wheel
uv pip install --python python --reinstall .\dist\geoharness-0.1.0-py3-none-any.whl
```

wheel 包含全部内置技能及其参考文件，安装后运行无需保留源码目录。确保该 Python 的 Scripts 目录（Windows）或 bin 目录（Linux/macOS）在 PATH 中；新环境可运行 `geoh init` 创建配置。全局配置与安装文件分开存放，升级安装不会覆盖配置或 `.env`。希望使用上面的服务参数时，在全局 YAML 中配置，或运行 `geoh run --config '配置文件的完整路径'`。

若启动提示配置的 MCP 模块不存在，系统会继续运行原生工具；需要相应扩展时，为该服务配置实际可用的 command/args。

MCP 配置必须对应真实安装入口，不能按服务名猜 Python 模块名。本机 Geo MCP 服务已验证
使用 `D:/Python312/python.exe -m src.server`，cwd 为
`E:/Administrator/hermes/geo-mcp-server`。其他机器应配置自己的 Python 和服务目录。
连接失败会显示具体原因；原生空间分析和 PostGIS 数据工具仍可使用。
新执行 `geoh init` 默认不启用外部 MCP；安装服务后再配置真实入口。已有用户配置不自动删除。

独立 PostgreSQL MCP 可使用 [Postgres MCP 官方项目](https://github.com/crystaldba/postgres-mcp)。
本机使用 `postgres-mcp==0.3.0`，该扩展需要 Python 3.12 或以上；GeoHarness 原生工具的
Python 版本要求不变。推荐安装到独立环境，避免改变 GIS 主环境的依赖。Windows 示例：

```powershell
uv venv --python 3.12 "$env:USERPROFILE\.geoharness\servers\postgres-mcp"
uv pip install --python "$env:USERPROFILE\.geoharness\servers\postgres-mcp\Scripts\python.exe" postgres-mcp==0.3.0 "mcp>=1.25,<2"
```

在全局 YAML 的 `mcp` 下添加以下配置，保留已有 Geo MCP 条目。`USERPROFILE` 是
Windows 现有环境变量；`GEOH_POSTGIS_DSN` 使用配置同目录 `.env` 中已有的数据库连接串。
若原生工具与 MCP 使用不同数据库，需提供对应连接串，不能直接替换为原生工具的 DSN。

```yaml
mcp:
  postgres:
    command: ${USERPROFILE}/.geoharness/servers/postgres-mcp/Scripts/postgres-mcp.exe
    args: ["--access-mode=restricted"]
    env:
      DATABASE_URI: ${GEOH_POSTGIS_DSN}
      PGCONNECT_TIMEOUT: "5"
```

真实入口是 `postgres-mcp`，不是 `python -m postgres_mcp_server`；该服务通过
`DATABASE_URI` 接收连接信息，仅配置 PGHOST/PGPORT 等变量不足以启动。restricted 模式
限制 SQL 为只读事务。高级索引分析及查询统计工具可能需要数据库扩展；连接成功不证明
所有这些扩展存在。凭证通过环境传给子进程，不放在命令行；诊断会遮蔽配置的连接串。
该版本使用 MCP 1.x 的 FastMCP API，安装命令明确限制 `mcp<2`；MCP 2.x 已移除此 API，
直接安装未设上限的发布包会在导入时失败。此限制仅用于独立服务环境。
Windows 上该服务可能显示 `Signal handling not supported on Windows`；这是服务未使用
Unix 信号处理的提示。是否连接成功应以 MCP 握手和实际 SQL 请求结果为准。

继承的 PostGIS PROJ 数据目录可能与 rasterio wheel 不兼容。启动时只在当前进程选择
与 rasterio 自带数据库兼容的目录，成功修复不显示警告，不修改机器环境或 PostGIS 文件；
pyproj 仍使用自己的数据目录。

`skill name=map-making` 兼容映射到项目的 `cartography`；用户实际安装的同名技能优先。
项目五个技能为 `spatial-analysis`、`cartography`、`geo-perception`、
`geo-comprehension`、`geo-reasoning`，未知名称会返回当前实际可用的技能名称。

工具运行成功后，即使模型下一次回复为空，已完成的调用和结果也会保存到会话。
恢复请求沿用结果，不重新执行该工具。空回复与工具失败分别处理，终端同一错误只显示一次。
已保存的完整工具对话可恢复；旧版本已经漏存的结果不会凭空补回，但 Python 运行目录的
脚本、stdout 和 manifest 仍在，可以按路径重新读取。

本机主模型仍为 `cbcn/deepseek-v4.1-flash(max)`。聚合服务的 `(max)` 后缀优先于
低推理请求参数；当推理耗尽输出额度时可能没有正文或工具调用。可在 `agent` 中明确配置
一次空回复恢复，临时使用已经验证的同模型 `(none)` 模式，界面会显示恢复模式：

```yaml
agent:
  max_tokens: 16384
  empty_response_retries: 1
  empty_response_fallback_model: cbcn/deepseek-v4.1-flash(none)
  empty_response_max_tokens: 8192
```

在一次任务中，确认主模式仅返回推理且输出额度耗尽、备用模式完整成功后，后续工具回合
沿用这个恢复模式及其输出上限；配置完全相同时，重启恢复待处理任务也保留该模式。
真实新任务或配置变化恢复主配置。并发任务互不影响，不改写全局模型。
普通空回复、网络失败或截断参数修复不会单独触发持续切换。
未配置恢复模式时不会自动换模型或盲目扩大预算。持续空回复会说明结束原因、token 消耗
和推理长度；失败请求的 token 也计入会话用量。升级后退出旧进程，重新执行 `geoh run`。
默认 `agent.include_stream_usage: true` 明确请求流式用量；不支持该参数的服务可设为 false，
服务没有返回用量时无法准确统计。恢复期间取消仍结算服务已经返回的用量；正文传输中断
会保留收到的部分内容，并明确提示回答尚未完成，残缺的工具调用不会执行。

工具调用因 `finish_reason=length/max_tokens` 返回残缺 JSON 时，默认还有一次定向参数恢复（`agent.tool_argument_retries: 1`，设0关闭）。恢复请求仅开放对应的修复接口，沿用配置的空回复恢复模式，完整调用和已有结果不重复执行；恢复后仍不完整、出现重复或传输失败时保留原诊断并拒绝该调用。恢复花费的 token 纳入本次记账；主模型配置不变。

`geo_python` 注入 `SOURCE_CWD: Path`、`RUN_DIR: Path` 和 `DATA_DIRS: list[Path]`，直接迭代 `DATA_DIRS`；环境变量 `os.environ['DATA_DIRS']` 才是按 `os.pathsep` 分隔的字符串。所有输出写到 `RUN_DIR / filename` 或相对文件名，由 `geo_deliver` 负责工作区交付。工具返回 `execution_contract`，对已知的类型、投影或路径错误返回 `recovery_hints`，保留真实退出状态，不自动重试脚本。

`DATA_DIRS[0]` 是工作区读取根，不能假定它就是某个数据子目录。每次模型请求会收到实际 `data_root`、已测数据和真实产物的 `data_refs`；尚无引用时先用 `geo_vector_data`/`geo_raster_data` 的 `list` 和 `inspect` 建立档案。Python 中的 `DATA_REFS` 使用同一快照：`resolve_data(ref)` 返回原路径，`read_vector(ref, target_crs=None)` 读取并可投影向量，`native_layer(ref, derivation=None, ...)` 保留原生来源和制图子集依据。未知、歧义或已变化的引用会拒绝；原生分析输出还核对生产者记录的 SHA256。应先投影整个 GeoDataFrame/GeoSeries，再遍历行；`iterrows()` 的 pandas Series 没有 `to_crs()`。

`todo_write` 维护 Agent 内部清单，默认写入 `.geoharness/tasks/TODO.md`，可通过 `agent.internal_todo_path` 配置工作区内的子目录文件。省略路径或传 `TODO.md` 均重定向到该位置，项目根 `TODO.md` 留给项目长期待办。工具拒绝其它目的地、敏感路径、符号/硬链接和已配置的拒绝路径；显式禁止工具仍有效。

配置 QGIS 输出目录中最近64个有清单的任务，仅其 `request.json`、`result.json`、`manifest.json`、`stdout.txt`、`stderr.txt` 可由 Python 精确只读访问；使用 QGIS 工具返回的文件路径。不会授予整个目录、`.profile` 或父配置目录，也不能写这些文件；链接转移和敏感文件名仍受保护。Shapely 几何本身没有 CRS，投影转换用 `gpd.GeoSeries([geometry], crs=已核实的源CRS).to_crs(目标CRS).iloc[0]`。缓冲、裁剪、相交、距离、分区计数和影响分析优先用 `geo_analysis`，其他算法用 `geo_qgis` 的 search→help→run 获得实际参数，避免读取工具源码猜 API。

React TUI 中，停止、关闭或回收模型流会及时释放网络连接。模型重试流可由不同任务
关闭，不再因 `ContextVar` token 的上下文不一致而产生后台异常。若本轮运行出现未捕获
的异常，后端会显示一次具体错误并结束本轮，界面恢复输入状态；可以继续输入，
已完成的工具不会因这次异常被自动重执行。更新安装后需要退出旧进程并重新启动。
输入前的认知提示 Hook 同样支持空回复恢复；重试状态不会被当成提示正文。
建议性 Hook 的普通失败会记录为失败结果并允许主请求继续，用户停止仍会立即传递；
在 Hook 恢复期间停止，也会结算服务已返回的用量。

流失败按 `provider_sse`、`http`、`transport` 和 `parser` 区分，显示请求标识、HTTP 状态、
异常类型、片段数量与最后收包时间。服务端返回的 `stream stall timeout` 会明确标为服务端
流错误；本地连接或读取超时属于传输错误。诊断只保留安全元数据，不保存推理正文、响应正文或凭证。
已确认的使用量与工具结果保留，重试不会重新执行成功工具。

配置、日志、会话及插件/自动任务状态统一保存到 `~/.geoharness/`，不同工作目录的状态分开保存。启动不会再在工作目录创建 `.openharness`。工作目录的 `.geoharness` 保存分析和 Python 运行记录，是有实际用途的目录。

## 使用本机 QGIS

QGIS 集成复用独立安装的 PyQGIS 和 Processing，不需要打开 QGIS 桌面，也不会把
QGIS 的 Qt/GDAL/PROJ 导入主 Agent 的 Python 环境。安装 QGIS 后可在任意数据目录运行：

```powershell
geoh qgis-doctor
```

该命令实际启动 QGIS，返回版本、提供者和算法数量；`geoh dry-run` 只验证工具组装。
配置中 `qgis.enabled` 默认为 true，Windows 优先自动发现安装记录与常规安装目录。
无法发现时，在全局 config.yaml 增加实际路径：

```yaml
qgis:
  enabled: true
  install_dir: '你的QGIS安装目录'
  outputs_dir: .geoharness/qgis
  timeout_seconds: 180
```

也可显式设置 `python_executable` 和 `prefix_path`；前者必须是原生 Python 可执行文件，
不接受批处理或 shell 脚本。其他系统使用明确配置的可用 PyQGIS 环境。设 enabled=false
会移除两个 QGIS 工具，现有 geo_analysis、geo_cartography 和 geo_python 仍可使用。

`geo_qgis` 提供 doctor、search、help、run、model 五个动作。先搜索本机实际算法，再查
完整参数、枚举选项和默认值，最后执行；search 的 execution_allowed/restriction 表示
当前集成是否支持执行该算法，注册数量不代表所有算法均已业务验收。

```json
{"action":"search","query":"buffer","limit":5}
{"action":"help","algorithm_id":"native:buffer"}
```

确认输入使用米制投影后，例如调用：

```json
{"action":"run","algorithm_id":"native:buffer","parameters":{"INPUT":"points.gpkg","DISTANCE":500,"OUTPUT":"buffer.gpkg"}}
```

这里 OUTPUT 相对本次新任务目录，返回 outputs 中的真实绝对路径，供下一步分析或
制图使用。不同算法按自身 CRS/单位解释距离，不自动把经纬度中的500换成500米。
`model` 接受本地 .model3 与 parameters：模型必须使用已批准的内置算法，文件通过
模型参数传入，输出由运行器控制；表达式生成文件路径、脚本提供者、数据库写入与自由
命令参数不在支持范围。缺少提供者、参数或不支持的输入会返回失败原因。

当前 QGIS 接口支持本地数据文件，数据库仍使用既有 PostGIS 工具。每个 QGIS 任务
单独启动进程并保存 request.json、result.json、manifest.json、stdout.txt、stderr.txt
和实际产物，默认位于工作目录 `.geoharness/qgis/`。任务间使用持久文件，不能传递上一
进程的内存图层。取消或超时会结束任务及其 native 子进程。运行器使用独立临时配置，
不继承 API 凭证或个人 QGIS 插件设置；文件保护用于可信本地算法，不是 OS 安全沙箱。

`geo_qgis_project` 提供 create、inspect、export、preflight。create 的 layers 是结构化数组，
按从底到顶排列；矢量可指定分类字段、类别颜色和 label_field，栅格保留实际数据。

```json
{"action":"create","title":"分析结果","crs":"EPSG:32647","map_id":"analysis-map","layers":[{"file_path":"affected.gpkg","name":"受影响对象","style":{"color":"#d73027"},"label_field":"name"}]}
```

创建真实 QGIS 工程及基础打印布局，并从该布局导出 preview.png；output_format=pdf
时同时导出 PDF。数据副本采用 GPKG/TIFF，工程使用相对引用，project.json 记录
文件、样式、布局及预览摘要。结构化布局包含标题、主图、可选详图、图例、比例尺和
说明栏；详图使用 layout.detail_extent。复杂注记和任意自定义布局仍需按实际能力处理。

打开返回的 project_file，修改样式或打印布局并保存。然后用 inspect 查看工程，或用
export(project_path=...,layout_name=...) 导出选定的已保存布局；多个布局时需要指定名称。
export 读取真实保存的图层和布局，生成新数据快照、工程和图片，保留原工程。
本地图片/符号资源随工程复制，网络图层、嵌入工程及项目 Python 宏不支持。

创建/导出只表示执行完成，视觉状态仍为 unverified。使用返回的 preview_path 进行
geo_map_review，保留相同 map_id；用 project_dir 和 primary_map=preview_path 调用
geo_deliver。PDF 可列入 files，主图摘要对应实际 PNG 预览。所有检查和交付仍使用
共享预算；最终 draft/passed 由下面的地图任务回执和实际检查证据决定。

详细的执行隔离与数据流见 [architecture.md](architecture.md)。

## 一个完整任务

准备具有明确 CRS 的行政区、危险区、居民点或道路文件，向 Agent 提出：

> 分析危险区及其周围500米内的居民点，统计每个行政区的受影响数量。输出资产距离表和影响选集，生成行政区、危险区、受影响居民点的专题图。检查成图并自动修正呈现问题，保留结果和检查报告。

原生 geo_analysis 提供 buffer、clip、intersection、distance、count、impact，不依赖 MCP/PostGIS。复杂组合使用 geo_python。既有数据库和 MCP 接口保留。

impact 的影响条件是相交或最短距离不超过阈值。分区按 intersects 计数，包含边界；跨区/重叠区对象分别计入每区，因此分区数之和不一定等于全局去重数。这项口径不代表因果或风险等级证明。

## 输出与复现

原生分析每次独立保存 GeoJSON、CSV、统计和 manifest，记录来源、参数、计量/导出 CRS 和计数口径；默认目录为工作区内 `.geoharness/analysis/`。未知 CRS 和无效几何会报错，不静默按 WGS84 处理。

在 `geo_cartography` 或 `geo_qgis_project` 的新建请求中传入 `analysis_manifests`（最多8个实际 geo_analysis 清单路径），系统会读取对应输出，核对 SHA256、要素计数、impact 的距离阈值/affected 标记及影响选集，并要求每份清单都至少有一份输出确实用于当前地图图层或声明的 `layer.derivation`。新清单的输出摘要必须一致；旧清单没有原始 SHA256 时只核对现存输出计数，并明确缩小验证范围。任意 Python/QGIS 清单或自由文字不属于此接口支持的统计证据。

QGIS 说明栏中的方法、数量、阈值和计量 CRS 来自上述核对结果，显示 CRS 单独取当前地图设置。`footer_text` 的自由文字作为备注保留，不能冒充已核对统计。这里验证的是持久输出的一致性，没有重新计算原始空间运算；`analysis_correctness` 仍为 `unverified`。复杂分析仍须另外核验方法、输入与结果。

Python 每次保留 analysis.py、stdout.txt、stderr.txt、manifest.json 和产物清单，默认在 `.geoharness/runs/`。SOURCE_CWD、DATA_DIRS、RUN_DIR 是脚本全局变量，也提供同名环境变量；环境中的 DATA_DIRS 按 os.pathsep 分隔。当前目录为 RUN_DIR，输出只写 RUN_DIR 或相对文件名。配置的分析、制图和既有运行产物目录可只读访问，仍拒绝凭证文件和源文件写入；超时终止并等待子进程。

数据 `list` 自动检查最多8个文件的实际元信息，尚未检查的文件会明确提示；使用前应调用 `inspect`。工具成功后保留真实路径、CRS、范围、字段/几何/数量或栅格大小、波段、分辨率、dtype、nodata，供后续模型请求使用。覆盖数据或修改 Shapefile 的 `.prj/.dbf` 等会使旧记录失效。栅格发现只读取头信息，不扫描像素。

Python 中 `GEO_DATA` 是已测元信息字典，`PREVIOUS_OUTPUTS` 是已有产物的 `list[Path]`。每次调用的 `RUN_DIR` 都是新目录；上一轮文件应使用工具返回的实际路径，不能拼日期或放进新 `RUN_DIR` 读取。裁剪使用 `mask_raster(raster_path, GeoSeries/GeoDataFrame, crop=True)`，它将带已知 CRS 的几何变换到栅格实际 CRS 并检查相交。分析投影与栅格投影可以不同；裸 Shapely 列表不能交给这个入口，因为无法核实其 CRS。返回值与 `rasterio.mask.mask` 相同，为数组和 transform。

Python 可只读读取已核验 QGIS 任务返回的诊断文件、预览图和 `project.json`，以便测量真实图片；不会授予整个 QGIS 目录、数据副本目录或 profile，也不能修改这些文件。布局/图层枚举用 `geo_qgis_project(action="inspect",project_path=...)` 或实际 `result.json`，不读取全局 `tool_artifacts`。清单与地图绑定失败会返回具体清单、原生产物路径和当前图层；选择对应产物或其 `layer.derivation`，不能为通过检查附加无关统计。

一次被识别为制图的 Python 脚本可能生成多张图片；系统按最后写出的图片登记一个 primary_map 候选和一个版本，map_candidates 列出全部图片候选。这些附件不是连续修订版；需要审查另一张图时明确选择它。geo_map_review 再次审查已登记旧图会更新该版本的检查，不增加新的成图版本。

工具结果包含 status（succeeded/failed/timed_out）、exit_code、error_summary 和日志路径。界面中的 `✗` 表示该次调用确实报错；只有 stderr 警告而退出码为0时仍算成功。`geo_python {}` 是没有收到完整 code 参数，脚本未执行；可能与服务返回不完整/无效工具参数有关。缩短脚本或改用原生工具，并查看实际错误。主 Agent 的输出预算通过 agent.max_tokens 配置，示例为16384，减少长推理和长脚本竞争同一预算的情况。

运行器清理凭证环境，限制通常的文件/GIS 接口、网络和子进程；原始数据默认只读，修改时生成新文件。它用于可信 Agent 分析脚本，不能隔离恶意 native 代码，**不是 OS 安全沙箱**。

工具定义公开当前 Python 超时上限；通常省略 timeout_seconds。超过上限的参数在执行前拒绝，并显示请求值和允许值，这与实际运行超时不同。工具参数截断、非对象 JSON 与缺少必填字段分别记录错误类别和位置，拒绝的调用不会执行；其它合法调用与已完成结果保留，模型可继续修正参数。

分析和工程发布遇到 Windows 临时访问/共享冲突时，仅重试最后的目录重命名，不重复计算。publication.max_attempts（默认4）与 retry_delay_seconds（默认0.1）控制有限重试；仍失败时保留暂存结果及恢复路径，不自动关闭占用进程或修改权限。

## 地图任务与质量回执

系统为地图维护持久 UUID，工具返回的 `map_id`/`job_id` 是该身份；模型提供的名称仅作为别名。后续修改、换文件名、审图、打包和交付应沿用返回的 map_id。存在等待模型处理的工具结果时，精确输入“继续”或“接着做”（也支持 continue/resume）与 `/continue` 一样恢复原任务，不追加新的用户目标，不重置预算或重复工具。40回合限制是单批运行上限；达到上限后同样可以这样续跑。明确的新指令会创建新请求及预算；没有待处理结果的续跑词仍关联当前地图，当前会话没有活动任务时关联工作区最近的持久任务。

会话快照保存当前地图身份、已用成图/审图额度、测量过的数据档案、确定性失败缓存和安全流失败记录。重启恢复时沿用这些状态，文件变化使原档案失效；新运行始终使用当前配置。正在进行的审图时间预留会释放，已消耗时间保留。快照中的视觉通过记录作为历史，恢复后需要当前图片的正式审查证据，不能由普通会话内容直接认证地图质量。

回执默认保存在工作区 `.geoharness/maps/<UUID>/receipt.json`，索引为 `.geoharness/maps/index.json`。`cartography.jobs_dir` 可配置，但必须是工作区子目录。只查询已有证据可调用：

```json
{"action":"status","map_id":"工具返回的UUID"}
```

也可用 `map_path` 查询已登记的实际图片。`geo_cartography(action="status")` 会重新计算当前图片和已登记工程/交付文件的摘要；返回摘要，完整回执在工具 metadata 的 `map_receipt` 中。没有对应任务时返回 draft，不创建任务、不绘图、不增加检查结果。

| 阶段 | 记录的实际证据 |
|---|---|
| parameters | 新建时实际使用的 preflight 参数与报告，状态为 recorded |
| data_checked | 字段、几何、CRS、范围和可见数量等数据合同检查 |
| rendered | 实际图片路径与 SHA256 |
| layout_checked | 与当前图片绑定的布局测量或 QGIS bounds_check |
| visual_checked | 与当前图片绑定的实际视觉审查 |
| packaged | 同图预览、工程清单与工程文件摘要 |
| delivered | 实际复制的交付文件和主图摘要 |

缺少证据的阶段为 unverified；布局或视觉检查报告问题时可为 issues，文件改变使旧证据失效时为 stale。`quality_status=passed` 要求同一张图的 data_checked、rendered、layout_checked、visual_checked 全部为 passed；其余为 draft。范围仅为地图数据合同、已测布局几何和图片视觉检查。当前 `analysis_correctness=unverified` 表示没有独立复算原始空间分析，不表示分析已经失败；输出摘要、计数和已复算面积的验证结果及范围另见 analysis facts 的 `verification`/`verification_scope`。打包或交付成功不会补齐缺失检查。

修改图片、QGIS 工程、已登记数据副本或交付文件后，旧摘要绑定的证明不能继续用于新文件；状态查询会标记受影响阶段 stale。保存修改后应导出新图，核对数据、布局并审查新图，再重新交付。完整交付包含 `map-receipt.json`；该回执是当时的快照，当前状态以重新查询结果为准。

## 成图检查与自动重绘

新建 QGIS 地图时，系统先将受控读取的图层保存为临时 GPKG/GeoTIFF，再对该数据副本预检并交给 QGIS 渲染；检查与成图使用同一份数据。冻结输入、预检失败不占成图次数，临时副本在读取线程和 QGIS 进程结束后清理。已有用户工程的 inspect/export 保留原数据、样式和布局，导出不覆盖输入工程。

geo_cartography 显式读取成图，结合任务、预期图层、参数和源数据摘要，检查空白、图层可见性/遮挡、图例对应、文字和布局。先准备完整布局，再合并修正可确认的错误/警告，不限制地图版本和修正次数。Matplotlib 和默认 QGIS 路径均可自动应用支持的呈现补丁并重绘；QGIS 使用下文的实际工程、区域和问题证据约束。不会因为 info 提示或单纯审美建议再画一版。报告由工具返回实际路径；Matplotlib 自动修正前地图和预览保存在成图旁独立 `.review-*` 目录。

原生工具、Python 制图及独立审查均不按版本、调用次数或累积审查时长停止。旧配置 `max_repairs`、`max_reviews_per_request`、`total_timeout_seconds` 仍可读取，但不再限制成图或检查。单次API请求和脚本的超时仍有效，用户可取消正在执行的任务。相同图片和审图配置的结果会缓存，失败也不会盲目重试；明确修正模型/token/provider参数后允许再次检查。持久地图身份、历史版本与回执继续保留。

QGIS 首版前的 `preflight` 会给出有界标注点密度风险，供决定优先级、符号大小和详图；这是规划提示，不是视觉通过证明。`label_style` 支持 `placement`、`distance_mm`、`offset_x_mm/offset_y_mm`、`priority`（0最低、10最高）；`around_point` 使用距离，固定 X/Y 偏移应使用 `over_point`。`style.obstacle=true` 可让未标注的重要几何参与 PAL 避让，但不能保证所有标签显示，也不能阻止线符号本身压盖面。疏散线压盖主体应调整实际线宽/透明度/图层顺序，或在本请求内改用详图/分图。

审图通过实际图片、登记工程及清单摘要核实后端。QGIS 返回 `qgis_patch`（布局毫米、字体pt、图层毫米/透明度及标签设置），图层用实际0-based `layer_index`；Matplotlib 才使用英寸 `layout_patch`。新建 QGIS 工程记录真实导出中的标注、图层顺序、符号和图例节点；这些工程事实用于排除矛盾建议，不代替视觉检查。审图同时提供整图和有界的原分辨率地图框、图例与底栏区域。可执行问题必须关联 `issue_id`、实际区域、框内图层和当前图片摘要。

`map_review.auto_repair` 默认启用。当前权限允许 QGIS 创建、且工程全部文件摘要和创建参数完整时，`geo_map_review` 自动应用支持的呈现修正，在同一任务内重绘，再检查实际新 PNG。没有修正次数上限；通过、没有有效修正、真实执行失败、重复修正计划/图片或用户取消时停止，并保留具体原因和实际失败诊断。`auto_repair=false` 可只检查。自动修正保持数据、分类值及颜色、分析方法和阈值；详图范围由实际问题区域中的原生要素推导，主图保留完整内容。模型建议的框外图层、错误单位或不可靠符号调整不会执行。

自动循环保存每版工程和报告，比较硬检查覆盖、视觉状态及剩余问题，保留较好的可交付候选。返回的 `image_path`、`final_render`、`selected_candidate` 和 `map_receipt` 指向同一候选；`latest_image` 和 `latest_review_status` 单独说明最后一次尝试。图片相同也仍按登记路径和版本关联工程，复制交付不增加成图次数。结构检查、视觉检查和分析正确性分别报告，服务失败不会把草稿改标为通过。

有放大图和密集重点点时，Python 可使用 `geoharness.cartography.layout` 的 create_map_figure、place_point_labels、save_map_figure。它预留主图、放大图、图例、说明的独立区域，按渲染后的文字边界避让；放不下的名字保留省略记录，使用编号对应名单。保存 `.layout.json` 记录分区相交、标签相交/裁切和图例/说明越界，与实际图片 SHA256 绑定。geo_map_review 先检查对应报告；明确的布局缺陷先修正，再调用视觉服务。

数据核验、布局几何检查与视觉审查分别报告。颜色检出可能来自图例，非白像素比例也不能证明图层、分级或位置正确。视觉服务失败时交付标为 **draft**（未完成视觉审查的草稿），说明实际原因和剩余问题；不得用像素计数替代通过结论。已知布局缺陷仍可统一修复，停止的是无效视觉重试。

不会因图片建议自动更换数据、统计方法、字段或影响阈值。视觉检查不能证明数值正确，必须同时核验分析结果。

下面是视觉审查状态，与最终 quality_status 分开：

| 视觉状态 | 含义 |
|---|---|
| passed | 完成视觉检查，没有报告需处理的问题 |
| issues | 仍有具体问题，报告保留原因 |
| unverified | 检查关闭、服务失败、回复无效或无法确认，不表示正确 |

PNG 是首期推荐输出。PDF/SVG 检查同参数 PNG 预览，导出文件本身的排版/字体需另行核验。HTML 没有实际网页截图时仍标为 unverified。Python 自定义地图用 geo_map_review，由 Agent 按问题修改呈现代码、重绘、再检查。

仅调用 geo_map_review 会登记已有 Python 图片及可用的布局报告，不会替它补造数据 preflight。缺少与当前图绑定的数据检查时，即使视觉审查 passed，最终仍为 draft。Matplotlib、HTML 和自主 Python 入口继续保留。

map_review 可单独配置 model、base_url、api_key；留空时复用主配置。主 Agent 保持 `cbcn/deepseek-v4.1-flash(max)`。本机 9Router 网关使用已实测的独立审图模型 `cbcn/deepseek-v4.1-flash(none)`，4096 token、45秒单次上限；这张业务地图一次返回完整审查约9秒、reasoning_tokens=0。仅传 thinking.type=disabled 或 reasoning_effort=low 时，网关仍把全部4096/8192 token用于推理，无完整结论；不能据此声称关闭思考。`request_extra_body` 用于服务支持的专用参数，默认空对象，其他服务按其实际能力配置。report.json 的 diagnostics 保留 response_model、finish_reason、reasoning_tokens、output_tokens 和 duration_seconds。预算耗尽时报告 unverified，即使附有部分 JSON 也不接受通过。

## 地图交付与手动修改

审图 diagnostics 区分网络、服务响应、JSON、schema、模型输出token耗尽和缓存；schema 失败保留字段位置/类型/说明及有界脱敏 JSON。缓存命中明确 request_sent=false，不能写成服务又失败一次。仅 JSON/schema 失败可用 geo_map_review 的 retry_invalid_response=true 显式恢复一次。layout_patch=null 可按空补丁处理，其它错误字段仍拒绝。

已有 Python 地图打包使用 geo_cartography(action="project", map_path="已有地图.png", project_output_dir="exports/工程目录", layers=...)。旧 output_path 作为已有地图输入继续兼容。工程目录必须留在工作区，且必须是尚不存在的新目录；已有空目录也不删除。QGIS 工程检查除图层有效外，也验证项目/地图框 CRS、范围和比例尺；打包成功不表示视觉审查通过。

默认地图和最终交付保存到当前工作目录的 `exports/`。geo_deliver 把明确选定的地图、结果数据、检查报告、脚本和工程复制到独立交付子目录，返回实际路径并保留之前的交付。delivery.outputs_dir 必须是工作目录的子目录；Python 脚本不负责跨目录复制，输出仍限于 RUN_DIR。

交付同时生成 `summary.md` 和 `summary.json`：从复制后通过摘要核验的最终选中工程读取实际 CRS 官方名称、分类/分级数量、图例节点、分析事实和检查状态；文件未通过核验时这些事实保持未验证。底栏最多保留三条逻辑说明，实际字体换行仍由 QGIS 测量；完整统计、参数、输出指纹及自由备注保存在工程事实报告和摘要里，手写统计不混入已验证底栏。

`geo_cartography` 的图层样式采用桥接参数（如 `column`、`render_method`、`categories`、`colors`）；需要 QGIS 的 `renderer`、`outline_color`、`width_mm`、`size_mm` 等精确符号参数时，使用 `geo_qgis_project` 的类型化图层参数。地图标题放在顶层 `title`，而不是 `qgis_layout.title`。参数不兼容时保留所需图层，按照工具给出的对应接口修正。

`DATA_REFS` 是“稳定 ID → 记录”的字典；名称位于记录的 `name`，不在 ID 字符串中。Python 可以直接用 `read_vector("唯一的已登记名称")`。当前地图任务在 QGIS 目录内登记且摘要未变的 `report.json` 也可按返回的准确路径只读访问；相邻文件、报告目录列表与写入权限仍不开放。

`mask_raster(path, GeoDataFrame/GeoSeries, crop=True)` 会转换几何到栅格的实际 CRS，返回 `(array, transform)`；应先解包，再读取 `array.shape` 或统计值。数组维度为“波段、行、列”，不包含栅格数据集的 `crs/profile` 属性，这些信息以已检查的数据档案为准。

默认 QGIS 制图的 PNG 与可编辑工程来自同一打印布局。Python/Matplotlib 自定义地图可用 geo_cartography 的 project 动作，以已存在的原成图和实际图层创建工程，不重新绘图；生成的 QGIS 布局不保证重现所有自定义注记。分类层明确 column（兼容 field_name）、categories 和 colors，原生绘图与 QGIS 使用对应的分类颜色；map_extent 锁定主图范围。不能为创建工程而另画简化图或丢弃分类字段。工程包含：

- `map.qgs` 或 `map.qgz`：QGIS 工程，真实图层、符号样式和可编辑打印布局；以返回的 project_file/qgis_project 为准。
- `data/`：矢量 GeoPackage 和栅格 GeoTIFF 数据副本，使用相对路径，不依赖原始文件位置。
- `project.json`：文件、样式、布局及预览摘要；Python/Matplotlib 工程另有 `redraw.py` 重绘入口。

geo_deliver 可指定 primary_map（兼容 map_path）和 review_path。交付时核对主图、工程 preview 与记录的 SHA256；不同地图或已替换的预览会拒绝交付。交付 quality_status=passed 除了同图数据、布局和实际视觉检查通过，还要求包含的工程与已登记快照一致，复制期间没有变化；其余为 draft。工具同时返回 map_id、map_receipt、review_status 和 receipt_path，工程生成本身不代表质量验收。

默认 `version_policy="prefer_checked"`：选中的已登记版本有布局/数据/视觉问题或检查已失效时，交付工具查找同一 map_id 下图片、报告、工程均未变化的布局通过版本，并一起选取它对应的工程。优先选择数据、布局、视觉均通过的版本，其次是最近的布局通过草稿；不因文件名或版本较新而覆盖较好的候选。也可只传 map_id 自动选择。没有候选会返回具体错误；明确要保留指定问题图时用 `version_policy="selected"`，仍如实标为 draft。另存的未登记图片不会被旧 map_id 自动替换。`selection` 返回实际选择、原因和工程是否可用；换版本时不沿用旧图审查报告，附件的其他分析文件仍由调用者明确选择。状态回执的 `delivery_candidate` 只建议候选，不修改当前版本或原文件。

累计时次面数据使用 `geo_analysis(action="area", source={"file_path":"实际面数据"}, group_field="实际时间字段")`。输出每个要素的 `area_m2`，以及整体/分组的 `feature_area_sum_m2`（面积之和）、`union_area_m2`（合并去重覆盖）和 `overlap_area_m2`（前两者之差，并非独立重叠区域的面积）。计量采用声明的米制投影，不按文件顺序推断时次；分组覆盖不能相加称为总体覆盖。输入已有 area_m2 字段会明确报冲突，避免覆盖原属性。将返回的 result 图层和 manifest 用于新建地图，说明栏从持久输出几何复算统计，并分别写出面积之和与去重覆盖；未重新处理原始数据，也不提升整个分析的正确性状态。

在 QGIS 3.x 打开返回的 .qgs/.qgz；双击图层修改颜色、透明度、分级等，通过“项目 → 布局”打开返回的布局，修改标题、图例、地图框、比例尺后导出 PNG/PDF。默认 QGIS 路径的原图即由该工程布局导出；Python/Matplotlib 工程使用另一渲染引擎，自定义注记可能需要参照预览和原始脚本补充。

分享或移动时携带整个工程目录。Python/Matplotlib 工程提供 redraw.py 时，安装 GeoHarness 后可在工程目录运行 `python redraw.py --output edited.png`，从数据副本和 JSON 重绘。此入口读取 project.json，不读取在 QGIS 中保存的样式/布局；在 QGIS 修改样式后应从 QGIS 或 geo_qgis_project 的 export 导出。保存到 GeoPackage 的数据修改可被两种方式读取。重新交付会创建新目录，保留用户之前编辑的工程。

## 后续方向

当前优先核对多图层叠加与影响范围的实际业务流程，空间分布与热点、栅格/地形业务和质量准确率评测延后。认知技能保留领域参考价值，简单叠加任务无需强制进入空间自相关或因果流程。当前实现和回执不代表最终全量或真实业务验收已经通过。

## 默认静态制图

日期字段在内存副本中转换成保留时区的 ISO 字符串，空日期保留为 null；分类值同步转换后写入工程，不需要用 Python 删除时间属性或重建几何。内容核验会区分真实业务 ID 与 GeoJSON 驱动自动加入的要素编号，仍拒绝业务属性或几何被修改的缓存。

两个新建入口共用分析来源与状态规则。映射原生分析目录中的实际输出时，可自动识别旁边明确列出该文件的 manifest；也可以显式传 `analysis_manifests`。无分析依据的普通专题地图仍可生成，报告的 `analysis_evidence_status` 为 `not_provided`。上次地图的事实不会附加到新地图。计量 CRS 与显示 CRS 分别来自分析清单和地图参数；手写备注的计量 CRS 与清单冲突时，在生成图片前拒绝。

需要只画分析结果的一部分时，在图层中使用 `derivation`，让系统从原输出生成可核验的制图子集；不要手工导出一个丢失来源的新文件。例如 `geo_qgis_project.layers` 中的一层可以写成：

```json
{
  "file_path": ".geoharness/analysis/实际任务/result.geojson",
  "derivation": {
    "feature_indices": [7, 15, 0],
    "keep_fields": ["name", "distance_m"],
    "annotations": {"map_label": ["1", "2", "3"]}
  },
  "label_field": "map_label"
}
```

`feature_indices` 是原输出中从 0 开始的行位置，省略时保留全部要素；上例位置仅示意，必须先查看实际数据。`keep_fields` 省略时保留全部属性，几何始终保留；`annotations` 每列必须与选中要素数一致，不能覆盖原字段。纯数值列保留数值；混合类型/object 注记列统一为文本/null，以保持 GeoPackage 写入与核验一致。`geo_cartography.layers` 的 JSON 字符串支持同一 `derivation` 结构。来源核验记录完整结果数和上图子集数，注记不是新的分析统计。

提供 `qgis_layout`/`layout` 的预检会在独立 QGIS 进程中测量真实字体、比例尺、图例与面板，不导出地图或工程、不占成图次数。仅检查数据且未提供布局时，预检保持无 QGIS 依赖。新建底栏根据实际可用宽度换行，按文字和比例尺高度扩展；页面放不下时，明确要求缩短备注或扩大页面。已保存工程的导出保留用户布局，不自动改写文字。

需要让主图关注事件、详图关注密集区域时，可在 `layout` 中指定各自的关注图层与显示图层。索引对应提交的原始 `layers` 数组，从0开始；显示顺序仍为数组中的从下到上顺序。背景可以显示而不参与范围计算。例如：

```json
{
  "main_frame": {"focus_layers": [1], "render_layers": [0, 1], "padding_ratio": 0.1},
  "detail_frame": {"focus_layers": [2], "render_layers": [0, 2]}
}
```

必需图层必须至少出现在一个地图框中，并通过最终地图框的联合可见性检查。显式主图范围与 `main_frame.focus_layers` 互斥，显式详图范围与 `detail_frame.focus_layers` 互斥。`geo_cartography` 自动添加的底图栅格位于原生图层数组的第0项，因此该入口的索引需包括底图。

原生预检返回 `preflight.layout_plan`，其中包含实际字体测量后适配的面板、逐框范围、关注区域占比和字体/数据/样式/运行时摘要。下一次 `create` 将该对象原样放到 `layout.layout_plan`，即可消费同一计划；不要手算适配前的地图框比例。数据、样式、布局、字体或QGIS运行时变化会使计划失效，要求重新预检，不占成功成图版本。未提供计划的创建仍自动完成测量、冻结和消费。工程保存并重开后核对同一计划，再导出图片。

新建布局默认启用 `legend_auto_fit`：根据 QGIS 实测图例尺寸加宽侧栏或缩短详图，详图至少保留20mm；不缩字号、不删类别、不自动改变纸张。仍放不下时返回 `content_metrics` 的必需/可用尺寸与实际字体证据。`effective_parameters`、`adaptations` 记录最终参数及调整原因。`legend_font_size_pt` 单独控制图例，`body_font_size_pt` 控制底栏；同时传常规 `layout.legend_font_size` 时，只有明确的 `qgis_layout.legend_font_size_pt` 优先于它。

工程编辑后的有效参数读取实际元素；纸张尺寸统一为mm，文字保留实际值与单位，仅Points字号另提供pt字段。`creation_parameters` 保存创建模板参数。硬布局检查支持毫米场景；用户将整个布局场景单位改成其他单位时，导出保留布局，检查状态为未核验并返回原因，不误报通过。

QGIS 原生新建/导出标记 `project_fidelity=saved_layout`，PNG、PDF 和预览从保存后重新打开的同一布局导出。给现有 Python/Matplotlib 图片补工程的 `action=project` 保留兼容，标记 `project_fidelity=reference`：QGIS 布局是参考布局，包内预览是原图副本，不能据此认定两者一致。需要同源交付请使用默认 QGIS 制图，再检查实际预览。

同一请求内，相同参数和未变化的本地输入触发同一确定性检查错误时，工具返回已有诊断并标记 `duplicate_error_blocked`，不重复执行该检查或重绘。文件内容摘要和状态共同判断输入是否变化，保留时间戳的同大小修改也会使缓存失效。修改参数、数据、关联分析输出或读取配置后可以重新执行。文件锁、超时等暂时故障不会按确定性错误缓存；没有稳定本地身份的目录别名、含外部依赖的栅格/VRT、已有工程的 inspect/export 和超过64MiB总读取上限的输入保留正常执行。该机制帮助避免无效循环，不能保证模型每次都给出正确参数。

`geo_cartography(action="preflight", ...)` 与 `geo_qgis_project(action="preflight", ...)` 可先检查实际数据，不绘图、不生成工程，也不消耗成图或视觉审查预算。正常新建地图会在预留预算前自动先执行同样的检查。检查 CRS、有效/有限几何、符号与标签字段、分类/分级、数值类型、指定要素总数，并返回主图/详图中的几何相交数量。相交计数不证明符号没有被遮挡；栅格只核验范围相交，不据此宣称像素值或语义正确。

显式 `map_extent` 使用 `{xmin,ymin,xmax,ymax}` 或四元组 `[xmin,ymin,xmax,ymax]`，必须同时指定显示坐标系（`geo_cartography.map_crs` 或 `geo_qgis_project.crs`）；`detail_extent` 同属该坐标系。不要直接把 GIS 四元组作为 `imshow.extent`，它的顺序是 `[xmin,xmax,ymin,ymax]`。没有指定范围时按对齐后的图层自动生成，单点/零跨度通过配置中的 minimum_span_geographic/minimum_span_projected 扩展。

图层的 `expected_feature_count` 用来核对实际总数，`minimum_visible_features` 控制主图或详图最少显示数；必需图层默认为1。确实允许空结果或界外背景时明确 `required=false`，报告会保留零计数/警告。`geo_cartography` 可以在内存副本中将无损数值字符串转为数值，不改源文件；直接 QGIS graduated/flow 字段必须已是数值类型。

PNG/PDF 默认使用 QGIS。配置 `cartography.static_renderer: qgis`，调用 `geo_cartography` 时可指定 `map_crs` 和 `qgis_layout`。`qgis_layout.detail_extent` 使用 `{xmin,ymin,xmax,ymax}`，与主图范围同属显示坐标系；`footer_text` 提供备注。主图、可选详图、图例和底栏分区生成，类别颜色复用已有桥接规则，中文字体与标注缓冲/放置由 QGIS 处理。这些设置不证明每个标签都已显示或位置正确，仍须检查实际成图。

返回的 `project_file` 是真实 QGIS 工程，`preview_path` 是从它的打印布局导出的 PNG。修改工程的图层、样式或打印布局并保存后，使用 `geo_qgis_project(action="export",project_path="...")` 导出新工程和图片。交付以返回的 `primary_map` PNG 与 `project_dir` 为主，PDF 可附加。视觉审查未完成时仍为草稿。

需要旧静态路径时指定 `renderer="matplotlib"` 或配置 `cartography.static_renderer: matplotlib`；HTML/SVG 保留原有渲染入口。QGIS 禁用或启动失败时，默认路径会给出具体错误。

QGIS 新建模板会在导出前检查自身分区、图例/文字内容溢出及元素边界。对用户已保存的工程，inspect/export 保留真实布局并在 bounds_check 中记录问题，允许合法的地图内注记或手动移动图例；非结构化模板可能返回 checked=false。有问题或缺少有效检查时按草稿处理。bounds_check 只覆盖报告中的布局元素与边界，不证明所有地图标签、图层语义或分析结果正确。显式 font_file 会随工程打包并由 GeoHarness 重导出时加载。QGIS 桌面需使用已安装字体，或由用户自行安装工程携带的自定义字体；系统默认中文字体无需另行打包。
