# test02 前置防错与 QGIS 质量修正

本次针对 test02 的真实运行记录实施修复，保留原始数据、旧地图、旧工程与检查回执。修复接口和工作流程，不把旧图自动视为已修好；主模型与服务商设置保持原样。

## 根因与处理

| 真实问题 | 证据与修复 |
|---|---|
| 栅格裁剪 CRS 不一致 | 模型事前已 inspect 两张栅格，知道 EPSG:32647，仍把 EPSG:32648 的裸几何传给 rasterio.mask。数据发现保留实际头信息，后续请求持续注入；Python 新增 mask_raster，要求带已知 CRS 的 GeoSeries/GeoDataFrame，按每张栅格实际 CRS 转换并检查相交。 |
| 读取不存在的旧产物 | prep_layers 把上一轮文件当作当前新 RUN_DIR 中的文件，另一次 buffer 拼错任务日期。现在提供来自真实成功清单的 existing_outputs/PREVIOUS_OUTPUTS；不以模型猜测日期生成定位信息。 |
| 分析清单与地图图层不对应 | 预检必须核对每个 manifest 的实际输出或 layer.derivation。拒绝时给出具体清单、输出路径、绑定问题及恢复方式，保留来源核验要求。 |
| QGIS 诊断和测量图片被拒 | 精确只读权限扩展到合法成功任务返回的实际预览及 project.json；目录枚举、凭证与额外写入继续拒绝。 |
| 分级样式区间倒序 | 对完整区间排序，颜色、标签和大小随区间一起移动；真正重叠的区间仍拒绝。 |
| 审图建议不适用于 QGIS | 从实际图片和工程摘要确认渲染后端。QGIS 使用毫米/pt、真实图层索引与几何类型，过滤无效图层及标签参数；不会将 Matplotlib 英寸或平方 pt 建议当作原生 QGIS 修正。 |
| 小幅修正后仍压盖，三版即停止 | 默认五次成功成图、五次审图，失败退还规则保留。预检提示点密集风险；审图携带前两版问题摘要与真实剩余额度，同类问题再次出现时建议当前请求内使用详图或分图。 |

数据元信息绑定实际测量时的文件签名，读取前后及入库前检查文件状态；不能将修改后的新签名配到旧 CRS。模型上下文限制总长度，按完整记录裁减，避免把路径截成不存在的文件。旧工程没有几何类型证据时，仅保留适用的字体/优先级等建议，不猜标签模式。

QGIS 的 around_point 使用 distance_mm，忽略 X/Y 偏移；over_point 使用 X/Y 偏移，忽略点到标签距离。审图返回时按明确建议或已验证的有效模式过滤无效字段，不静默改变标签模式。原生工程记录 PAL 放置模式、毫米单位、优先级和障碍设置，保存后读回验证。

## 质量状态与完成策略

visual issues 仍是真实问题，data/layout/package passed 不能代替图片视觉检查。系统应在当前请求仍有额度时合并修正；反复标签压盖应调整呈现策略，不能仅告知用户另开一轮。服务不可用或额度确实耗尽时保留草稿和具体剩余问题，不用像素占比冒充审图通过。

analysis_correctness=unverified 表示没有独立复算原始空间分析，不表示已有分析失败。持久输出摘要、计数、影响阈值与复算面积的核验分别记录在 analysis facts 的 verification/verification_scope；这不能被提升为原始数据与业务方法已验证。

## 已完成的实际案例验证

使用原 test02 数据的两张 EPSG:32647、30m 栅格和 EPSG:32648 几何，均裁出 422×349 的非空窗口；已核验 QGIS preview.png 和 project.json 可以通过 Python 精确读取。发现阶段记录十份元信息（八份向量、两份栅格），原源文件及既有 PNG 摘要未改变。证据：`C:/Users/Administrator/AppData/Local/Temp/geoharness-test02-prevention-qwbslmgn/verification.json`。

一次真实视觉 API 调用对原第三版返回 issues、QGIS 毫米/pt 建议及 detail_map，未重新渲染原图。证据：`C:/Users/Administrator/AppData/Local/Temp/geoharness-test02-review-om64esqo/verification.json`。此调用早于最后的几何/有效模式过滤，不能据此声称其所有旧建议都已能有效执行；最后过滤由回归覆盖。

最终代码再次实际调用审图时，聚合网关返回 HTTP 502/InternalServerError，诊断明确为 provider 阶段，状态 unverified。该次检查前后旧图片和旧 receipt 的 SHA256 均保持；证据：`C:/Users/Administrator/AppData/Local/Temp/geoharness-test02-review-wy3ob39o/verification.json`。没有把此失败当成工具输出为空、本地校验失败或视觉 passed，也未对相同请求机械重试。

真实 QGIS 保存/读回回归确认 OverPoint、3/-2mm 偏移、priority=10 和无标签障碍 factor=3 保持。另用 labelingResults 核验环绕模式与 OverPoint 的距离/偏移语义。原业务整图没有重新绘制，最终业务视觉质量仍需对下一次实际成图审查。

## 最终验证与安装

最终完整回归 **1051 passed**，耗时 542.37 秒；197 条警告为 pyproj/Typer 弃用和缺 CRS 的测试夹具提示。ruff 与 diff 检查通过。首轮唯一失败是旧测试用 versions 子串误拒 remaining_render_versions；现解析实际证据，核对首版无历史记录、无完整版本列表且剩余额度正确。相关 71 项通过后重新执行最终全量，不沿用首轮失败结果。

独立审查指出的图层几何过滤、共享预算覆盖真实地图历史、旧元信息绑定新文件签名三项边界均已修正并回归，长路径上下文限额也已修补。最后的独立闭环复核因账户 usage limit 未能完成；主 Agent 做了代码检查及最终完整回归，未宣称二次独立复核通过。

全局 wheel 已安装到 `D:/Python312/Lib/site-packages/geoharness`，73 个源码模块和 6 份项目技能与工作区逐字节一致。安装证据：`C:/Users/Administrator/AppData/Local/Temp/geoharness-test02-final-wheel-78c4273d2cd147278ce1dfa05a55a59d/install-verification.json`。

本机 `C:/Users/Administrator/.geoharness/config.yaml` 只修改两个预算字段：`map_review.max_repairs=4`、`map_review.max_reviews_per_request=5`，实际成功成图额度为初版加四次修正。修改前已备份；程序比较完整解析配置，确认其余模型、服务商及凭证引用保持一致。运行时主模型仍为 `cbcn/deepseek-v4.1-flash(max)`。

去除 PYTHONPATH 后，从 `F:/Desktop/Project/GeoHarness/example/test02` 运行全局 version/dry-run 均成功，注册 15 个原生工具，未创建 `.openharness`。QGIS doctor 成功返回 3.44.7、440 个算法、空 stderr；该检查不证明外部 MCP 或模型服务当前可用。安装版再用真实 test02 数据完成裁剪及 QGIS 图片/说明读取，源文件和旧图摘要保持；证据：`C:/Users/Administrator/AppData/Local/Temp/geoharness-test02-prevention-s2h5vq2a/verification.json`。

最后对聚合网关的模型列表接口检查也返回 HTTP 502，因此本轮不能验收真实主模型端到端回复或业务整图重绘。此前视觉 API 的成功记录不能证明网关现在健康。原业务第三版没有被重绘，也没有被改标为通过。
