---
name: spatial-analysis
description: 地理空间分析入口。聚焦叠加、缓冲、距离、分区计数与影响范围；使用原生 GIS、QGIS Processing 或 Python，核对持久输出并交付地图与结果。
---

# 空间分析工作流

先明确用户问题、输入图层、分析对象、距离/范围口径与交付要求。仅询问不能从数据或配置得到、并会改变分析结果的信息。

工具文本返回准确的产物路径、统计和最多 20 行属性预览；不要猜测输出文件名。完整属性/manifest 用 geo_python 按返回路径读取。SOURCE_CWD、RUN_DIR 是 Path 全局变量，同时也提供同名环境变量；DATA_DIRS 全局变量是路径集合，环境变量按 os.pathsep 分隔。脚本当前目录为 RUN_DIR。

## 检查输入

本地数据用 geo_vector_data，数据库数据用 geo_db_data；确认几何类型、字段、CRS 和范围。load 的概况没有完整几何，不要手工重建 GeoJSON。未知 CRS 需要明确声明；已有 CRS 用变换，不重新贴标签。检查空/无效几何和阈值；计量使用米制投影。原生分析默认按全部输入范围选择适用的 UTM，跨带或极地等范围需要显式配置 analysis.metric_crs。

## 执行空间运算

QGIS 启用且可用时，优先复用其 Processing：先 `geo_qgis(action="doctor")` 确认运行环境，
再 `search` 查实际算法、`help` 获取参数与枚举，最后 `run` 执行。常见算法可搜索
buffer、clip、intersection、zonal、slope、hillshade、nearest；不猜测 ID 或选项索引。
`parameters` 是参数对象，输入用真实本地数据路径，输出由工具放到新的任务目录。
距离按所选算法的 CRS/单位解释；需要米制时先重投影，不能把经纬度上的500当作500米。
`model` 可执行已存在的本地 `.model3` 多步骤模型；缺少算法、外部提供者或参数时报告实际错误。
保留结果、参数、CRS、日志和 manifest，算法完成不等于分析方法得到证明。

首期 geo_analysis 不依赖 MCP 或 PostGIS。source、reference、zones 使用结构化 SpatialSource，每个来源恰好提供 file_path、source_name 或 geojson 中的一项。

| action | 运算与输入 |
|---|---|
| buffer | source 按 distance_m 生成缓冲区 |
| clip | source 按 reference 面图层裁剪 |
| intersection | source 与 reference 做几何交集 |
| distance | source 到 reference 的最短距离，输出 distance_m |
| count | 按 zones 或 reference 统计 source 要素数量 |
| impact | source 资产到 reference 危险区的距离及 affected，可选 zones 分区统计 |

例如：

```json
{"action":"impact","source":{"file_path":"居民点.geojson"},"reference":{"file_path":"危险区.geojson"},"zones":{"file_path":"行政区.geojson"},"distance_m":500,"output_name":"impact"}
```

affected 表示相交或最短距离不超过阈值，不是未经验证的风险等级。分区采用 intersects，包含边界；跨区/重叠区会分别计数，所以分区数之和可能超过全局唯一资产数。保留这项计数口径。

输出包含 GeoJSON、CSV、统计、manifest 和可制图 layers。计量 CRS 与导出 CRS 分别记录。核验实际数量、单位和参数；空影响选集也是有效结果，不应改阈值制造受影响对象。

保留实际 manifest 路径。将统计/方法写入地图时，给 geo_cartography 或 geo_qgis_project 的新建请求传 analysis_manifests（最多8个 geo_analysis 原生清单）。工具读取真实输出，核对摘要/计数、impact 阈值标记与影响选集，并要求至少一份输出是当前实际地图图层。旧清单没有原始摘要时只能核对现存输出计数；任意 Python、Processing 清单与自由说明不属于这个接口支持的统计证据。

这类核对证明持久输出一致性，没有重新计算原始空间运算。不能把 analysis facts 的 verified 当成数学或业务结论已证明；地图回执中的 analysis_correctness 始终 unverified。需要严格正确性结论时，核验原始输入、方法和关键数值并明确验证范围。

## 组合分析与自主 Python

