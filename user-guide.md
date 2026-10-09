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

未配置恢复模式时不会自动换模型或盲目扩大预算。持续空回复会说明结束原因、token 消耗
和推理长度；失败请求的 token 也计入会话用量。升级后退出旧进程，重新执行 `geoh run`。
默认 `agent.include_stream_usage: true` 明确请求流式用量；不支持该参数的服务可设为 false，
服务没有返回用量时无法准确统计。恢复期间取消仍结算服务已经返回的用量；正文传输中断
会保留收到的部分内容，并明确提示回答尚未完成，残缺的工具调用不会执行。

React TUI 中，停止、关闭或回收模型流会及时释放网络连接。模型重试流可由不同任务
关闭，不再因 `ContextVar` token 的上下文不一致而产生后台异常。若本轮运行出现未捕获
的异常，后端会显示一次具体错误并结束本轮，界面恢复输入状态；可以继续输入，
已完成的工具不会因这次异常被自动重执行。更新安装后需要退出旧进程并重新启动。
输入前的认知提示 Hook 同样支持空回复恢复；重试状态不会被当成提示正文。
建议性 Hook 的普通失败会记录为失败结果并允许主请求继续，用户停止仍会立即传递；
在 Hook 恢复期间停止，也会结算服务已返回的用量。

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

在 `geo_cartography` 或 `geo_qgis_project` 的新建请求中传入 `analysis_manifests`（最多8个实际 geo_analysis 清单路径），系统会读取对应输出，核对 SHA256、要素计数、impact 的距离阈值/affected 标记及影响选集，并要求至少一份输出确实用于当前地图图层。新清单的输出摘要必须一致；旧清单没有原始 SHA256 时只核对现存输出计数，并明确缩小验证范围。任意 Python/QGIS 清单或自由文字不属于此接口支持的统计证据。

QGIS 说明栏中的方法、数量、阈值和计量 CRS 来自上述核对结果，显示 CRS 单独取当前地图设置。`footer_text` 的自由文字作为备注保留，不能冒充已核对统计。这里验证的是持久输出的一致性，没有重新计算原始空间运算；`analysis_correctness` 仍为 `unverified`。复杂分析仍须另外核验方法、输入与结果。

Python 每次保留 analysis.py、stdout.txt、stderr.txt、manifest.json 和产物清单，默认在 `.geoharness/runs/`。SOURCE_CWD、DATA_DIRS、RUN_DIR 是脚本全局变量，也提供同名环境变量；环境中的 DATA_DIRS 按 os.pathsep 分隔。当前目录为 RUN_DIR，输出只写 RUN_DIR 或相对文件名。配置的分析、制图和既有运行产物目录可只读访问，仍拒绝凭证文件和源文件写入；超时终止并等待子进程。

一次被识别为制图的 Python 脚本可能生成多张图片；系统按最后写出的图片登记一个 primary_map 候选和一个版本，map_candidates 列出全部图片候选。这些附件不是连续修订版；需要审查另一张图时明确选择它。geo_map_review 再次审查已登记旧图会更新该版本的检查，不增加新的成图版本。

工具结果包含 status（succeeded/failed/timed_out）、exit_code、error_summary 和日志路径。界面中的 `✗` 表示该次调用确实报错；只有 stderr 警告而退出码为0时仍算成功。`geo_python {}` 是没有收到完整 code 参数，脚本未执行；可能与服务返回不完整/无效工具参数有关。缩短脚本或改用原生工具，并查看实际错误。主 Agent 的输出预算通过 agent.max_tokens 配置，示例为16384，减少长推理和长脚本竞争同一预算的情况。

运行器清理凭证环境，限制通常的文件/GIS 接口、网络和子进程；原始数据默认只读，修改时生成新文件。它用于可信 Agent 分析脚本，不能隔离恶意 native 代码，**不是 OS 安全沙箱**。

