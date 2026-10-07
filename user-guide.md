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

配置、日志、会话及插件/自动任务状态统一保存到 `~/.geoharness/`，不同工作目录的状态分开保存。启动不会再在工作目录创建 `.openharness`。工作目录的 `.geoharness` 保存分析和 Python 运行记录，是有实际用途的目录。

## 一个完整任务

准备具有明确 CRS 的行政区、危险区、居民点或道路文件，向 Agent 提出：

> 分析危险区及其周围500米内的居民点，统计每个行政区的受影响数量。输出资产距离表和影响选集，生成行政区、危险区、受影响居民点的专题图。检查成图并自动修正呈现问题，保留结果和检查报告。

原生 geo_analysis 提供 buffer、clip、intersection、distance、count、impact，不依赖 MCP/PostGIS。复杂组合使用 geo_python。既有数据库和 MCP 接口保留。

impact 的影响条件是相交或最短距离不超过阈值。分区按 intersects 计数，包含边界；跨区/重叠区对象分别计入每区，因此分区数之和不一定等于全局去重数。这项口径不代表因果或风险等级证明。

## 输出与复现

原生分析每次独立保存 GeoJSON、CSV、统计和 manifest，记录来源、参数、计量/导出 CRS 和计数口径；默认目录为工作区内 `.geoharness/analysis/`。未知 CRS 和无效几何会报错，不静默按 WGS84 处理。

Python 每次保留 analysis.py、stdout.txt、stderr.txt、manifest.json 和产物清单，默认在 `.geoharness/runs/`。SOURCE_CWD、DATA_DIRS、RUN_DIR 是脚本全局变量，也提供同名环境变量；环境中的 DATA_DIRS 按 os.pathsep 分隔。当前目录为 RUN_DIR，输出只写 RUN_DIR 或相对文件名。配置的分析、制图和既有运行产物目录可只读访问，仍拒绝凭证文件和源文件写入；超时终止并等待子进程。

工具结果包含 status（succeeded/failed/timed_out）、exit_code、error_summary 和日志路径。界面中的 `✗` 表示该次调用确实报错；只有 stderr 警告而退出码为0时仍算成功。`geo_python {}` 是没有收到完整 code 参数，脚本未执行；可能与服务返回不完整/无效工具参数有关。缩短脚本或改用原生工具，并查看实际错误。主 Agent 的输出预算通过 agent.max_tokens 配置，示例为16384，减少长推理和长脚本竞争同一预算的情况。

运行器清理凭证环境，限制通常的文件/GIS 接口、网络和子进程；原始数据默认只读，修改时生成新文件。它用于可信 Agent 分析脚本，不能隔离恶意 native 代码，**不是 OS 安全沙箱**。

## 成图检查与自动重绘

geo_cartography 显式读取成图，结合任务、预期图层、参数和源数据摘要，检查空白、图层可见性/遮挡、图例对应、文字和布局。先准备完整布局，再合并修正可确认的错误/警告，最多两轮，可配置为 0 或 1 轮。不会因为 info 提示或单纯审美建议再画一版。修正前地图、PNG 预览和 report.json 位于成图旁独立的 `.review-*` 目录；路径通过工具结果返回，不覆盖已有旁侧文件。

原生工具、Python 制图及独立审查共享一次用户请求的预算。每张地图最多初版加两轮修正；所有地图合计默认最多4次模型审查、120秒审查时间窗口。各版本保持同一 map_id，改文件名不会重新获得相同地图的版本预算。相同图片的检查结果会缓存，服务失败也不会对未改变的图片反复重试。检查通过、无法验证或预算耗尽后交付最新版本并说明剩余问题；用户明确提出新修改要求后开启新预算。该时间预算针对图片审查，不包括空间分析和实际渲染耗时。

不会因图片建议自动更换数据、统计方法、字段或影响阈值。视觉检查不能证明数值正确，必须同时核验分析结果。

| 状态 | 含义 |
|---|---|
| passed | 完成视觉检查，没有报告需处理的问题 |
| issues | 仍有具体问题，报告保留原因 |
| unverified | 检查关闭、服务失败、回复无效或无法确认，不表示正确 |

PNG 是首期推荐输出。PDF/SVG 检查同参数 PNG 预览，导出文件本身的排版/字体需另行核验。HTML 没有实际网页截图时仍标为 unverified。Python 自定义地图用 geo_map_review，由 Agent 按问题修改呈现代码、重绘、再检查。

map_review 可单独配置 model、base_url、api_key；留空时复用主配置。示例保持主 Agent 为 `cbcn/deepseek-v4.1-flash(max)`，图片审查使用同一服务的 `cbcn/deepseek-v4.1-flash`，单次45秒、4096 token，减少长思考造成的重复等待。本机一次合成图片检查约7秒返回有效结果，这不代表每张业务地图的耗时。推理模型的 token 预算包含思考消耗；预算耗尽时报告 unverified 和原因，不能当作检查通过。

## 地图交付与手动修改

默认地图和最终交付保存到当前工作目录的 `exports/`。geo_deliver 把明确选定的地图、结果数据、检查报告、脚本和工程复制到独立交付子目录，返回实际路径并保留之前的交付。delivery.outputs_dir 必须是工作目录的子目录；Python 脚本不负责跨目录复制，输出仍限于 RUN_DIR。

原生制图同时生成可移植工程；Python 自定义地图可用 geo_cartography 的 project 动作，以现有成图和实际图层创建工程，不重新绘图。工程包含：

- `map.qgs`：QGIS 工程，真实图层、符号样式和可编辑打印布局。
- `data/`：矢量 GeoPackage 和栅格 GeoTIFF 数据副本，使用相对路径，不依赖原始文件位置。
- `project.json`、`redraw.py`：原生绘图参数及重绘入口；保留原始预览和使用说明。

在 QGIS 3.x 打开 map.qgs；双击图层修改颜色、透明度、分级等，通过“项目 → 布局”打开 GeoHarness Map，修改标题、图例、地图框、比例尺后导出 PNG/PDF。QGIS 与 matplotlib 使用不同渲染引擎，基础布局可编辑，但不保证与原图逐像素一致；Python 的自定义注记可能需要参照预览和原始脚本补充。已使用本机 QGIS 3.44.7 实际读取工程并导出布局验证。

分享或移动时携带整个工程目录。安装 GeoHarness 后，在工程目录运行 `python redraw.py --output edited.png`，可从数据副本和 JSON 重绘。此入口读取 project.json，不读取在 QGIS 中保存的样式/布局；在 QGIS 修改样式后应从 QGIS 导出。保存到 GeoPackage 的数据修改可被两种方式读取。重新交付会创建新目录，保留用户之前编辑的工程。

## 后续方向

下一阶段补充空间分布与热点分析，再做栅格/地形分析。认知技能保留领域参考价值，简单叠加任务无需强制进入空间自相关或因果流程。