组合任务使用 geo_python。可用已安装的 GeoPandas、Shapely、pandas、matplotlib 和 rasterio，读取 SOURCE_CWD/DATA_DIRS，输出只写 RUN_DIR 或相对文件名。保留脚本、参数、日志与结果，检查 exit_code；失败按实际错误修正。

制图脚本一次生成多张图片时，最后写出的图片作为 primary_map 候选登记一个版本，map_candidates 列出全部图片候选；附件不是连续修订，后续明确选择主图。已登记旧图的重复审查只更新该版本检查。

源数据默认只读；修改数据时生成新文件。不使用网络、shell、子进程或读取凭证。应用级保护用于可信分析脚本，不是 OS 沙箱。

## 结果制图与检查

QGIS 分析结果可直接作为 `geo_qgis_project` 的真实图层创建可编辑工程和布局。
`geo_cartography` 默认 PNG/PDF 由 QGIS 同一工程布局导出，保留 Matplotlib、HTML/SVG
与 geo_python 入口。检查工程实际导出的 PNG（preview_path），用返回的 project_dir 交付；
需要编辑后重新出图时，inspect/export 原工程，导出到新目录，保留用户的原工程。

加载 skill(name="cartography")。将 geo_analysis 返回的 layers 序列化后传给 geo_cartography，突出真正的影响选集，不要把全部资产画成受影响对象。

新建制图会在预算前检查实际字段、数值/类别、几何、CRS、范围与可见数量；独立 action=preflight 不绘图或消耗成图/审查预算。GIS map_extent 是 xmin,ymin,xmax,ymax，与 imshow 顺序不同；显式主/详范围同时声明显示 CRS。required 图层默认至少相交1个要素，空影响选集应明确 required=false，保留零数量与警告。计量 CRS 和显示 CRS 分开；几何相交计数与栅格范围相交不能证明图层实际无遮挡或像素/语义正确。

原生制图会读取实际成图进行视觉检查，并输出可编辑 QGIS 工程。Python PNG 使用 geo_map_review，提供目标、预期图层与真实分析摘要。后续版本、审图和交付沿用工具返回的系统 UUID map_id；“继续/接着做/continue/resume”保留任务关联，每次真实用户输入开启新预算，内部恢复不重置。当前请求预算与任务 UUID 分开，切换历史 map_id 也不会补充额度。只合并修复具体呈现错误，不为审美反复改图，不根据视觉建议更换数据、方法或阈值。

使用 geo_cartography(action="status",map_id=...) 读取当前回执。parameters、data_checked、rendered、layout_checked、visual_checked、packaged、delivered 分别记录实际证据；quality_status=passed 要求同一图的数据、渲染、已测布局和实际视觉均通过。视觉通过或工程打包不能补出缺少的 preflight；自定义 Python 图缺少绑定数据检查仍为 draft。QGIS 标签配置与 bounds_check 也不证明所有标签、位置或业务语义正确。停止无效视觉重试，说明未验证阶段并交付草稿；颜色/非白像素计数不能代替视觉审查。

## 交付

用 geo_deliver 把数据结果、实际主图、QGIS 工程、脚本/manifest 和报告交付到当前工作目录 exports 的新子目录。传 primary_map 与实际 review_path，保留 map-receipt.json；工程与图片摘要/快照变化时旧证明失效，应核对新版本后重新交付。Python 图用 geo_cartography(action="project",map_path=...,project_output_dir=...) 和实际图层打包，不重新绘图或提升质量，不在脚本中绕过 RUN_DIR 复制文件。

最终给出数量/距离/范围、方法、计量/显示 CRS、单位、计数口径和工具返回的路径。区分 quality_status 的 passed/draft 与阶段 issues/unverified/stale；只声称实际完成的核对，不把业务正确性或准确率评测写成已完成。

当前优先多图层叠加与影响范围验收，热点/空间分布、栅格/地形业务和准确率评测延后。现有 skill(name="geo-perception")、skill(name="geo-comprehension")、skill(name="geo-reasoning") 可提供领域参考；只用实际可用工具，不把简单叠加强制升级到 Moran/LISA 或因果分析。
