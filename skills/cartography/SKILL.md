---
name: cartography
description: 分析结果制图与图片检查。生成单图层或多图层 PNG/PDF/SVG/HTML，读取实际成图检查图层、图例、文字与布局，有限自动修正后交付。
---

# 制图与检查工作流

## 明确地图表达什么

使用真实分析结果和对应图层。analysis_type 只选择符号化规则，不执行分析。核对数量、CRS、字段、单位及范围；不要把目录/通配符当作 file_path，也不要根据概况回复重建几何。

## 原生制图

QGIS 启用且可用时，优先使用 `geo_qgis_project` 创建真实工程与打印布局，
由该布局直接导出 PNG/PDF。输入为真实本地图层及明确样式/字段/CRS；
inspect 可查看已有工程，export 读取用户保存的布局与样式并另建导出，不覆盖原工程。
preview_path 是实际布局导出的 PNG；用 geo_map_review 检查它，再用 project_dir 交付。
工程生成/导出成功仅表示执行成功，视觉状态仍需独立报告，不宣称 passed。
无法保留任务要求的复杂布局时说明具体缺项，不能悄悄换成简化图。

geo_cartography 的 export/full 渲染并生成可编辑 QGIS 工程，symbolize/compose 仅返回方案。首期优先 PNG。绘图前一次准备好标题、图例、范围和字号；普通多图层地图优先使用原生工具。多图层一次传 layers JSON 字符串，按数组顺序由下到上绘制；每层可带 label 和 color。geo_analysis 返回的 layers 可直接序列化使用。

```json
{"action":"export","output_format":"png","title":"影响范围分析","review_task":"检查危险区、受影响居民点和行政区是否清楚可见，图例是否对应","layers":"[{\"file_path\":\"行政区.geojson\",\"label\":\"行政区\",\"color\":\"#dbe8f2\"},{\"file_path\":\"危险区.geojson\",\"label\":\"危险区\",\"color\":\"#d7301f\"},{\"file_path\":\"affected.geojson\",\"label\":\"受影响居民点\",\"color\":\"#ff7f00\"}]"}
```

源图层必须有明确 CRS，静态合成统一到首图层或栅格底图 CRS；失败就报错，不继续绘制错位图层。影响选集为空时保留零影响结论，用危险区/背景图表达，不改变阈值或假造对象。输出只能位于工作区或配置 outputs_dir，不覆盖源数据。

layout 可设置 width、height、dpi、legend_outside、title_font_size、legend_font_size、point_size、line_width、polygon_alpha。

## 自动检查与修正

工具显式把图片和任务/图层/参数摘要送入视觉模型，检查空白、图层可见性、图例对应、文字乱码/遮挡、范围与布局。文件落盘不代表检查通过。

将具体错误/警告合并为一份修正清单，能用呈现参数修复时统一重绘，最多 map_review.max_repairs 轮（默认2）。每轮使用同一份加载数据，保留修正前地图与检查报告。不要因审美偏好、info 提示或无法确认的问题重绘；不自动改动源数据、分析口径或阈值。

同一用户请求的所有工具共享审查次数及时间预算，默认最多4次、120秒实际服务调用时间，不计中间分析、渲染和写报告耗时；每张地图最多初版加2轮成功成图，执行失败不占成功版本。同图的所有文件名、格式、Python/原生工具使用同一 map_id。未改变图片的重复检查直接使用缓存，失败检查也不盲目重试。passed 后停止完善；unverified 或审查预算耗尽后停止视觉重试，仍有版本预算时统一修复已测出的明确布局问题，随后按未完成视觉审查的草稿交付。用户的新修改请求会开启新的预算。

- passed：视觉检查通过，不等于统计或真实位置得到证明。
- issues：存在剩余问题，报告具体内容。
- unverified：检查关闭、模型不可用、格式无法核实或回复无效；不宣称地图正确。

分别报告数据核验、布局几何检查和模型视觉审查的结果。非白像素比例和颜色检出最多证明文件有绘制内容；图例本身就有这些颜色，不能证明图层正确、对象数量正确或不存在压盖。严禁用“像素级校验确认所有图层与配色正确”等语句替代失败的视觉审查。服务失败时明确说“地图已生成，视觉审查未完成，作为草稿交付”，说明具体原因及已知问题，不把自动审查责任推给用户。

PDF/SVG 使用同参数 PNG 预览检查，不能替代导出文件自身的排版/字体核验。HTML 的静态预览不能证明网页正确，没有实际网页截图时仍为 unverified。

## Python 自定义制图

geo_python 生成图片后调用 geo_map_review(file_path=..., task=..., evidence=..., map_id=...)，使用与 Python 相同的 map_id。一次合并处理具体错误，遵守共享预算。保留脚本、产物和报告；未解决问题与地图一起说明，不因布局建议修改分析结果。

有晕渲、放大图或密集重点标签时采用自定义布局，不能为获得工程而退回缺失要素的通用地图。使用 `geoharness.cartography.layout` 的 `create_map_figure(layout, title=..., subtitle=..., detail=True)` 预留主图、放大图、图例和说明的独立区域；先设置最终范围、比例，再用 `place_point_labels(ax, points, font_size=..., max_offset_points=...)` 测量文字边界并避让，禁止所有名字使用相同固定偏移。无法放下的标签使用编号与名单对应，不能静默丢弃重点点。`save_map_figure(page, output_path)` 保存成图及对应 SHA256 的 `.layout.json`，先处理 panel_overlaps、label_overlaps、clipped_labels、overflow_text，再进行视觉审查；布局报告仅证明已测的几何项。可参照 [Python 布局示例](references/python-layout.md)。

对已生成的地图，用 geo_cartography(action="project", output_path=已存在的实际地图路径, layers=实际图层JSON, ...) 生成 QGIS 工程；该操作不重新制图或审查。必须传实际来源和对应样式，分类层明确 column/field_name、categories 和 colors，不能仅把“风险分级”写在 label 中。参数错误时修正缺失参数，不能 action=full 另画简化图或换 map_id 绕过预算。工程预览必须是同一张交付地图。Python 自定义文字、标注和复杂注记可能需在 QGIS 内参照预览补充，原脚本一并交付。

## 交付

调用 geo_deliver(files=地图/分析结果/脚本/报告实际路径列表, project_dir=工程目录) 交付到当前工作目录的 exports 子目录。最终回复使用返回的 delivery_dir、qgis_project 和文件路径，明确检查状态、修正次数、数据、CRS、单位与图层含义。用户在 QGIS 中打开 map.qgs，通过“项目 → 布局”修改 GeoHarness Map 后导出。移动工程时携带整个 project 目录；Python 重绘使用 project.json，不读取 QGIS 的样式修改。预览、报告和修改前地图保留在独立目录，不覆盖用户编辑的工程。
