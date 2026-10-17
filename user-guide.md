# GeoHarness 使用指南

当前优先完成多图层叠加与影响范围分析，支持原生 GIS 和自主 Python 分析，并读取成图进行检查、修正和重绘。空间热点与栅格/地形是后续重点。

本版本保留本地矢量/栅格及 QGIS 分析制图能力；暂不提供在线天地图瓦片下载工具。

## 分析、引用与制图的执行契约

数据加载返回的 `data:` 引用包含实际路径、生产者、内容版本、状态及已测的 schema/CRS。不同运行的 `result` 不可混用；优先使用精确引用或完整返回路径。已知失败、超时或部分完成运行的文件可用于排错，但不能作为已完成的分析成果进入制图或交付。普通原始数据不要求具有生产者清单。

火灾前后比较使用 `geo_accessibility(action="compare", objective="distance"或"time", ...)`，明确选择危险区时次。两种情景从原始道路一次生成共同的接入候选集；危险情景只屏蔽道路及连接线，不能引入基线中不存在的新接入选择。工具输出逐居民点状态转移、目的地/接入点变化和目标差值，并检查删边情景的可达性与最优目标单调性。不再把两次独立吸附的结果直接相减解释为绕行效果。`snap_too_far` 只表示当前数据和接入距离限制下没有候选，并非现实中的结构性不可达；直线连接线也不代表已核实的人行道路。原生 `qgis_inputs` 可直接接入地图；成图证据检查核对产物摘要、CRS、数量和居民点状态，不冒充重新分析原始路网。

`geo_python` 每次运行仍是独立进程。跨步骤文件使用 `input_refs={"residents":"实际返回的引用或路径"}`，脚本通过 `INPUTS["residents"]` 读取。`expected_outputs=["risk.csv"]` 声明必须实际生成的相对文件。可用稳定 `stage_id` 与 `depends_on=["上一步ID"]` 建立依赖；前置失败、文件被修改或依赖版本变化会拒绝后续步骤。修复代码后复用原阶段名，不需要重跑无关的成功分析。运行、日志、文件存在与科学分析正确是不同的检查维度。

执行前会检查语法及直接顶层导入的依赖；这不是对任意 Python 的完整静态验证。写出矢量时检查 CRS、有限有效几何及可识别的 ID/名称与源几何的对应关系。`read_vector` 为 Python 数据副本补充稳定的 `__geoh_id` 行标识，不修改源文件，也不改变 `native_layer` 引用的原始字段。排序时保留 GeoDataFrame 的几何列；需要合并统计时，使用 `entity_join(source, table, key="__geoh_id")` 或已有唯一ID，不要将排序后的属性与旧 `geometry.values` 按位置拼接。无法识别稳定键或几何已派生时，对象关联保持未验证；只覆盖部分对象时明确记录部分检查。GeoPandas 的 GeoJSON 写出统一转换到 EPSG:4326；需要保存米制坐标时使用 GeoPackage。原始输入不会被转换覆盖。

`sample_raster_points(path, points, band=1)` 用带 CRS 的点图层采样，返回数值或 NaN；不要用 `Point.buffer(0)` 构造采样窗口。`score_risk(frame, key="id", components=[...], missing_policy="error")` 提供显式的加权评分：每项填写 `field/weight/minimum/maximum/direction`，方向为 `higher` 或 `lower`，权重和必须为1。缺失值可显式选择 `unknown`，不会当作零风险。该函数保留对象几何，只验证计算约束，不证明权重、人口估算或风险模型具有科学有效性。

`geo_accessibility` 提供道路网络可达性分析。输入道路、居民点、避难所及各自唯一非空 ID 字段；明确双向道路假设，或提供方向字段及每种取值的 `forward/reverse/both/closed` 映射，同时明确速度、最大吸附距离与米制投影。可选择具体时次的危险区域，排除与危险区域相交的路段、端点和连接线。输出逐居民点状态、原因、可达避难所数、完整路线和方法假设；距离包括端点连接线，时间分别使用道路与连接线速度。仅连接具有相同 XY 顶点的道路，不自动处理未打断交叉线或猜测桥梁层级。目标可显式选择最短总距离或按声明速度估计的最短时间；容量保留为属性，不进行容量分配。

QGIS 地图可以分步配置：先 `geo_qgis_project(action="spec", title="专题图")` 保存草案，再用返回的 `spec_ref` 与 `patches=[{"path":["layers",0],"value":{"file_path":"实际路径","style":{"color":"#cc3300"}}}]` 逐层增加或修改。补全后以 `action="preflight", spec_ref=...` 进行实际数据和布局检查，再仅用 `action="create", plan_ref=...` 创建。不要在 `plan_ref` 创建时再次提交标题、格式或地图ID；改变配置需重新预检。

同一地图框的显式范围与 `focus_layers` 自动聚焦互斥。名单表的 `fields[0]` 必须是唯一非空键；`columns` 指并排的完整表格数，而不是属性字段数。只显示前N项时必须明确 `top_n` 与 `sort_by`，也可用 `keys` 精确选择；不选择则保留全部。`placement="auto"` 在实测空间不足时自动增添名单页，`pages` 直接分页，`inline` 要求全部放在主页面。完整名单另存 `roster_full.csv`，名单页保留在可编辑工程中并导出页预览；主地图 PNG 对应工程的第1页。空间不足会返回实际行数、容量和尺寸，不通过静默删行解决。地图生成仍无版数上限；相同失败输入不变时应先修正，不机械重复执行。

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
geo_deliver。PDF 可列入 files，主图摘要对应实际 PNG 预览。所有检查和交付沿用同一地图任务；最终 draft/passed 由下面的地图任务回执和实际检查证据决定。

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

系统为地图维护持久 UUID，工具返回的 `map_id`/`job_id` 是该身份；模型提供的名称仅作为别名。后续修改、换文件名、审图、打包和交付应沿用返回的 map_id。存在等待模型处理的工具结果时，精确输入“继续”或“接着做”（也支持 continue/resume）与 `/continue` 一样恢复原任务，不追加新的用户目标，不重复已完成工具。40回合限制是单批运行上限；`agent.auto_continue` 默认 true，有新的成功工具结果时会自动进入下一批，沿用原任务与历史。没有新结果或末组重复失败时停止并保留待处理结果；仅运行目录、时间或长输出回执路径改变不算新进展。网络/API故障和用户取消不会触发自动续跑。设置 false 可保留手动续跑方式。明确的新指令会创建新请求；没有待处理结果的续跑词仍关联当前地图，当前会话没有活动任务时关联工作区最近的持久任务。

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