工具定义公开当前 Python 超时上限；通常省略 timeout_seconds。超过上限的参数在执行前拒绝，并显示请求值和允许值，这与实际运行超时不同。工具参数截断、非对象 JSON 与缺少必填字段分别记录错误类别和位置，拒绝的调用不会执行；其它合法调用与已完成结果保留，模型可继续修正参数。

分析和工程发布遇到 Windows 临时访问/共享冲突时，仅重试最后的目录重命名，不重复计算。publication.max_attempts（默认4）与 retry_delay_seconds（默认0.1）控制有限重试；仍失败时保留暂存结果及恢复路径，不自动关闭占用进程或修改权限。

## 地图任务与质量回执

系统为地图维护持久 UUID，工具返回的 `map_id`/`job_id` 是该身份；模型提供的名称仅作为别名。后续修改、换文件名、审图、打包和交付应沿用返回的 map_id。用户输入“继续”或“接着做”（也支持 continue/resume）时关联当前任务，当前会话没有活动任务时关联工作区最近的持久任务。每次真实用户输入都会开启新预算，包括“继续”；内部恢复、空回复重试和自动续跑沿用原预算。

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

缺少证据的阶段为 unverified；布局或视觉检查报告问题时可为 issues，文件改变使旧证据失效时为 stale。`quality_status=passed` 要求同一张图的 data_checked、rendered、layout_checked、visual_checked 全部为 passed；其余为 draft。范围仅为地图数据合同、已测布局几何和图片视觉检查，`analysis_correctness` 永远为 unverified。打包或交付成功不会补齐缺失检查。

修改图片、QGIS 工程、已登记数据副本或交付文件后，旧摘要绑定的证明不能继续用于新文件；状态查询会标记受影响阶段 stale。保存修改后应导出新图，核对数据、布局并审查新图，再重新交付。完整交付包含 `map-receipt.json`；该回执是当时的快照，当前状态以重新查询结果为准。

## 成图检查与自动重绘

新建 QGIS 地图时，系统先将受控读取的图层保存为临时 GPKG/GeoTIFF，再对该数据副本预检并交给 QGIS 渲染；检查与成图使用同一份数据。冻结输入、预检失败不占成图次数，临时副本在读取线程和 QGIS 进程结束后清理。已有用户工程的 inspect/export 保留原数据、样式和布局，导出不覆盖输入工程。

geo_cartography 显式读取成图，结合任务、预期图层、参数和源数据摘要，检查空白、图层可见性/遮挡、图例对应、文字和布局。先准备完整布局，再合并修正可确认的错误/警告，最多两轮，可配置为 0 或 1 轮。Matplotlib 路径可自动应用支持的呈现补丁并重绘；默认 QGIS 路径检查实际布局 PNG，由 Agent 根据具体问题调整支持的样式/布局参数后重绘，当前工具不自动应用审图补丁。不会因为 info 提示或单纯审美建议再画一版。报告由工具返回实际路径；Matplotlib 自动修正前地图和预览保存在成图旁独立 `.review-*` 目录。

原生工具、Python 制图及独立审查共享一次用户请求的预算。当前请求默认最多初版加两轮成功修正；执行失败会释放该次成图预留。所有地图合计默认最多4次模型审查、120秒实际调用耗时；不计空间分析、渲染、生成工程和写报告的间隔，并发调用预留可用时间，完成后按实际耗时结算。预算与持久任务身份分开，改文件名、别名或切换已存在的历史 map_id 都不会补充当前请求额度。相同图片和审图配置的结果会缓存，失败也不会盲目重试；明确修正模型/token/provider参数后允许再次检查。真实用户新输入重置预算，历史地图与回执仍保留。

有放大图和密集重点点时，Python 可使用 `geoharness.cartography.layout` 的 create_map_figure、place_point_labels、save_map_figure。它预留主图、放大图、图例、说明的独立区域，按渲染后的文字边界避让；放不下的名字保留省略记录，使用编号对应名单。保存 `.layout.json` 记录分区相交、标签相交/裁切和图例/说明越界，与实际图片 SHA256 绑定。geo_map_review 先检查对应报告；明确的布局缺陷先修正，再调用视觉服务。

