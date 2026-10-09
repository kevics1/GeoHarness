---
name: cartography
description: 分析结果制图、图片检查与编辑工程交付。使用数据 preflight、同工程 QGIS PNG/PDF 和持久回执，保留 Matplotlib/SVG/HTML 与自主 Python 制图。
---

# 制图与检查工作流

## 明确地图表达什么

使用真实分析结果和对应图层。analysis_type 只选择符号化规则，不执行分析。核对数量、CRS、字段、单位及范围；不要把目录/通配符当作 file_path，也不要根据概况回复重建几何。

涉及统计或方法说明时，给 geo_cartography/geo_qgis_project 的新建请求传 `analysis_manifests`：最多8个实际 geo_analysis 清单。工具读取对应输出，核对 SHA256/计数、impact 阈值与选集，并要求输出确实参与当前图层。没有原始摘要的旧清单只验证现存输出计数。QGIS 说明栏的统计、方法、计量 CRS 与显示 CRS 来自核对结果；自由 footer_text 是备注，不是统计证据。原始源数据不重新分析，analysis_correctness 仍为 unverified；area 另从持久输出几何复算面积。任意 Python/QGIS 清单不属于该证据接口。

时次/类别面数据的面积使用 `geo_analysis(action="area", source=..., group_field="实际时间字段")`。每组分别返回 union_area_m2（去重覆盖）和 feature_area_sum_m2（要素面积之和），累积/重叠范围不能相加称为总覆盖。将其 result 和清单用于制图，说明栏自动保留这两个口径；不手写未经核对的面积总量，不假定时次按文件顺序排列。

## 原生制图

日期分类由工具统一为保留时区的 ISO 字符串/null；不要删除 time 属性、生成 sort_order 或手写几何来绕过类型错误。需要子集、字段选择或地图注记时，在原生输出图层中传 derivation：feature_indices（0起始原输出行位置）、keep_fields、annotations（新列且每个选中要素一个值）。系统生成并核验副本，记录完整分析数与上图子集数；原字段/几何不能改写，注记不是统计证据。

使用相同的分析来源规则，无论选择 geo_cartography 还是 geo_qgis_project。工具可识别明确列出实际输入文件的相邻原生清单；analysis_manifests 仍可显式提供。not_provided 表示未绑定分析依据，上一地图事实不会继承。用生成底栏记录统计及计量/显示 CRS，footer_text 仅写来源与上下文，不能写与清单冲突的计量 CRS。

带 qgis_layout/layout 的 preflight 测量真实字体/图例/比例尺/底栏，不导出成图、不占版本。底栏自动换行与扩展高度；放不下时缩短备注或扩大页面，不反复增高同一错误参数。duplicate_error_blocked 表示相同确定性失败已返回，必须改变已诊断的参数或输入；暂时文件锁/超时另行处理。保留成功分析输出，不为逃避检查换工具、换 map_id 或重建几何。

`geo_cartography` 的 PNG/PDF 默认使用 QGIS，复用数据解析和分类颜色，
由真实工程的同一打印布局导出图片。需要直接定义本地图层时使用 `geo_qgis_project`；
其 layers 是结构化数组，包含明确样式、字段和 CRS。
inspect 可查看已有工程，export 读取用户保存的布局与样式并另建导出，不覆盖原工程。
preview_path 是实际布局导出的 PNG；用 geo_map_review 检查它，再用 project_dir 交付。
工程生成/导出成功仅表示执行成功，质量由实际阶段回执决定。
无法保留任务要求的复杂布局时说明具体缺项，不能悄悄换成简化图。

geo_cartography 的 export/full 渲染并生成可编辑 QGIS 工程，symbolize/compose 仅返回方案。多图层一次传 layers JSON 字符串，按数组顺序由下到上绘制；每层可带 label、color 和 label_field。geo_analysis 返回的 layers 可直接序列化使用。先一次准备好标题、图例、范围、字号和说明，推荐 PNG 作为实际检查的主图。

```json
{"action":"export","output_format":"png","title":"影响范围分析","review_task":"检查危险区、受影响居民点和行政区是否清楚可见，图例是否对应","layers":"[{\"file_path\":\"行政区.geojson\",\"label\":\"行政区\",\"color\":\"#dbe8f2\"},{\"file_path\":\"危险区.geojson\",\"label\":\"危险区\",\"color\":\"#d7301f\"},{\"file_path\":\"affected.geojson\",\"label\":\"受影响居民点\",\"color\":\"#ff7f00\"}]"}
```