图片像素相同但工程包内容不同、且导出到新图片路径时，会登记独立版本，保留旧版本及其证据。新包不会凭像素相同继承旧数据/视觉结论；原地改动仍使对应证据过期，完整包内容相同的复制保持原版本。

## 成图检查与自动重绘

新建 QGIS 地图时，系统先将受控读取的图层保存为临时 GPKG/GeoTIFF，再对该数据副本预检并交给 QGIS 渲染；检查与成图使用同一份数据。冻结输入、预检失败不占成图次数，临时副本在读取线程和 QGIS 进程结束后清理。已有用户工程的 inspect/export 保留原数据、样式和布局，导出不覆盖输入工程。

geo_cartography 显式读取成图，结合任务、预期图层、参数和源数据摘要，检查空白、图层可见性/遮挡、图例对应、文字和布局。先准备完整布局，再合并修正可确认的错误/警告，不限制地图版本和修正次数。Matplotlib 和默认 QGIS 路径均可自动应用支持的呈现补丁并重绘；QGIS 使用下文的实际工程、区域和问题证据约束。不会因为 info 提示或单纯审美建议再画一版。报告由工具返回实际路径；Matplotlib 自动修正前地图和预览保存在成图旁独立 `.review-*` 目录。

原生工具、Python 制图及独立审查均不按版本、调用次数或累积审查时长停止。旧配置 `max_repairs`、`max_reviews_per_request`、`total_timeout_seconds` 仍可读取，但不再限制成图或检查。单次API请求和脚本的超时仍有效，用户可取消正在执行的任务。相同图片、审查目标和配置的已完成结果可缓存复用；暂时服务故障使用短期冷却，其他失败不盲目重试。明确修正模型/token/provider参数后重新核对实际条件。持久地图身份、历史版本与回执继续保留。

QGIS 首版前的 `preflight` 会给出有界标注点密度风险，供决定优先级、符号大小和详图；这是规划提示，不是视觉通过证明。`label_style` 支持 `placement`、`distance_mm`、`offset_x_mm/offset_y_mm`、`priority`（0最低、10最高）；`around_point` 使用距离，固定 X/Y 偏移应使用 `over_point`。`style.obstacle=true` 可让未标注的重要几何参与 PAL 避让，但不能保证所有标签显示，也不能阻止线符号本身压盖面。疏散线压盖主体应调整实际线宽/透明度/图层顺序，或在本请求内改用详图/分图。

主图和详图可分别设置 `focus_layers`、`render_layers`、`label_layers`；本框 `label_layers=[]` 保留符号并关闭文字，另一框可继续显示这些标注。`detail_minimum_zoom` 以最终主图比例尺÷详图比例尺核验实际放大倍数，建议详图设置为大于1；未指定时兼容区位插图。启用该检查后，图例不足优先扩大页面高度，并记录实际适配，不悄悄缩小详图。

迭代地图优先使用同一套 `geo_qgis_project` 契约：`action=spec` 保存草案，`action=inspect,spec_ref=...` 查看当前图层和完整参数，再用 `edits` 修改草案。支持 `operation=layer/style/labels/frame/roster`；图层由返回的 `layer_key` 定位，颜色微调保留原有分类，换数据路径保留未明确修改的样式。高级 `patches` 入口继续保留。`action=help,section=style/labels/frame/layout/edits` 返回实际运行模型的对应 Schema；QGIS Processing 的算法帮助不描述地图布局接口。预检后仍仅用返回的 `action+plan_ref` 创建。工作区中的摘要校验草案会进入后续请求的数据上下文，可从原草案继续；不要重新拼接时间戳路径。

每个图框可声明 `purpose` 与 `requirements`。例如火场详图的要求为 `{"layer_index":"返回的火场layer_key","coverage":"complete"}`，系统会核验该框完整覆盖火场几何；`intersects` 只要求达到 `minimum_visible_features`，不能据此声称完整覆盖。可使用 `key_field` 和 `feature_keys` 声明具体对象子集。图框选择和要求中的图层既可用精确 `layer_key`，也可用零起始索引；保存草案后转为稳定标识，图层重排不会把火场要求转给道路。主图出现火场不能替代详图的火场要求。短 `frame` 编辑要求明确用途与主体；原有直接布局调用仍兼容。

视觉建议中的放大倍数使用 `detail_zoom_policy="advisory"`，报告实际比例而不只为放大倍数无限扩大纸张；明确的硬性要求使用 `"hard"`（旧接口默认行为）。可用 `auto_expand_max_ratio` 限定纸面自动扩展比例。详图无法与主图共页时，`detail_placement="page"` 建立真实可编辑的独立详图页，并保留对应预览；名单页继续排在已有地图页之后。主 PNG 只表示第一页，其他页须查看各自预览，不能把第一页审查结果当作全部页面均已审查。

多页工程按 `page_previews` 返回的实际路径，对 `frames` 非空的每一地图页调用 `geo_map_review`，保持相同 `map_id`。这会登记同一版本的逐页审查，不新建地图版本；只有全部地图页均通过且文件未改变，整体视觉状态才能通过。尚未审其他页时继续审对应页，而不是重复审主 PNG；名单页仍使用原生布局测量。任一地图页或审查报告改变都会使原结论过期。

编号名单使用 `layout.label_table`，与来源说明栏分开。指定 `layer_index`、现有 `fields`（第一个是唯一非空编号）、可选 `headers`、并排 `columns`、`height_mm` 和字号；原生QGIS属性表保留在可编辑工程中。`layer.derivation.number_field` 可从选中要素顺序生成1..N编号，不必手写数组。预检检查字段和编号，原生测量检查所有行可见；不会为了通过布局检查截掉名单。

带布局的QGIS预检返回 `plan_ref` 和工作区内 `report_path`。模型可用 `{"action":"create","plan_ref":"返回的引用"}` 创建已检查的地图，避免重复生成长图层数组、路径和布局计划；创建时仍重新检查源数据、字体及计划。修改参数需重新预检。报告文件以摘要绑定，修改后引用失效；Python仅获得确切报告的读取权限。`qgis.plans_dir` 默认 `.geoharness/qgis-plans`，须为当前工作区子目录。原生分析及QGIS图层的 `file_path` 也支持当前已登记的 `data:` 引用。