数据核验、布局几何检查与视觉审查分别报告。颜色检出可能来自图例，非白像素比例也不能证明图层、分级或位置正确。视觉服务失败时交付标为 **draft**（未完成视觉审查的草稿），说明实际原因和剩余问题；不得用像素计数替代通过结论。已知布局缺陷仍可在剩余成图版本内统一修复，停止的是无效视觉重试。

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

审图 diagnostics 区分网络、服务响应、JSON、schema、预算和缓存；schema 失败保留字段位置/类型/说明及有界脱敏 JSON。缓存命中明确 request_sent=false，不能写成服务又失败一次。仅 JSON/schema 失败可用 geo_map_review 的 retry_invalid_response=true 显式恢复一次，仍受原预算约束。layout_patch=null 可按空补丁处理，其它错误字段仍拒绝。

已有 Python 地图打包使用 geo_cartography(action="project", map_path="已有地图.png", project_output_dir="exports/工程目录", layers=...)。旧 output_path 作为已有地图输入继续兼容。工程目录必须留在工作区，且必须是尚不存在的新目录；已有空目录也不删除。QGIS 工程检查除图层有效外，也验证项目/地图框 CRS、范围和比例尺；打包成功不表示视觉审查通过。

默认地图和最终交付保存到当前工作目录的 `exports/`。geo_deliver 把明确选定的地图、结果数据、检查报告、脚本和工程复制到独立交付子目录，返回实际路径并保留之前的交付。delivery.outputs_dir 必须是工作目录的子目录；Python 脚本不负责跨目录复制，输出仍限于 RUN_DIR。

默认 QGIS 制图的 PNG 与可编辑工程来自同一打印布局。Python/Matplotlib 自定义地图可用 geo_cartography 的 project 动作，以已存在的原成图和实际图层创建工程，不重新绘图；生成的 QGIS 布局不保证重现所有自定义注记。分类层明确 column（兼容 field_name）、categories 和 colors，原生绘图与 QGIS 使用对应的分类颜色；map_extent 锁定主图范围。不能为创建工程而另画简化图或丢弃分类字段。工程包含：

- `map.qgs` 或 `map.qgz`：QGIS 工程，真实图层、符号样式和可编辑打印布局；以返回的 project_file/qgis_project 为准。
- `data/`：矢量 GeoPackage 和栅格 GeoTIFF 数据副本，使用相对路径，不依赖原始文件位置。
- `project.json`：文件、样式、布局及预览摘要；Python/Matplotlib 工程另有 `redraw.py` 重绘入口。

geo_deliver 可指定 primary_map（兼容 map_path）和 review_path。交付时核对主图、工程 preview 与记录的 SHA256；不同地图或已替换的预览会拒绝交付。交付 quality_status=passed 除了同图数据、布局和实际视觉检查通过，还要求包含的工程与已登记快照一致，复制期间没有变化；其余为 draft。工具同时返回 map_id、map_receipt、review_status 和 receipt_path，工程生成本身不代表质量验收。

在 QGIS 3.x 打开返回的 .qgs/.qgz；双击图层修改颜色、透明度、分级等，通过“项目 → 布局”打开返回的布局，修改标题、图例、地图框、比例尺后导出 PNG/PDF。默认 QGIS 路径的原图即由该工程布局导出；Python/Matplotlib 工程使用另一渲染引擎，自定义注记可能需要参照预览和原始脚本补充。

