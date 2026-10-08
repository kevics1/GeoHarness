---
name: spatial-analysis
description: 地理空间分析入口。首期聚焦叠加、缓冲、距离、分区计数与影响范围；原生 GIS 与 Python 结合，保留结果并检查成图。
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

## 组合分析与自主 Python

组合任务使用 geo_python。可用已安装的 GeoPandas、Shapely、pandas、matplotlib 和 rasterio，读取 SOURCE_CWD/DATA_DIRS，输出只写 RUN_DIR 或相对文件名。保留脚本、参数、日志与结果，检查 exit_code；失败按实际错误修正。

源数据默认只读；修改数据时生成新文件。不使用网络、shell、子进程或读取凭证。应用级保护用于可信分析脚本，不是 OS 沙箱。

## 结果制图与检查

QGIS 分析结果可直接作为 `geo_qgis_project` 的真实图层创建可编辑工程和布局。
检查该工程实际导出的 PNG（preview_path），用返回的 project_dir 交付；
需要编辑后重新出图时，inspect/export 原工程，导出到新目录，保留用户的原工程。

加载 skill(name="cartography")。将 geo_analysis 返回的 layers 序列化后传给 geo_cartography，突出真正的影响选集，不要把全部资产画成受影响对象。

原生制图会读取成图进行视觉检查，并输出可编辑 QGIS 工程。Python 生成的 PNG 使用 geo_map_review，提供用户目标、预期图层及数据/分析摘要；geo_python、geo_map_review 和 geo_cartography 的各个版本使用同一 map_id。只合并修复具体错误，不为审美建议反复改图。审查通过、无法验证或预算耗尽时交付最新版本并说明限制。视觉检查不能证明数值正确，仍需程序核验。不根据视觉建议更换数据、统计方法或距离阈值。

## 交付

用 geo_deliver 把数据结果、地图、QGIS 工程、脚本/manifest 和检查报告交付到当前工作目录的 exports 子目录。Python 地图先用 geo_cartography(action="project") 和实际图层生成工程，不重新绘图；不要在脚本中绕过 RUN_DIR 复制文件。最终给出数量/距离/范围、方法与单位、计数口径及工具返回的交付路径。明确 passed、issues 或 unverified；剩余问题如实说明。

热点与空间分布、栅格/地形是后续重点。现有 skill(name="geo-perception")、skill(name="geo-comprehension")、skill(name="geo-reasoning") 可提供领域参考；只用实际可用的工具，不把简单叠加任务强制升级到 Moran/LISA 或因果分析。
