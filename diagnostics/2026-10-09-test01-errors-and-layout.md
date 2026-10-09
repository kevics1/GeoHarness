# test01 工具错误与 PNG/QGS 布局差异诊断

## 范围与结论

本轮仅诊断，不修改产品代码、技能、模型配置或用户已有产物。核对的是修复版安装之后的最新会话：

- 会话：`C:/Users/Administrator/.geoharness/data/sessions/test01-aacc6482245b/session-bfde4744da4a.json`
- 工作区：`F:/Desktop/Project/GeoHarness/example/test01`
- 交付：`exports/西昌330森林火灾应急专题图交付-ywlu45qp`

错误主要集中在参数生成、原生图例布局和 Python 回退流程。原生空间分析实际执行的四次均成功。PNG 与 QGS 不一致已用本机 QGIS 直接读回并导出复现：PNG 来自自定义 Python 布局，QGS 来自另一个基础版式生成器。preview 相同只证明原 PNG 被复制，不证明工程能重绘同图。

## 错误分布

40 个 assistant turn、55 次工具调用、13 条 `is_error`，其中八条实际执行失败、五条执行前参数拒绝。另有一次版本预算阻止和两次审图正常返回 `issues`，不计为执行异常。

| 工具 | 调用 | is_error | 实际执行失败 | 执行前参数拒绝 |
|---|---:|---:|---:|---:|
| geo_python | 18 | 5 | 4 | 1 |
| geo_cartography | 6 | 4 | 4 | 0 |
| geo_qgis_project | 3 | 3 | 0 | 3 |
| geo_analysis | 5 | 1 | 0 | 1 |
| 其它工具 | 23 | 0 | 0 | 0 |
| 合计 | 55 | 13 | 8 | 5 |

以下索引为会话中零基 assistant message index：

- 9：`DATA_DIRS` 是 Python `list[Path]`，脚本把它当环境变量字符串使用 `.split()`。
- 21、27、31、33：provider 工具参数 JSON 不完整，全部 `finish_reason=length`。最短只有116字符，不能仅归因于脚本过长。工具没有执行。
- 25：Shapely `Point` 没有 `.to_crs()`，脚本混淆几何与带坐标系的 GeoSeries/GeoDataFrame。
- 35：精简 QGIS 调用时移除了必需 `layers`，Pydantic 拒绝，QGIS 没有执行。
- 39、41、51：QGIS 实际失败原因均为 `legend content exceeds its reserved rectangle`。
- 43：Python 读取工具刚返回的 `.geoharness/qgis/request.json`，但其 read roots 只包含 analysis/cartography/execution，遗漏 QGIS 产物窄目录。
- 47：模型尝试指定系统字体文件，`C:/Windows/Fonts/simhei.ttf` 不在允许读取范围。
- 49：模型尝试写工作区 `fonts`，Python 运行只能写自己的 `RUN_DIR`。

累计 usage 为2,247,684 input tokens和187,501 output tokens；这是会话记录中的累计量，不代表独立计费或未缓存 token 数量。

## 原生图例：实际尺寸不够，字体警告被误解

三个图例失败运行的最终 Python 异常均来自 `project_worker._validate_layout_bounds()`。同日志中的 `FT_New_Face`、`Cannot find font directory /fonts` 是警告；原生 `_font()` 已通过中文字体检查，不能把这些警告直接写成终止原因。

参数入口也存在歧义：模型将 `layout.legend_font_size` 从8改到7.5再改到7，但显式 `qgis_layout.body_font_size_pt` 一直为9。`qgis_bridge.native_layout()` 对显式 qgis_layout 直接返回，worker 图例组件使用 body_format，因此这几次缩字号没有影响真正的 QGIS 图例。

用首个失败请求重建冻结输入，运行独立 QGIS 进程的 `project_preflight`；临时观察钩子只记录现有测量结果，然后调用原校验器。没有修改安装模块或导出业务地图。

| 探针 | 图例可用宽×高 mm | 图例实际所需宽×高 mm | 结果 |
|---|---|---|---|
| 原参数，A4、侧栏58mm、9pt | 58 × 82.6 | 63.50 × 87.10 | 失败 |
| 仅关闭图例，作为因果对照 | 无图例 | 无图例 | 预检通过 |
| 仅将真实 body 字号改为7pt | 58 × 82.6 | 52.28 × 85.41 | 高度不足 |
| 仅改为A3，保留58mm侧栏 | 58 × 134.8 | 63.50 × 87.10 | 宽度不足 |
| A3基础上侧栏改为78mm、保留9pt | 78 × 134.8 | 63.50 × 87.10 | 预检通过 |

所有探针均实际加载 Microsoft YaHei，中文字体有效，本次没有字体警告。关闭图例仅为定位问题，不建议以删除图例作为交付修正。

探针记录：`C:/Users/Administrator/AppData/Local/Temp/geoharness-test01-legend-check-opybkn7g/summary.json`。