短引用创建仅传 `action` 和 `plan_ref`；预检计划已经携带已检查的 `map_id`、`title` 和输出格式/默认值，不再重复这些字段。通用的保留地图身份要求不向短引用增加字段。冗余参数会明确列出，需修改标题、格式或样式时重新预检。标签优先使用 `placement="auto"`；`around_point`/`over_point` 只用于点，`parallel` 只用于线，面可用 `horizontal`。检查针对 derivation 筛选后实际绘制的几何。颜色支持 Qt 名称、十六进制色及 `rgba(r,g,b,a)`；rgba 的 RGB 为 0–255 整数、alpha 为 0–1，入口统一转换为 Qt 的 `#AARRGGBB`。

向量分类参数应放在 `layers[i].style` 内。为兼容确定性的层级误写，入口仅将外层的 `renderer`、`field`、`categories` 归入 `style`；内外相同可合并，冲突则拒绝，其他未知字段仍拒绝。预检的 `parameter_normalizations` 记录具体图层和移动字段，原输入对象不被改写，分类字段和值仍接受完整校验。

详图未达到最小放大倍数时，默认先联合调整纸面尺寸和详图比例，保留请求的地理范围、图层、分类和标签，计算保留1%余量。微小坐标序列化误差使用绝对1e-5、相对5e-6的容差；1.99999531可视为两倍，1.991仍不足。不能适配或关闭自动适配时，诊断返回实际图框尺寸、范围、CRS、比例尺与放大倍数（错误文字保留八位小数），并给出扩大图框、候选范围或分图建议。候选范围需重新核验主体覆盖，不自动裁掉必要内容或降低要求。页面、侧栏、名单表、详图范围或比例改变后，旧建议失效，需重新预检；不要手动舍入候选坐标。

大型工程的成功回复只列出产物路径、图层计数及实际布局范围/比例尺等摘要，完整来源与标注证据保存在 `project_manifest` 指向的工作区清单中，可用 Python 按确切路径读取。失败回复显示具体错误、实际诊断文件路径及有关的必需/可用尺寸、字体加载、有效参数或比例尺建议，省略无关的全部图例和堆栈；完整回执保留在元数据和实际诊断文件中。短引用每次重新检查，不会被引用展开前的旧错误缓存挡住。非法JSON会提示可信的解析原因及位置，不执行该调用，不猜测修补字段或坐标；成功调用保留。

`geo_map_review` 只读取实际 PNG/JPEG/WebP 图片并报告可见问题：文字不可读、标签/符号重叠、图层压盖、内容裁切、异常空白及图例不对应。返回 `status`、`summary`、`issues`、`limitations` 和报告路径。它不生成修复参数，不改变样式或数据，不重绘、不选择其它版本。后续修改由调用者依据问题，通过制图工具显式执行。

审图模型直接接收整页图及有界的原分辨率地图框、图例和底栏裁图。请求只带当前页用途和检查目标，不发送逐点工程表、完整分析报告或参数规划schema；工程清单留在本地用于页/图片摘要绑定。不能凭图片证明真实坐标、统计数值或全部图外要素，无法判断的部分写入 `limitations`。结构检查、视觉检查和分析正确性分别报告。

`map_review.auto_repair` 已废弃且不再生效；旧调用中的 `auto_repair=false` 仍兼容，`true` 明确报只读错误。旧 `evidence` 参数兼容接收，但不会将任意工程表发送到图片模型；正常调用只需 `file_path`、`task`，登记工程沿用 `map_id`。旧修复建议不再作为本工具的输出字段。未知/非法返回结构仍为 `unverified`。

审查采用流式接收。`timeout_seconds` 是图片准备和生成的总截止（默认180秒），`read_timeout_seconds` 是网络空闲超时（默认90秒）。持续收到思考增量不表示审查已完成。诊断记录首响应、首思考、首答案时间、用量和完成原因，不保存思考正文；区分 `total_deadline`、`transport_timeout`、`output_budget` 和不完整流。失败保持 `unverified`，不自动降低模型或思考强度，也不自动追加重绘。

当前 `max` 模型的实测中，16384输出token可能全部被思考占用而没有最终答案。按后续要求，审图输出预算从32768提高四倍，配置示例使用 `map_review.max_tokens: 131072`，工具配置也允许此上限；这是生成预算上限，并不要求每次耗尽。工具尊重实际配置，诊断中的 `output_token_limit/output_tokens/reasoning_tokens/finish_reason` 可区分预算耗尽与网络失败。实际可用上限仍由模型服务决定，增加预算仍受180秒总截止约束，不保证任意输入都能完成。

有放大图和密集重点点时，Python 可使用 `geoharness.cartography.layout` 的 create_map_figure、place_point_labels、save_map_figure。它预留主图、放大图、图例、说明的独立区域，按渲染后的文字边界避让；放不下的名字保留省略记录，使用编号对应名单。保存 `.layout.json` 记录分区相交、标签相交/裁切和图例/说明越界，与实际图片 SHA256 绑定。布局报告用于制图阶段的几何检查；geo_map_review 仍直接检查图片，不以该报告代替或阻断像素审查。

数据核验、布局几何检查与视觉审查分别报告。颜色检出可能来自图例，非白像素比例也不能证明图层、分级或位置正确。视觉服务失败时交付标为 **draft**（未完成视觉审查的草稿），说明实际原因和剩余问题；不得用像素计数替代通过结论。已知布局缺陷仍可统一修复，停止的是无效视觉重试。

图片审查按精简的问题schema验证；修复参数和其它未知字段不能被当作有效审查结论。非法JSON、缺失结论或不完整流保持未验证。

不会因图片建议自动更换数据、统计方法、字段或影响阈值。视觉检查不能证明数值正确，必须同时核验分析结果。

向量检查返回实际字段类型和空值数量；登记的原生分析输出也带schema。`geo_analysis` 的 `source.filters` / `reference.filters` 可按现有属性精确选择时间或情景，匹配为空不会自动使用全部时段。distance/impact返回 `reference_relation`（covered_by、intersects、outside）；可用 `distance_breaks_m: [50, 1000]` 生成 `distance_class`，范围内部与外部距离分级分开。`reference_scope` 记录所选参考集合和合并方式。成图事实会从落盘几何复算关系、距离及分类；图例名称应表达这一实际口径，不能把外部邻近点称为范围内。

