# 新错误只读诊断与天地图接入

## 错误原因（不修改原脚本）

核对本机会话23e716a8b6c4、实际analysis.py、.execution.json及QGIS工程/审查报告：

| 现象 | 直接原因与输入 | 信息是否缺失 |
|---|---|---|
| 栅格stats旁显示Unknown data ref | stats实际上成功；并行Python compute_extents_targets失败，传入`.geoharness/analysis/spread_area_by_time-d8ce580f978e/result.geojson`相对路径 | 稳定data引用及完整路径已在注册信息中；模型没有按契约复用 |
| compute_frames_targets再次Unknown | read_vector传入`spread_area_by_time-d8ce580f978e/result.geojson`，仍是相对路径 | 三个输出都叫result.geojson会造成名字歧义，但这两次是Unknown，不是Ambiguous；模型后续解释并非直接根因 |
| max模型空回复 | finish_reason=length，max_tokens=output_tokens=16384；55547推理字符、0可见回答 | 单次输出预算被推理消耗；不是工具数据为空。现有none回退只重试模型请求 |
| label_style.font_size_pt拒绝 | layers[5]/[6]标签字号字段应为size_pt | 字段schema已可用，模型混用布局字号命名 |
| _detail_crop.png拒绝写入 | 写入`.geoharness/runs/`父目录，不是当前crop_frames_inspect独有RUN_DIR | RUN_DIR与只写当次目录规则已提供；使用RUN_DIR / 文件名 |
| 审图后KeyError('extent') | 实际project_create-20261014T090249Z-719dfa40e360/project.json及legend_ramp_partial可复现：_covering_pair对legend调用_issue_extent直接索引frame['extent'] | 图例没有地理extent，本来就不应要求模型提供；系统策略分派和异常兜底有缺陷 |

最后一项复现了相同异常并定位代码分支，但没有找到用户原始终止时的完整堆栈，不能断言其原始调用链完全相同。
建议后续修复：非地理frame不进入空间范围修复，缺extent/ROI明确RepairUnavailable，修复失败保留审查结果。
本次只诊断以上旧问题，不修改读取保护、写入保护、主模型或旧图。

## 新底图实现与验证

新增geo_basemap list/fetch：配置provider ID、实际范围/CRS、可选zoom；网络只访问配置URL，凭证仅从指定环境变量取。
已有rasterio/pyproj/Pillow负责投影、GeoTIFF和像素，未增加下载库。评估contextily后未引入：其主要瓦片路径按Web Mercator处理，此处需要c4490与w3857两套网格。
实现每次瓦片数/像素上限、单请求超时、XML/非图像拒绝、取消检查、工作区缓存、摘要与几何校验、无凭证回执，QGIS读取本地RGB TIFF。
QGIS只新增工作区内底图缓存根，仍拒绝.geoharness/.env和其他凭证文件。新数据档案可供后续调用定位。

服务GetCapabilities实测：img_c/vec_c为EPSG4490、z1矩阵2×1、层级1—18；ter_w为900913（3857别名）、z1矩阵2×2、层级1—14。
三个真实小范围瓦片请求均HTTP403，卫星响应301012“权限类型错误”；不将能力描述成功当作瓦片成功。
合成瓦片分别验证网格、像素/范围、缓存与限额；本机原生QGIS对本地合成TIFF的preflight及plan_ref create成功，工程与PNG产生。
不运行全量测试、真实模型或业务地图，不声称真实底图或最终业务视觉质量通过。