建议返回结构化的 required/available width、height、有效字号、超出项和生效参数路径；版式按这些测量适配换行、栏宽、栏高、列数与纸幅。已有固定纸幅要求应优先保持，不应无限缩小字号。不要让模型把每次局部变化解释成新根因，也不要把 warning 放在真正 error_summary 之前。

## 工具参数截断：恢复分支未覆盖残缺调用

`api_client._stream_request()` 先为错误 JSON 添加 `_geoharness_argument_error`，随后调用 `is_effectively_empty()`；有该工具调用时消息非空，直接返回。现有低推理 fallback 针对空回复，因此没有处理本次四个 length + invalid_json 调用。

建议加入一次有限的定向恢复，仅重新生成无效调用，保留有效的并行调用与已完成工具结果；恢复仍失败时明确终止该调用。图层和样式可以保存在地图任务中，后续调用只修改局部参数，减少反复发送整份图层 JSON 或脚本。

Python 的 DATA_DIRS 类型已经写在工具描述里，本次仍被模型误用；补齐窄目录诊断权限、提供常用重投影/诊断操作，比只增加提示文字更可靠。不应因此扩大整个 .geoharness 或系统目录的权限。

## PNG 与 QGS 不一致：两条渲染链路

真实主图：`xichang_fire_330_v3.png`，2100×1500；含主图、详图、DEM彩色晕渲、可燃物栅格、编号点注记、连接线、两比例尺及自定义图例/底栏。

交付工程：`project/map.qgs`；QGIS 读回显示13个有效 OGR 图层、一个 Map frame、EPSG:32648有效。没有栅格图层，没有详图框，也没有底栏文本。版式来自默认10×8英寸、150dpi的 MapLayout，不是传入的A4 qgis_layout。实际重导出PNG为1500×1200，呈现基础主图和侧栏图例。

形成差异的调用链：

1. 原生图例失败后，模型用 `geo_python` 写 Matplotlib 自定义地图。
2. 最后调用 `geo_cartography(action="project")` 为现成PNG附加工程。
3. `_project_only()` → `_attach_project()` → `export_map_project()`；这条旧兼容路径使用 MapLayout 和基础QGS生成器，不使用请求中的完整 qgis_layout，也无法从PNG恢复任意Python布局。
4. `project.py` 将原PNG复制为 preview，并生成一个单独的基础工程。此次工程调用也没有传入两个栅格源。
5. `jobs._package()` 检查主图、preview及记录SHA一致、工程存在、包内文件完整；没有证明工程实际导出与主图相同，因此 packaged 可以 passed，而布局不同。

`redraw.py` 读取 project.json 的通用 native 参数，也不是本次自定义绘图脚本的等价入口；保存的 MapLayout、图层样式无法表达全部自定义元素。

直接使用本机QGIS对原交付工程执行 project_inspect/project_export均成功，差异明显，原工程、主图、preview摘要保持不变。只读检查排除了拿错版本/工程打不开是此次差异主因。

重导出证据：`C:/Users/Administrator/AppData/Local/Temp/geoharness-test01-layout-check-ee7hlh7y/project_export.json`。

建议让用户要求可编辑QGIS工程的地图始终使用同一份保存并重开的 QgsLayout 导出PNG/PDF/preview，详图、栅格、标注、图例和底栏全部进入工程。Python保留分析与数据准备；自定义Python图也可继续支持，但补出的基础QGS必须声明为参考工程，不能声称与主图等价。更完整的跨渲染引擎转换需统一布局规格后另做支持。

验收应在交付目录重开工程、按记录参数导出，检查布局对象及解码后的像素；preview必须来自这次工程导出。文件完整性与布局一致性需要不同的证据。

## 版本选择与说明口径

- v2硬布局检查通过、9/9注记安置；v3新增长编号名单后出现overflow和clipped，v4因预算被阻止。最后仍默认交付最新v3。建议默认选择最近通过硬检查的候选，退化版本保留为过程文件；布局预检应尽量在成功成图计数之前完成。
- 自定义文本称预测蔓延“合计48.1km²”，这是三个高度重叠时次面积33.1493、11.4720、3.4893km²之和；去重覆盖约33.1496km²。不能把前者当作累计新增或去重覆盖。此处应分别显示分时次面积和去重面积，业务说明由结构化事实生成。原始数据存在很小边界碎片，不应称严格嵌套。

## 建议实施顺序与验收

1. 同时完成原生图例自适配、有效参数反馈和工程/图片使用同一保存布局；复验当前真实图层无需Python回退，交付工程重开导出与主图一致。
2. 为截断工具调用增加有限恢复；覆盖无效调用与有效并行调用共存，确保成功工具不重跑。
3. 补齐Python只读诊断入口和常用GIS操作契约；覆盖 DATA_DIRS、重投影、QGIS日志读取及字体资源路径。
4. 交付选择通过硬检查的候选，绑定业务说明口径；覆盖本次v3退化而v2可用、重叠时次面积不可直接称总覆盖的情况。

本报告给出已复现原因与建议，尚未实施上述新改动。测试通过不替代该分支的实际工程读回与导出验收。