分享或移动时携带整个工程目录。Python/Matplotlib 工程提供 redraw.py 时，安装 GeoHarness 后可在工程目录运行 `python redraw.py --output edited.png`，从数据副本和 JSON 重绘。此入口读取 project.json，不读取在 QGIS 中保存的样式/布局；在 QGIS 修改样式后应从 QGIS 或 geo_qgis_project 的 export 导出。保存到 GeoPackage 的数据修改可被两种方式读取。重新交付会创建新目录，保留用户之前编辑的工程。

## 后续方向

当前优先核对多图层叠加与影响范围的实际业务流程，空间分布与热点、栅格/地形业务和质量准确率评测延后。认知技能保留领域参考价值，简单叠加任务无需强制进入空间自相关或因果流程。当前实现和回执不代表最终全量或真实业务验收已经通过。

## 默认静态制图

`geo_cartography(action="preflight", ...)` 与 `geo_qgis_project(action="preflight", ...)` 可先检查实际数据，不绘图、不生成工程，也不消耗成图或视觉审查预算。正常新建地图会在预留预算前自动先执行同样的检查。检查 CRS、有效/有限几何、符号与标签字段、分类/分级、数值类型、指定要素总数，并返回主图/详图中的几何相交数量。相交计数不证明符号没有被遮挡；栅格只核验范围相交，不据此宣称像素值或语义正确。

显式 `map_extent` 使用 `{xmin,ymin,xmax,ymax}` 或四元组 `[xmin,ymin,xmax,ymax]`，必须同时指定显示坐标系（`geo_cartography.map_crs` 或 `geo_qgis_project.crs`）；`detail_extent` 同属该坐标系。不要直接把 GIS 四元组作为 `imshow.extent`，它的顺序是 `[xmin,xmax,ymin,ymax]`。没有指定范围时按对齐后的图层自动生成，单点/零跨度通过配置中的 minimum_span_geographic/minimum_span_projected 扩展。

图层的 `expected_feature_count` 用来核对实际总数，`minimum_visible_features` 控制主图或详图最少显示数；必需图层默认为1。确实允许空结果或界外背景时明确 `required=false`，报告会保留零计数/警告。`geo_cartography` 可以在内存副本中将无损数值字符串转为数值，不改源文件；直接 QGIS graduated/flow 字段必须已是数值类型。

PNG/PDF 默认使用 QGIS。配置 `cartography.static_renderer: qgis`，调用 `geo_cartography` 时可指定 `map_crs` 和 `qgis_layout`。`qgis_layout.detail_extent` 使用 `{xmin,ymin,xmax,ymax}`，与主图范围同属显示坐标系；`footer_text` 提供备注。主图、可选详图、图例和底栏分区生成，类别颜色复用已有桥接规则，中文字体与标注缓冲/放置由 QGIS 处理。这些设置不证明每个标签都已显示或位置正确，仍须检查实际成图。

返回的 `project_file` 是真实 QGIS 工程，`preview_path` 是从它的打印布局导出的 PNG。修改工程的图层、样式或打印布局并保存后，使用 `geo_qgis_project(action="export",project_path="...")` 导出新工程和图片。交付以返回的 `primary_map` PNG 与 `project_dir` 为主，PDF 可附加。视觉审查未完成时仍为草稿。

需要旧静态路径时指定 `renderer="matplotlib"` 或配置 `cartography.static_renderer: matplotlib`；HTML/SVG 保留原有渲染入口。QGIS 禁用或启动失败时，默认路径会给出具体错误。

QGIS 新建模板会在导出前检查自身分区、图例/文字内容溢出及元素边界。对用户已保存的工程，inspect/export 保留真实布局并在 bounds_check 中记录问题，允许合法的地图内注记或手动移动图例；非结构化模板可能返回 checked=false。有问题或缺少有效检查时按草稿处理。bounds_check 只覆盖报告中的布局元素与边界，不证明所有地图标签、图层语义或分析结果正确。显式 font_file 会随工程打包并由 GeoHarness 重导出时加载。QGIS 桌面需使用已安装字体，或由用户自行安装工程携带的自定义字体；系统默认中文字体无需另行打包。