`geo_vector_data` 的 load/inspect 均返回实际路径、格式、全部字段名和类型、空值数量、CRS、范围及要素/记录数；不再只显示前20或40个字段。`geo_raster_data` 的 inspect/stats 均附带源路径、格式、CRS、范围、尺寸、分辨率、各波段类型/NoData/单位等头信息。`geo_db_data` 的 load/inspect 返回表定位符、字段及空间信息；数据库没有本地文件路径，load的范围和数量描述已加载的行，并标出读取上限。完整描述同时放在工具元数据的source_detail中，未知信息明确标为unknown，不提供连接凭证。list保留发现索引，不为列目录额外读取全部数据。

向量描述还返回 `encoding`、`encoding_source`、`encoding_status`。Shapefile 按每个文件自己的 `.cpg` 声明读取，GeoJSON/JSON 使用格式规定的 UTF-8；没有有效声明时明确 unknown，由驱动按默认方式读取，不猜测整目录编码。登记的 DATA_REFS 保留编码，`read_vector(ref)` 自动采用它；不要为所有图层统一强制 GBK 或 UTF-8。声明变化会使旧数据引用失效，需重新加载。

向量按路径或名称 load/inspect 后，注册 `name` 统一为实际文件短名（不带扩展名），`requested_name` 保留原请求，`path` 保留绝对路径。Python助手接受稳定ID、准确路径、唯一注册名/原请求名/文件名/短名；兼容旧记录中 name 为完整路径的情况。同名文件不猜测，须使用稳定ID或完整路径，不做中英文引号或相似字符替换。文件签名、编码声明、生产清单和结果摘要变化仍会使引用失效。

每次 `geo_python` 都启动独立进程，上一段脚本定义的函数或变量不会保留。提供的GIS辅助函数是 `resolve_data`、`read_vector`、`native_layer`、`mask_raster`；`print_stats` 不是内置函数。栅格统计应单独调用 `geo_raster_data action=stats`，或在脚本中明确导入 Rasterio/NumPy 编写计算。失败回执优先显示 `error_summary` 和 `recovery_hints`，完整输出及执行信息保留；不要把后半段引用/函数错误当成前面已成功的栅格统计失败。

数据已登记时直接使用 `read_vector(ref)` 或 `resolve_data(ref)`，不再遍历 `DATA_DIRS` 猜路径；`DATA_DIRS` 是读取根目录，文件可能位于子目录。手动搜索必须先检查未命中情况，再拼接或读取路径。路径为 None 的类型错误会提示该定位问题，不当成编码或权限故障。

`geo_analysis` 成功结果提供 `qgis_inputs`，包含实际原生产物的 `layers` 和对应 `analysis_manifests`，可合并进 QGIS 预检参数。只合并确实上图的产物及清单，样式直接附在原生图层上；筛选、保留字段或新增注记使用 `layer.derivation`。不要为调整外观先手动重写文件，也不要把没有上图的统计清单绑定到地图。

登记的原生分析结果引用还包含 `analysis_manifest`。QGIS 使用该 `data:` 引用时自动带入对应清单，并核验结果和生产清单的签名/摘要；确切原生结果路径沿用已有清单推断及严格内容核验。预检返回 `analysis_binding_note`。Python产物或原始Shapefile不会因此自动变成原生分析结果；显式传入的清单仍保留并逐份核验，不静默替换图层。

分析清单与实际图层不匹配时，系统根据清单记录的原始输入路径给出候选替换建议；只有唯一对应时才列出具体 `layers[i].file_path`。这是恢复建议，不代表原始数据与结果等价，也不会自动替换。模型需保留样式、选择正确结果重新预检；没有确定对应时仅列候选，不凭名称猜测。

原生分析的source/reference/zones推荐使用对象；若聚合服务或模型多编码为一层合法JSON对象字符串，入口会解析一次，再执行原有定位符/权限/CRS/参数校验。多重编码、非对象、非法JSON和未知字段仍拒绝。Python读取None会明确提示未选中数据源，应使用返回路径或read_vector(name/ref)，不把这类错误解释为文件权限故障。

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

503 等暂时服务故障和明确网络/连接超时使用 `map_review.service_failure_ttl_seconds`（默认60秒）冷却；服务端 `Retry-After` 可延长。冷却按审图模型、服务地址和凭证身份隔离，跨改名、重新导出、普通任务重置和会话恢复保留，不通过重绘绕过。冷却到期后的下一次审图调用可重新请求，不在工具内循环等待。`geo_map_review(retry_service_failure=true, ...)` 表达明确复审意图，只检查同一图片、不启动自动重绘，不越过冷却，也不会强行重试400/401等非暂时错误；它不替代 JSON/schema 专用恢复开关。

回执的 `visual_review_completed`、`request_sent`、`image_provider_calls`/`image_cache_hits`、`request_provider_calls`/`request_cache_hits` 分别说明视觉检查完成情况、是否调用服务、当前任务中该图及整个任务的客户端调用/缓存次数；不推断SDK内部HTTP次数。请求已开始后被取消也属于一次客户端调用，取消继续生效。`service_retry_at`、`service_retry_after_seconds`、`service_retry_allowed`、`next_action` 给出实际恢复条件；`quality_note` 提醒服务失败不能证明地图正确。普通“继续”不保证发新请求或升级质量。通过/有问题的完成审查可在同图、同审查目标与配置下复用内存结果；目标变化重新审查，持久快照恢复仍沿用须核验当前视觉证据的规则。

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

新建布局默认启用 `legend_auto_fit`：根据 QGIS 实测图例、名单表、底栏和详图比例尺联合计算纸面空间。必要时增加页宽、页高或调整详图比例；加宽侧栏时保留主图宽度，名单表及底栏增长时同步保留地图高度。不缩字号、不删类别、不裁请求的地理范围。名单使用原生表格实际尺寸计算；内部尺寸求解限制迭代并检测重复状态，无法容纳时保留明确失败，不限制地图版本数。`legend_auto_fit=false` 保持请求尺寸、不自动扩展，已冻结计划也不重新适配。`effective_parameters`、`adaptations` 记录最终参数及原因。`legend_font_size_pt` 单独控制图例，`body_font_size_pt` 控制底栏；同时传常规 `layout.legend_font_size` 时，只有明确的 `qgis_layout.legend_font_size_pt` 优先于它。