`geo_cartography(action="preflight")` 与 `geo_qgis_project(action="preflight")` 只核对实际数据，不绘图、不生成工程、不消耗成图或视觉预算。新建图在预留预算前自动执行同样检查：符号/标签字段、分类/分级与数值类型、几何、CRS、范围、指定总数和主/详图几何相交数量。缺项应修正参数或数据副本，不能跳过检查后宣称通过。

显式 map_extent 使用 GIS 顺序 `[xmin,ymin,xmax,ymax]` 或同名对象，同时声明 map_crs（直接 QGIS 为 crs）；detail_extent 是同显示坐标系的同名对象。imshow.extent 的顺序为 `[xmin,xmax,ymin,ymax]`，不能直接复用 GIS 四元组。未指定显示 CRS 时取栅格或首图层 CRS。计量 CRS 与显示 CRS 分开，显示投影不证明距离单位正确。

required 图层默认至少在主图或详图中相交1个要素，可用 expected_feature_count/minimum_visible_features 指定总数和阈值；确实允许空影响选集或界外背景时明确 required=false，保留零计数和警告，不改变影响阈值或假造对象。栅格可见性只检查范围相交；这些计数不证明符号未被遮挡。输出只能位于工作区或配置 outputs_dir，不覆盖源数据。

QGIS 使用 qgis_layout 的主图、可选 detail_extent、图例与 footer_text 分区；layout 保留 dpi 和常规呈现参数。`renderer="matplotlib"` 或 cartography.static_renderer=matplotlib 可选原静态路径；HTML/SVG 和 geo_python 保留。QGIS 不可用时说明实际错误，按用户需求明确选替代入口，不静默丢弃布局要求。

新建布局自动按实际图例尺寸调整侧栏/详图，不删类别、不缩字号。遇到布局失败先看 `content_metrics.required_mm/available_mm`、`effective_parameters`、`adaptations` 和已加载字体证据，调整真实生效的参数。图例字号用 `qgis_layout.legend_font_size_pt`，底栏用 `body_font_size_pt`。默认原生包 `project_fidelity=saved_layout`；旧 Python 图片补的 QGIS 工程为 `reference`，必须告知布局不同源，不能把复制预览作为工程一致的证据。

## 自动检查与修正

工具显式把图片和任务/图层/参数摘要送入视觉模型，检查空白、图层可见性、图例对应、文字乱码/遮挡、范围与布局。文件落盘不代表检查通过。

将具体错误/警告合并为一份呈现修正清单，最多 map_review.max_repairs 轮（默认2）。Matplotlib 路径可应用支持的呈现补丁自动重绘；默认 QGIS 路径会检查导出的图片，Agent 按实际问题修改支持的布局/样式参数并重绘，不能声称工具已经自动应用补丁。保留修正前地图与报告；不要因审美偏好、info 提示或无法确认的问题重绘，不自动改动源数据、分析口径或阈值。

工具返回系统 UUID 作为 map_id；后续文件名、格式、Python/原生版本、审图与交付沿用它，不另造别名绕过预算。持久回执默认在工作区 .geoharness/maps/，cartography.jobs_dir 必须为工作区子目录。用户“继续/接着做/continue/resume”关联当前或最近持久任务；每次真实用户输入重置请求预算，内部恢复与自动续跑不重置。

同一用户请求的所有工具共享审查次数及时间预算，默认最多4次、120秒实际服务调用时间，不计中间分析、渲染和写报告耗时；当前请求最多初版加2轮成功成图，失败释放成图预留。预算与持久 UUID 分开，切换历史 map_id 也不补充额度。相同图片/审图配置使用缓存，失败也不盲目重试。实际检查完成后停止无依据的完善；视觉 unverified 或预算耗尽时停止视觉重试，有版本额度时仍可统一修复已测明确布局问题，随后草稿交付。

- passed：该项实际检查通过；visual_checked=passed 不等于最终质量通过。
- issues：存在剩余问题，报告具体内容。
- unverified：检查关闭、模型不可用、格式无法核实或回复无效；不宣称地图正确。

用 `geo_cartography(action="status",map_id=...)` 读取已有回执和当前文件摘要，也可按 map_path 查询已登记图片；status 不创建任务或增加检查结果。分别报告 parameters、data_checked、rendered、layout_checked、visual_checked、packaged、delivered 的实际状态。quality_status=passed 要求同图数据、实际渲染、已测布局和实际视觉检查均为 passed；缺少证据为 draft，文件改变导致旧阶段 stale。analysis_correctness 永远 unverified，打包不能提升质量。

