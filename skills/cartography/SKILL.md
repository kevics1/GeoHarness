---
name: cartography
description: 分析结果制图与图片检查。生成单图层或多图层 PNG/PDF/SVG/HTML，读取实际成图检查图层、图例、文字与布局，有限自动修正后交付。
---

# 制图与检查工作流

## 明确地图表达什么

使用真实分析结果和对应图层。analysis_type 只选择符号化规则，不执行分析。核对数量、CRS、字段、单位及范围；不要把目录/通配符当作 file_path，也不要根据概况回复重建几何。

## 原生制图

geo_cartography 的 export/full 渲染并生成可编辑 QGIS 工程，symbolize/compose 仅返回方案。首期优先 PNG。绘图前一次准备好标题、图例、范围和字号；普通多图层地图优先使用原生工具。多图层一次传 layers JSON 字符串，按数组顺序由下到上绘制；每层可带 label 和 color。geo_analysis 返回的 layers 可直接序列化使用。

```json
{"action":"export","output_format":"png","title":"影响范围分析","review_task":"检查危险区、受影响居民点和行政区是否清楚可见，图例是否对应","layers":"[{\"file_path\":\"行政区.geojson\",\"label\":\"行政区\",\"color\":\"#dbe8f2\"},{\"file_path\":\"危险区.geojson\",\"label\":\"危险区\",\"color\":\"#d7301f\"},{\"file_path\":\"affected.geojson\",\"label\":\"受影响居民点\",\"color\":\"#ff7f00\"}]"}
```

源图层必须有明确 CRS，静态合成统一到首图层或栅格底图 CRS；失败就报错，不继续绘制错位图层。影响选集为空时保留零影响结论，用危险区/背景图表达，不改变阈值或假造对象。输出只能位于工作区或配置 outputs_dir，不覆盖源数据。

layout 可设置 width、height、dpi、legend_outside、title_font_size、legend_font_size、point_size、line_width、polygon_alpha。

## 自动检查与修正

工具显式把图片和任务/图层/参数摘要送入视觉模型，检查空白、图层可见性、图例对应、文字乱码/遮挡、范围与布局。文件落盘不代表检查通过。

将具体错误/警告合并为一份修正清单，能用呈现参数修复时统一重绘，最多 map_review.max_repairs 轮（默认2）。每轮使用同一份加载数据，保留修正前地图与检查报告。不要因审美偏好、info 提示或无法确认的问题重绘；不自动改动源数据、分析口径或阈值。

同一用户请求的所有工具共享审查次数及时间预算，默认最多4次、120秒；每张地图最多初版加2轮修正。同图的所有文件名、格式、Python/原生工具使用同一 map_id。未改变图片的重复检查直接使用缓存，失败检查也不盲目重试。passed、unverified 或预算耗尽后立即交付，不再生成 v4、v8 等自动完善版本。用户的新修改请求会开启新的预算。

- passed：视觉检查通过，不等于统计或真实位置得到证明。
- issues：存在剩余问题，报告具体内容。
- unverified：检查关闭、模型不可用、格式无法核实或回复无效；不宣称地图正确。

PDF/SVG 使用同参数 PNG 预览检查，不能替代导出文件自身的排版/字体核验。HTML 的静态预览不能证明网页正确，没有实际网页截图时仍为 unverified。

## Python 自定义制图

geo_python 生成图片后调用 geo_map_review(file_path=..., task=..., evidence=..., map_id=...)，使用与 Python 相同的 map_id。一次合并处理具体错误，遵守共享预算。保留脚本、产物和报告；未解决问题与地图一起说明，不因布局建议修改分析结果。

对已生成的地图，用 geo_cartography(action="project", output_path=实际地图路径, layers=实际图层JSON, ...) 生成 QGIS 工程；该操作不重新制图或审查。工程还原真实数据和基础样式/布局，Python 自定义文字、标注和复杂注记可能需用户在 QGIS 内参照预览补充，原脚本一并交付。

## 交付

调用 geo_deliver(files=地图/分析结果/脚本/报告实际路径列表, project_dir=工程目录) 交付到当前工作目录的 exports 子目录。最终回复使用返回的 delivery_dir、qgis_project 和文件路径，明确检查状态、修正次数、数据、CRS、单位与图层含义。用户在 QGIS 中打开 map.qgs，通过“项目 → 布局”修改 GeoHarness Map 后导出。移动工程时携带整个 project 目录；Python 重绘使用 project.json，不读取 QGIS 的样式修改。预览、报告和修改前地图保留在独立目录，不覆盖用户编辑的工程。