工程编辑后的有效参数读取实际元素；纸张尺寸统一为mm，文字保留实际值与单位，仅Points字号另提供pt字段。`creation_parameters` 保存创建模板参数。硬布局检查支持毫米场景；用户将整个布局场景单位改成其他单位时，导出保留布局，检查状态为未核验并返回原因，不误报通过。

QGIS 原生新建/导出标记 `project_fidelity=saved_layout`，PNG、PDF 和预览从保存后重新打开的同一布局导出。给现有 Python/Matplotlib 图片补工程的 `action=project` 保留兼容，标记 `project_fidelity=reference`：QGIS 布局是参考布局，包内预览是原图副本，不能据此认定两者一致。需要同源交付请使用默认 QGIS 制图，再检查实际预览。

同一请求内，相同参数和未变化的本地输入触发同一确定性检查错误时，工具返回已有诊断并标记 `duplicate_error_blocked`，不重复执行该检查或重绘。文件内容摘要和状态共同判断输入是否变化，保留时间戳的同大小修改也会使缓存失效。修改参数、数据、关联分析输出或读取配置后可以重新执行。文件锁、超时等暂时故障不会按确定性错误缓存；没有稳定本地身份的目录别名、含外部依赖的栅格/VRT、已有工程的 inspect/export、plan_ref 短引用和超过64MiB总读取上限的输入保留正常执行。该机制帮助避免无效循环，不能保证模型每次都给出正确参数。

`geo_cartography(action="preflight", ...)` 与 `geo_qgis_project(action="preflight", ...)` 可先检查实际数据，不绘图、不生成工程。正常新建地图会在渲染前自动先执行同样的检查。检查 CRS、有效/有限几何、符号与标签字段、分类/分级、数值类型、指定要素总数，并返回主图/详图中的几何相交数量。相交计数不证明符号没有被遮挡；栅格只核验范围相交，不据此宣称像素值或语义正确。

显式 `map_extent` 使用 `{xmin,ymin,xmax,ymax}` 或四元组 `[xmin,ymin,xmax,ymax]`，必须同时指定显示坐标系（`geo_cartography.map_crs` 或 `geo_qgis_project.crs`）；`detail_extent` 同属该坐标系。不要直接把 GIS 四元组作为 `imshow.extent`，它的顺序是 `[xmin,xmax,ymin,ymax]`。没有指定范围时按对齐后的图层自动生成，单点/零跨度通过配置中的 minimum_span_geographic/minimum_span_projected 扩展。

图层的 `expected_feature_count` 用来核对实际总数，`minimum_visible_features` 控制主图或详图最少显示数；必需图层默认为1。确实允许空结果或界外背景时明确 `required=false`，报告会保留零计数/警告。`geo_cartography` 可以在内存副本中将无损数值字符串转为数值，不改源文件；直接 QGIS graduated/flow 字段必须已是数值类型。

PNG/PDF 默认使用 QGIS。配置 `cartography.static_renderer: qgis`，调用 `geo_cartography` 时可指定 `map_crs` 和 `qgis_layout`。`qgis_layout.detail_extent` 使用 `{xmin,ymin,xmax,ymax}`，与主图范围同属显示坐标系；`footer_text` 提供备注。主图、可选详图、图例和底栏分区生成，类别颜色复用已有桥接规则，中文字体与标注缓冲/放置由 QGIS 处理。这些设置不证明每个标签都已显示或位置正确，仍须检查实际成图。

返回的 `project_file` 是真实 QGIS 工程，`preview_path` 是从它的打印布局导出的 PNG。修改工程的图层、样式或打印布局并保存后，使用 `geo_qgis_project(action="export",project_path="...")` 导出新工程和图片。交付以返回的 `primary_map` PNG 与 `project_dir` 为主，PDF 可附加。视觉审查未完成时仍为草稿。

需要旧静态路径时指定 `renderer="matplotlib"` 或配置 `cartography.static_renderer: matplotlib`；HTML/SVG 保留原有渲染入口。QGIS 禁用或启动失败时，默认路径会给出具体错误。

QGIS 新建模板会在导出前检查自身分区、图例/文字内容溢出及元素边界。对用户已保存的工程，inspect/export 保留真实布局并在 bounds_check 中记录问题，允许合法的地图内注记或手动移动图例；非结构化模板可能返回 checked=false。有问题或缺少有效检查时按草稿处理。bounds_check 只覆盖报告中的布局元素与边界，不证明所有地图标签、图层语义或分析结果正确。显式 font_file 会随工程打包并由 GeoHarness 重导出时加载。QGIS 桌面需使用已安装字体，或由用户自行安装工程携带的自定义字体；系统默认中文字体无需另行打包。

### QGIS 短回执与详细诊断读取

预检回执优先返回 `plan_ref`、`create_call` 和 `inspect_call`；创建时直接使用返回的 `create_call`。完整报告仍保存到哈希核验的工作区计划文件，文件不存在、链接、内容改变和大小超限分别报错，不需要猜引用或遍历目录。

`geo_qgis_project(action="inspect", plan_ref="实际返回的引用", detail_path=["preflight","frame_checks"], offset=0, limit=8)` 按 JSON 路径读取预检内容；草案同样使用 `spec_ref`，参数从 `["parameters"]` 开始。返回的 `read_call` 下钻大对象，`next_call` 读取下一段。路径仅选择已核验 JSON 对象，不授予额外文件或目录权限。`qgis.inspection_max_chars` 默认2400，控制短回执与诊断视图长度；完整数据保留在原报告中。

帮助增加 `section="layer"` 和 `section="roster"`。简短帮助省略嵌套定义和较长说明，通过返回的 `detail_call` 或 `detail_path=["schema","properties","字段名"]` 读取完整定义；`help(section="edits")` 的完整结构可按 `schemas` 中的实际索引下钻。无需读取全局 `tool_artifacts`。

`geo_map_review` 只保存图片审查报告，不调用证据修复、编译或原生生成。后续显式 build 的失败属于制图阶段，不能解释成视觉服务空响应。

### 可达性结果的场景、字段与解释

`geo_accessibility` 返回实际应用的 `input_selection`、所选面合并及缓冲的 `hazard_scope`、方法参数和状态定义。未指定 `hazards.filters` 时使用全部传入面，不能称为只选最大或最严重时点。`hazard_scope` 分别记录未缓冲所选面与缓冲后危险区相交的居民点数；数量单位为点要素。