非白像素比例和颜色检出最多证明文件有绘制内容，不能证明图层、对象数量、类别或不存在压盖，也不能替代失败的视觉审查。服务失败时明确说“地图已生成，视觉审查未完成，作为草稿交付”，说明实际原因和问题。QGIS label_field/放置设置与 bounds_check 只覆盖对应字段和已测元素边界，不证明所有标签已显示、位置及业务语义正确。

PDF/SVG 使用同参数 PNG 预览检查，不能替代导出文件自身的排版/字体核验。HTML 的静态预览不能证明网页正确，没有实际网页截图时仍为 unverified。

## Python 自定义制图

geo_python 生成图片后调用 geo_map_review(file_path=..., task=..., evidence=..., map_id=...)，使用与 Python 相同的 map_id。一次合并处理具体错误，遵守共享预算。保留脚本、产物和报告；未解决问题与地图一起说明，不因布局建议修改分析结果。

一次制图脚本生成多图时，系统只将最后写出的 primary_map 候选登记为一个版本，map_candidates 列出全部图片候选。附件不是连续修订；明确选择需审查/交付的主图。重审已登记旧图只更新该版本检查，不增加成图版本。

geo_map_review 可登记实际图片与其布局报告，但不会给自定义脚本补造数据 preflight。缺少同图 data_checked 时，即使视觉 passed 最终也为 draft；单独跑一次预检不能自动变成当前图片的检查证明。

晕渲、放大图和标签先使用 QGIS 的 raster_style、detail_extent、label_field/label_style；这些需求本身不需要改走 Python。需要图片与工程同源时保持原生 QGIS 路径。默认模板确实无法表达要求、已经选择 Python 自定义制图时，使用 `geoharness.cartography.layout` 的 `create_map_figure(layout, title=..., subtitle=..., detail=True)` 预留主图、放大图、图例和说明的独立区域；先设置最终范围、比例，再用 `place_point_labels(ax, points, font_size=..., max_offset_points=...)` 测量文字边界并避让，禁止所有名字使用相同固定偏移。无法放下的标签使用编号与名单对应，不能静默丢弃重点点。`save_map_figure(page, output_path)` 保存成图及对应 SHA256 的 `.layout.json`，先处理 panel_overlaps、label_overlaps、clipped_labels、overflow_text，再进行视觉审查；布局报告仅证明已测的几何项。可参照 [Python 布局示例](references/python-layout.md)。

对已生成的地图，用 geo_cartography(action="project", map_path=实际图片, project_output_dir=新工程目录, layers=实际图层JSON, ...) 生成 QGIS 工程；output_path 兼容已有图片输入，该动作不重绘或审查。工程目录必须是工作区内尚不存在的新目录。传实际来源与样式，分类层明确 column/field_name、categories 和 colors，不能仅把“风险分级”写在 label 中。参数错误时修正参数，不能 action=full 另画简化图或换 map_id。工程预览必须是同一交付图；Python 自定义注记可能需在 QGIS 内参照预览补充，原脚本一并交付。

## 交付

调用 geo_deliver(files=实际地图/分析结果/脚本/报告路径列表, project_dir=工程目录, primary_map=实际PNG预览, review_path=实际报告)。交付到当前工作目录 exports 的新子目录；核对主图、工程预览、已登记快照和复制结果。打包/交付成功不补齐质量证据；保留 delivery.json 与 map-receipt.json。

先读取 status 的 delivery_candidate。默认交付 version_policy=prefer_checked 会在已登记选中图有已知问题/失效证据时选择未变化的布局通过候选及其对应工程，优先全质量通过，否则仍为草稿；也可只传 map_id。selection 返回实际版本和原因，最终使用这些返回路径，不能继续引用较新问题图。无候选先修复或如实报告；用户明确要求保留指定问题图时用 version_policy=selected。换图不沿用旧图的审查报告或工程，不静默重绘，不删旧产物。

最终使用返回的 delivery_dir、qgis_project、receipt_path 和文件路径，明确质量/阶段状态、方法、CRS、单位及剩余问题。QGIS 打开返回的 .qgs/.qgz，通过“项目 → 布局”编辑后保存，用 geo_qgis_project inspect/export 另建输出；修改图片或工程使旧证明失效，应核对新版本后重新交付。分享携带整个工程目录。Python/Matplotlib 工程的 redraw.py 读取 project.json，不读取 QGIS 样式修改；保留原脚本和前版本，不覆盖用户编辑工程。
