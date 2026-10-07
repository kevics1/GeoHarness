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

在本 worktree 使用系统 Python 时，显式指定源码目录以免读到其他目录的 editable 安装：

```powershell
$env:PYTHONPATH = Join-Path (Get-Location) 'src'
python -m geoharness dry-run --config config.example.yaml
python -m geoharness run --config config.example.yaml --cwd '你的数据目录' --print
```

已有安装也可将 `python -m geoharness` 换成 `geoh`。去掉 --print 使用现有 React TUI。--config 会传到后端，未指定时沿用 `~/.geoharness/config.yaml`。相对数据路径和输出目录按工作目录解析。dry-run 验证组装，不连接 MCP，也不证明模型可用。

## 一个完整任务

准备具有明确 CRS 的行政区、危险区、居民点或道路文件，向 Agent 提出：

> 分析危险区及其周围500米内的居民点，统计每个行政区的受影响数量。输出资产距离表和影响选集，生成行政区、危险区、受影响居民点的专题图。检查成图并自动修正呈现问题，保留结果和检查报告。

原生 geo_analysis 提供 buffer、clip、intersection、distance、count、impact，不依赖 MCP/PostGIS。复杂组合使用 geo_python。既有数据库和 MCP 接口保留。

impact 的影响条件是相交或最短距离不超过阈值。分区按 intersects 计数，包含边界；跨区/重叠区对象分别计入每区，因此分区数之和不一定等于全局去重数。这项口径不代表因果或风险等级证明。

## 输出与复现

原生分析每次独立保存 GeoJSON、CSV、统计和 manifest，记录来源、参数、计量/导出 CRS 和计数口径；默认目录为工作区内 `.geoharness/analysis/`。未知 CRS 和无效几何会报错，不静默按 WGS84 处理。

Python 每次保留 analysis.py、stdout.txt、stderr.txt、manifest.json 和产物清单，默认在 `.geoharness/runs/`。SOURCE_CWD、DATA_DIRS、RUN_DIR 是脚本全局变量；当前目录为 RUN_DIR，输出只写 RUN_DIR 或相对文件名。配置的分析、制图和既有运行产物目录可只读访问，仍拒绝凭证文件和源文件写入；超时终止并等待子进程。

运行器清理凭证环境，限制通常的文件/GIS 接口、网络和子进程；原始数据默认只读，修改时生成新文件。它用于可信 Agent 分析脚本，不能隔离恶意 native 代码，**不是 OS 安全沙箱**。

## 成图检查与自动重绘

geo_cartography 显式读取成图，结合任务、预期图层、参数和源数据摘要，检查空白、图层可见性/遮挡、图例对应、文字和布局。可确认的呈现问题自动修正并再检查，最多两轮，可配置为 0 或 1 轮。修正前地图、PNG 预览和 report.json 位于成图旁独立的 `.review-*` 目录；路径通过工具结果返回，不覆盖已有旁侧文件。

不会因图片建议自动更换数据、统计方法、字段或影响阈值。视觉检查不能证明数值正确，必须同时核验分析结果。

| 状态 | 含义 |
|---|---|
| passed | 完成视觉检查，没有报告需处理的问题 |
| issues | 仍有具体问题，报告保留原因 |
| unverified | 检查关闭、服务失败、回复无效或无法确认，不表示正确 |

PNG 是首期推荐输出。PDF/SVG 检查同参数 PNG 预览，导出文件本身的排版/字体需另行核验。HTML 没有实际网页截图时仍标为 unverified。Python 自定义地图用 geo_map_review，由 Agent 按问题修改呈现代码、重绘、再检查。

主模型默认兼任视觉模型；map_review 可单独配置 model、base_url、api_key。轮数、超时、图幅及字体等参数在 YAML，敏感值通过 `.env` 引用。推理模型的 token 预算包含思考消耗；此聚合服务的 `(max)` 模型可能产生较长思考，示例给出 32768 token 和 180 秒上限。预算耗尽时报告 unverified 和原因；按实际延迟/费用调整，不能把未完成检查当作通过。

## 后续方向

下一阶段补充空间分布与热点分析，再做栅格/地形分析。认知技能保留领域参考价值，简单叠加任务无需强制进入空间自相关或因果流程。