结果图层保留英文状态码，并提供 `gh_status_label_zh`（比较表为 `gh_baseline_status_label_zh/gh_hazard_status_label_zh`）。默认中文含义使用“当前模型条件下可达”“位于所选危险范围及指定缓冲区内”等限定描述，不等同真实逃生、实际受火或避难所启用。速度、接驳距离和方向设定仍是显式模型条件，科学正确性保持 `unverified`。

比较表新增实际求解结果的 `gh_baseline_time_min/gh_hazard_time_min` 和 `gh_baseline_distance_m/gh_hazard_distance_m`；不可达值为空。各产物的 `role/scenario/field_roles` 与完整schema保存在manifest，并传到注册数据引用；不要根据merge后缀猜字段。较大回执省略的部分列在 `deferred_sections`，使用返回的 `inspect_contract_call` 通过原生清单读取具体产物schema。`analysis.max_output_chars` 默认12000，完整内容仍保存在manifest。

### 同一地图的编辑控制与恢复

原生草案保存、预检、创建和导出在当前运行时按工作区及服务端地图任务身份互斥。图片审查不持有地图修改控制权，也不触发内部创建。并发手工修改返回 `mutation_conflict`，不会排队后应用过期输入。只读帮助和inspect仍可用。

新预检计划同时绑定当时地图版本与可编辑草案头。绘出新版本或草案被修改后，旧计划返回最新版本、`inspect_call/preflight_call`，需要对最新草案重新预检。旧版未包含绑定的计划保留兼容，创建回执明确标为 `legacy_unbound`。

每次创建返回 `latest_version/latest_spec_ref`，这是下一次编辑的依据；交付候选可以是不同的历史版本。`inspect(spec_ref=...)` 可查看 `mutation_state`，其中记录当前或最近操作、阶段、开始/结束和失败/取消状态。历史草案可以inspect，更新或预检过期草案会给出恢复入口。导出现有历史工程仍保留其实际样式，不自动套用最新草案。

### 完整详图页与只用于名录的数据

`layout.detail_title` 是实际绘出的详图标题，`detail_frame.purpose` 仍只说明分析用途。独立详图页复用主图的标题、图例、比例尺和页脚设置；未给独立标题时使用主标题加“详图”。`legend=false/scale_bar=false` 仍按显式开关执行。侧栏详图指定标题时会为标题预留空间。

图层可设置 `role="table_only"`，默认仍为 `map`。table_only记录保留字段、几何、计数、快照和名录核验，但不扩大地图范围、不进入地图或图例，QGIS工程树中保持未勾选；不能将它用作地图框的render/focus/labels/requirements。无需使用0.01mm符号隐藏名录层。

布局回执的 `page_completeness` 核对每个地图页的实际组件及关联地图，`roster_selection.membership_check` 比较实际表格中的对象身份集合和选中集合，区别于仅检查行数。它们不证明像素辨识度或分析结论正确。对旧工程export保留原布局，不自动补齐其页面元素。

### 图片审查与后续编辑

图片审查输出可见问题、位置、严重度及无法判断的部分，不输出修复参数，也不据工程对象推断图片已经清晰。报告绑定实际图片摘要；图片或工程绑定变化后需重新审查。

主模型可在读取同轮报告后合并必要修改，再显式调用 build。服务失败或未发现新问题时，不据此追加重绘。业务结论、像素可读性及整项任务是否完成仍分别验收。

### 地图台账健康检查与恢复

地图版本和完整报告存入任务目录中的 ledger.sqlite3；receipt.json 只保存固定大小的定位信息。任务头与版本记录由SQLite事务提交，同一报告按SHA256去重。既有工具仍返回原来的字典结构，读取当前修改状态不加载历史报告。需要比较候选版本的旧接口仍会读取相关历史证据。

使用 geo_qgis_project(action="inspect", map_id="实际返回的地图身份") 查询台账健康、当前版本和分页历史。即使旧台账超过普通读取上限，该健康入口也不会先解析整个旧文件。返回 recover_call 时，action="recover" 只迁移该地图台账；它保留原JSON备份，在临时库核验全部记录、身份和内容后切换，幂等调用不会创建新地图。普通inspect不会自动迁移。旧输入的普通读上限仍为16MiB，显式迁移有128MiB独立边界；更大记录需要单独处理，不能关闭所有读取保护。

诊断输出超过 qgis.inspection_max_chars 时返回有界视图和实际 read_call。recover 只改变内部状态存储，不重画地图，不修正原分析结果或提高质量状态。

图片已渲染而登记失败时，回执保留实际图片路径、render_executed和receipt_status，不再统一称为未开始。图片审查可在台账不可读情况下保存独立像素报告；其地图质量保持draft、登记状态为unregistered，不认证整个工程。

交付默认仍按已核验候选规则执行。若台账不可读，调用者可以显式指定version_policy="selected"并给出实际文件，保存带ledger_errors的草稿恢复包；它不执行历史候选替换、不宣称质量通过。原始台账、旧地图和先前交付目录不会被此降级路径清空。


## 分析输入与事实核对

每次 `geo_python` 都使用新的 `RUN_DIR`。跨步骤先声明 `input_refs={alias: 实际返回的ref或路径}`，再读 `INPUTS[alias]`。可用 `input_requirements={alias:{fields:[...],key:"id",crs:"EPSG:32648",metric:true,valid_geometry:true}}` 核对实际文件；字段缺失会列出实际schema，CRS不一致会拒绝执行，不会自动重贴CRS。CSV可检查fields/key/finite_fields，矢量还可声明layer/crs/metric/valid_geometry。

`extent_geometry(west=...,south=...,east=...,north=...,crs=...)` 返回带CRS的GeoSeries；交给 `mask_raster` 会转换到栅格真实CRS。不要把顺序不明的四元组直接传给box，也不要给裸Shapely对象猜CRS。

使用 `artifact_facts(INPUTS["result"],key="id",numeric_fields=["d_fire_m"],predicates=[{"name":"within500","field":"d_fire_m","op":"<=","value":500}],metric_crs="EPSG:32648")` 从落盘文件复算行数、缺失数、统计量、条件计数及面积（指定metric_crs时）。面积为逐要素面积之和，重叠可能重复；不是自动求并集或重新预测火势。返回源文件SHA256和验证范围。`check_claims(facts,[{"path":["counts","within500","count"],"expected":13}])` 对比明确声明并返回实际值及是否相符；空声明不会通过。将facts、claims结果和评分方法另存为JSON，加入 `expected_outputs` 和交付files。此检查不审计任意自然语言，也不证明评分权重或风险方法科学有效。缺失路网时间仍应保留unknown，不应无说明地填充为某个分钟数。


## 名录身份、布局进展与逐页审核

`layout.label_table.key_field` 可指定稳定、唯一且非空的源ID，独立于 `fields` 中的显示编号；省略时仍使用第一个显示字段。`keys` 始终按此身份字段选择。可将 `fields` 设为 `["label_no","name"]`、`key_field` 设为 `"id"`；未选记录的label_no可为空，源ID仍须全表唯一且非空。排序、精度和分页不会改变source_keys。field_widths中多写未显示字段时会明确列出错误键及实际显示字段。

布局自动拟合遇到不改变尺寸的候选会停止该候选，返回实测zoom和原因；重复状态返回 `layout_no_progress`。这不放松必要要素覆盖和hard zoom，也不设置累计制图版本上限。应根据诊断换分页、纸张或详图布局，而不是反复提交同一输入。

原生导出为每个图框记录当前页、purpose和实际启用标签的label_sources（包含图框样式覆盖），并保留实际标签落位。审核只对当前页图片负责；总览关闭编号时不会授权加回编号的修复。`label_not_placed` 需绑定原生feature_ids，已落位的工程断言不能驱动修改；标签可读性或压盖仍需原图pixel证据。矛盾意见保留在诊断，不自动转为视觉通过。多页地图仍须逐页检查才有完整视觉结论。severity_reason仅为可选说明，不授权任何动作。


若复制成果已经完成而台账登记在最后失败，交付仍返回实际目录和delivery.json，标记receipt_status=failed、quality_status=draft并保留ledger_errors；这不把草稿提升为已核验成果。活跃修改期间不能迁移同一地图台账，普通只读健康查询仍可使用。无效矢量输出会给出有界行号和有效性原因；坐标变换后应再次验证，不能因原投影中有效就跳过目标投影检查。

交付复制结束后的台账读取或登记失败，会返回 `delivery_status="copied_unregistered"`、`receipt_status="failed"`、`quality_status="draft"`、真实目录和 `next_action`。此时不生成过时的 `map-receipt.json`；先保存已返回的文件，再检查或恢复台账。复制前默认候选校验仍会拒绝不可读台账，显式selected草稿恢复除外。

## 工具失败的紧凑显示

交互终端中的失败工具调用默认显示为 `geo_python … × → 76L`：小 `×` 表示失败，
`NL` 是该工具返回文本的实际非空行数。错误正文不自动展开，恢复的历史错误也同样折叠。
完整错误仍保留在模型上下文、会话与工具运行记录中，真实失败状态不变。
打印模式的失败结果也使用相同摘要；会话级异常仍明确显示。正常结果显示沿用原行为。

## 调用前易错提醒与精简帮助

`geo_python` 与 `geo_qgis_project` 的关键注意事项同时进入系统提示和工具说明。
Python 强调每次运行都是新进程、使用真实返回路径、核对字段/CRS/API签名，
以及 `rasterio.read(1)` 的二维结果与 `mask_raster` 的三维结果区别。错误 API 和数组形状
有分别的恢复建议，不把相同代码换名重试当作修复。

`geo_qgis_project action=help` 的 `cautions` 始终保留当前章节的关键组合规则，
即使 schema 需要分页；`detail_call` 可读取包含字段说明/defaults 的完整原始 schema。
`workflow` 帮助提供推荐短调用字段，完整旧输入仍兼容。重点是 FIT 与显式范围互斥、
render 必须包含 focus/label、只读诊断选择器不能带进修改请求、冻结创建只用返回的
`action+plan_ref`，以及名录稳定键和真实布局测量。部分跨字段错误会返回精确字段、
关联字段和实际缺失图层，不静默修改用户要求。

## Python 栅格快捷操作

`read_raster(path, bands=None, out_shape=None, resampling="nearest", geometries=None)`
返回 `RasterData`，包含 `array`、`crs`、`transform`、`nodata`。`array` 始终是带 mask 的
`(bands, rows, columns)` 三维数组；`bands=1` 也保留波段轴。`out_shape` 是 `(rows, columns)`，
改变分辨率时同步更新变换矩阵；裁剪使用带 CRS 的 GeoSeries/GeoDataFrame，并变换到源栅格 CRS。
裁剪范围对齐实际像元窗口，坐标边界附近的浮点舍入可能增加一个边界像元，仍以返回 transform 为准。

`write_raster(path, data, crs=None, transform=None, nodata=...)` 写 GeoTIFF。
`data` 可直接使用 `read_raster` 返回值；原始数组必须是三维并显式提供 CRS 与 Affine。
该接口拒绝把二维 RGB 错误数组当作单波段、拒绝无 CRS/无效变换、拒绝有效像元与 NoData 冲突。
原始 Rasterio 仍可使用；`read(1)` 是二维，`mask_raster(..., indexes=1)` 也可为二维，
不能不看实际 `shape` 就追加 `[0]`。

`raster_backdrop(source, output, band=1, out_shape=None, geometries=None, percentiles=(2,98),
rgb=False, resampling="nearest")` 生成显示用灰度/RGB底图，保留地理信息和掩膜，
零值保留给 NoData，有效灰度使用 1–255。它是显示拉伸，不是分析重分类或风险计算；
不修改源栅格，插值方法由调用者按数据类型选择。

TIFF 输出会读回检查 band/row/column、CRS 和变换矩阵；这只验证栅格元信息契约，
不代表所有像元或科学分析正确。错误分类区分 API 参数、数组维度、权限等确定性原因，
只有已识别的瞬态失败建议原样重试。

## 覆盖与布局失败的实测候选

主体覆盖或硬性详图放大要求失败时，原生工具最多试测三种局部方案，例如保留声明对象
的完整范围、详图独立页、名录独立页或扩大地图框。试测不导出 PNG，也不会自动应用方案。
`diagnostics.candidates` 区分 `native_preflight_passed` 与失败候选，记录实际图框、放大倍数、
主体校验与纸张结果。旧公式建议单独标为 `unverified_formula`，不能当作已验证方案。

从 `spec_ref` 预检失败时，`candidate_edit_calls` 只给出试测通过的候选，并绑定
`base_spec_ref` 和输入摘要。原样使用其中的 `edit_call` 会建立新规范，再预检并使用新的
`create_call`；原规范保持不变。不要把旧候选的坐标拼到另一套纸张、样式或数据上。

范围候选使用完整声明的选择集及原来显式的 focus 对象，不减少最低数量、不改 coverage，
不降低 hard zoom。未给 `feature_keys` 的要求使用整个来源集合，可能产生较宽的范围；
需要局部详图时应明确稳定键选择。缺失或重复身份、源要素不足和图层未绘制不能靠布局修好，
这种情况下不会返回虚假的范围修复。多页成果仍须分别审核像素后才能给出完整视觉结论。

## 思考强度、快速构建与任务时间目标

`agent.reasoning_effort: max` 显式发送 `thinking.type=enabled` 和
`reasoning_effort=max`，并让空响应及截断参数恢复保持主模型与思考模式，
不使用配置中的 none 备用模型。未设置此字段时保留旧恢复行为。
`map_review.reasoning_effort` 单独配置；示例配置将主模型与审图均设为 max。

图片审查可以使用独立模型和服务。在用户配置文件 `~/.geoharness/config.yaml` 中
直接编辑 `map_review`；Windows 默认位置是用户目录下的 `.geoharness/config.yaml`。
无需修改代码。以下模型名是占位符，必须换成服务提供方实际支持的标识：

```yaml
map_review:
  enabled: true
  model: "实际审图模型标识"
  base_url: "实际 OpenAI 兼容 API Base URL"
  api_key: "${GEOH_REVIEW_API_KEY}"
  reasoning_effort: max
  request_extra_body: {}
  response_format: tool
  max_tokens: 131072
  timeout_seconds: 180
  read_timeout_seconds: 90
```

`model/base_url/api_key` 中每个空字段分别继承主配置对应字段。可直接填写 API key，
也可通过私有 `.env` 引用；诊断摘要不显示密钥或服务地址。当前审图客户端使用
OpenAI 兼容的 Chat Completions 图片接口，不能直接填其他协议的原生端点。
主模型及 `agent.reasoning_effort` 不受这些字段影响。

`reasoning_effort: null` 表示不强制发送统一思考参数，不等于关闭思考；此时
`request_extra_body` 原样生效（其中若仍包含 `thinking/reasoning_effort`，它们仍会发送）。
显式 `max` 会启用思考并覆盖旧扩展中的冲突值。更换模型时，应按照实际提供方能力
设置思考扩展与输出预算，不自动降强度、不自动换模型或重试另一种返回格式。
审图 `reasoning_effort` 和 `response_format` 忽略大小写及两端空格；模型标识原样发送。

`response_format: tool` 使用 `record_map_review` 函数调用；`json` 不发送函数，
使用 JSON 文本（提供方需支持 `response_format: {type: json_object}`）。两者使用相同
问题结构及本地校验；不完整、非法或截断结果均为 `unverified`。改变这些设置会使旧
审图缓存失效。保存配置后重启运行中的 GeoHarness 会话，新会话和每次 CLI 诊断读取新值。

```powershell
# 只读配置检查，不联网；显示实际配置路径、模型来源和参数
geoh review-doctor
# 显式联网：随机小图检查数字与颜色/形状识别，不认证地图质量
geoh review-doctor --live
# 显式联网：只检查指定图片，不渲染、不写业务地图台账
geoh review-doctor --live --image "实际地图.png"
# 如启动时指定了其他配置，诊断时使用同一个路径
geoh review-doctor --config "实际配置.yaml"
```

每次联网诊断只有一次模型请求，完整结果保存在返回的独立报告路径。
实际地图出现 `issues` 仍可表示审查已完成，不能把命令成功等同于地图无问题。
流式诊断记录首响应、最后事件、推理/回答字符数和完成标记，不保存思考正文。
`failure_kind=total_deadline` 且阶段为 `generation` 表示已接收响应但总时限到达；
`response_tail` 表示完成标记之后收流尚未结束，均不能统称网络服务故障。
模型别名是否由第三方网关正确解释仍需核验实际响应，字段本身不证明服务端执行了 max。
`max_tokens` 是思考与回答共用的输出上限，和思考强度不同；预算耗尽会明确保持未完成状态。

主请求的最近64条安全计时保存在会话 `tool_metadata.geo_request_metrics`，包含请求模型、
返回模型、请求强度、首个内容/思考/工具增量的时间、请求耗时、token用量和错误类型。
服务端未返回 reasoning tokens 时记录为未知，不推断为0。记录不包含提示词、脚本、
凭证或思考正文。审图仍在对应报告中记录请求模型、思考强度及实际用量/耗时。

推荐先用 `spec` 保存初始草案，再调用：

```json
{"action":"build","spec_ref":"实际返回的草案引用","patches":[{"path":["title"],"value":"更新标题"}]}
```

`build` 支持和 `spec` 相同的 typed edits/patches；一次调用完成保存新草案、冻结数据、
数据预检及一个原生进程内的布局验证和导出。成功后不需要再对相同输入执行
`preflight/create`。返回 `latest_spec_ref/build_call` 用于后续修改，旧草案仍会因版本冲突被拒绝。
独立 `spec/preflight/create` 和原生冻结计划接口继续保留。

QGIS 多页成果可用 `geo_map_review(file_path=实际预览路径, task=任务要求,
all_pages=true)` 一次审查同一工程的全部明确返回页面。
工具逐页核对清单和文件摘要后审查，汇总各页问题及完整报告路径；不会猜测文件名、
重画地图或自动应用修改。各页服务请求及台账写入串行执行，保持现有证据绑定。
像素缓存仍按图片、设置和证据核验，图片相同不能绕过清单或源绑定变化。
图片审查始终只读，由主模型合并同轮问题后决定是否显式执行一次 build。
`auto_repair=true` 已移除；旧配置仍可加载但不生效。

审图历史兼容 SQLite locator，并只解冻最近相关页的两份历史摘要；不会为此加载整份台账。
历史读取最多检查最近32个版本，较早的记录仍保留，可经 inspect 分页读取。

`agent.task_budget_seconds: 1200` 设置20分钟任务目标，
`delivery_reserve_seconds: 180` 预留最后3分钟交付。每次模型请求会收到实际已用/剩余时间，
临近交付时优先处理事实阻断并交付已有产物，停止外观微调和追加重建。超过目标后不再自动追加下一批回合。该机制不强杀正在运行的工具，也不保证
任意网络延迟下都能在20分钟完成；已完成与未完成检查分别报告，不能据时间预算宣称质量通过。
