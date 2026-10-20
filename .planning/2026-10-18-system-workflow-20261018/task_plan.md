# 系统执行路径修复

用户批准五阶段系统方案。保留模型/high及审图配置、原数据/历史成果、.env.example既有修改、上游与指令文件。20分钟不是预算，不引入固定次数截停。

1. 统一持久化任务引用/执行与登记状态；页面清单贯穿审查、台账和交付，先修名单缺页 — complete（95项相关通过，独立审查无有效报告）。
2. 默认输入契约和政策确认状态；标准Python自动绑定真实引用，扩展路径显式依赖 — complete（128项相关及3项政策入口检查通过）。
3. 实际计算来源/类别事实与说明同源，分离分析/显示/存储CRS — complete（实际读写CRS证据、报告传播、评分分级政策及像素身份已接入）。
4. 呈现检查、修复进度和策略切换；原生图片工具只读 — complete（读图工具无修复参数，重复问题保留定位进度，像素缓存按稳定身份复用）。
5. 像素/文件身份、历史归档与微压缩整合；整链回放、文档、安装验收 — complete（历史投影已有归档回读，像素身份兼容，wheel/source回归通过）。

每阶段实现→真实调用入口回归→ruff/pytest→提交→CLI检查。复用现有Pydantic、阶段依赖与SQLite清单，不新建重复数据库。真实业务验收与离线验证分开。

2026-10-19 新测试只读诊断：complete。21次制图尝试/17成功、29页审查、至少27分52秒；定位同像素重审与结论漂移、页面目标/选集错误、重复问题追踪未建立、全册重建与事实链绕过。上方complete仅代表上轮已实施与离线检查，不代表系统收敛验收；真实业务仍未通过。详见 diagnostics/2026-10-19-latest-test-review.md。

2026-10-19 全局重构设计：complete（仅方案，未实施）。文档 docs/system-workflow-redesign.md 定义任务控制器、强制分析包、制图编译/页面依赖、审图三身份及处置、证据持久化、兼容迁移和真实验收；后续实施需用户明确启动。20分钟仍为性能目标，不设调用/时间截停。





2026-10-19 用户已明确启动实施，并补充QGIS Processing作为标准分析主路径，geo_python提供兼容的PyQGIS控制台入口。
实施A（implemented / engineering_checked）：持久操作证据、任务依赖控制与状态恢复；QGIS/Python统一执行登记。配置/实现失效、恢复别名、unknown不重发均有回归。
实施B（implemented / engineering_checked）：标准QGIS分析及显式控制台，分析结果发布与事实绑定。真实Processing→事实→工程→草稿交付已验证。
实施C（partially_implemented）：稳定图片契约和持久缓存、页面增量及保守几何检查已验证；完整confirmed/dismissed/fix_verified处置状态机和精确文字/任意符号检测未实现，不能记complete。
实施D（engineering_checked / business_acceptance_pending）：交付复算、中文文档、原生QGIS与安装核对推进完成；独立审查无有效报告，真实模型对话质量/性能未验收。
所有complete仅按实际证据记录；保留主模型high、不修改用户密钥，不采用20分钟强制截停。

2026-10-19 最新西昌测试整改（用户已授权实施，字段说明属于交付要求）：
E1 engineering_checked：修复证据目标身份覆盖；稳定引用、结构化结果和错误，回放翻页失败。
E2 engineering_checked：控制器增量计划/自动单步登记/成功回执含业务结果，减少plan-run-evidence往返。
E3 engineering_checked：Python/QGIS字段中文语义、默认/格式/适用backend、完整示例及精确恢复提示。
E4 engineering_checked：QGIS可编辑工程交付要求，真实图层/布局/相对路径；草稿也不静默漏工程。
E5 partially_implemented：复用已有分析/页面依赖机制；本轮补齐持久问题处置和交付未解决问题传播。精确全部文字/符号检查、真实业务关联准确率与收敛仍待验收。
E6 engineering_checked / business_acceptance_pending：1637项完整回归1636通过，1项续跑问题修复后20项相关复验通过；最终wheel43项、实际安装42项、真实原生工程整链/迁移、126模块+34技能文件一致性及CLI17工具通过。独立审查无有效报告，真实业务质量/效率仍待验收。

收尾新增回归发现执行证据ID被自动续跑误判为进展，离线复现相同结果100次调用；忽略仅回执身份变化后20项相关回归通过，真实业务耗时不据此推断达标。

2026-10-19 最新森林火灾只读审查：complete（仅诊断，未实施）。17分18秒/82模型请求/93工具调用/5套交付前工程。已复现原生自动调度展开默认字段导致合法冻结引用请求失败；交付v5被prefer_checked替换为v1，最终答复版本/页数错误；最近避难和道路距离因gid与行索引错配，各211错值+23空值；持久问题ID投影被空值覆盖，18条无处置。报告 diagnostics/2026-10-19-forest-latest-output-review.md。后续修改待本轮审查后的用户指示，不能将旧工程测试通过视为这些问题已解决。

2026-10-19 用户已授权本轮完整修复：
F1 engineering_checked：稳定ID分析及最近距离独立校验；默认字段身份、顶层证据结果和问题ID真实入口回归。真实森林234点只读复现与正确关联验证通过，不认证任意Python语义。
F2 engineering_checked：普通build/create/export只渲染预览；交付冻结后一次生成工程并复用，包括重启后的同spec_ref入口。固定选定版本，逐页SHA、来源、布局绑定；未知中断记录不自动恢复。
F3 engineering_checked：简短行动摘要、当前/历史问题分离、稳定ID去重、中文字段/help及指南同步，完整证据仍保留。
F4 engineering_checked / business_acceptance_pending：完整1657项快照1634通过/23失败，修复后23项精确复验通过；收尾59项、最终wheel73项通过。已实际安装，127模块+34资源零差异，CLI17工具通过，安装后真实链29项通过。独立审查无有效报告，真实主/视觉模型业务质量与效率仍待验收，长期待办已记录TODO。保留high、配置、源数据、历史成果及.env.example既有修改。

2026-10-19 用户进一步明确流程（取代旧方案的QGIS预览约定）：数据获取→数据审查→空间分析→非QGIS原生制图→原生图片审查通过→交付时一次生成可编辑QGIS文件。标准分析仍可用QGIS Processing；制图/修图不得启动QGIS。当前请求只读审查最新测试、统计问题并提出完整方案，尚未授权本轮新实现。
G1 analysis_complete：审查森林火灾改15:37–15:57现有会话、SQLite、原生请求、15份图片审查及两份交付；89调用20错误、14 build、18 help；7次成功QGIS预览、2次交付工程。补核真实几何距离/名称234条一致，但最终高程由4空值退化为234空值；仅改名册后其他页像素相同，审查没有继承。原始数据SHA未变。
G2 design_complete / implementation_not_started：完整方案已保存diagnostics/2026-10-19-native-workflow-review-and-plan.md。无QGIS制图的统一计划、分析字段契约、独立问题判定、交付准入与一次工程编译；跨渲染器不承诺字节同图，需同计划一致性验证。仅改renderer不够，旧Matplotlib工程是reference。新架构未实施，本轮不修改产品/配置，不运行真实模型或覆盖历史产物。

G3 implementation_in_progress（用户已明确启动）：统一非QGIS制图计划/原生渲染与最终工程编译；审图准入、稳定页面/增量审查；输入别名/不可达预检/同任务调度；分析字段连续性；中文接口与文档；原生多页真实链及wheel/安装验收。主代理负责渲染和交付，独立子任务仅修改输入调度与分析契约，不修改配置或业务数据。

G3 engineering_checked / business_acceptance_pending：统一Scene与非QGIS渲染/增量修图/页面审查绑定、一次最终可编辑工程、字段连续性和中文工具接口已实施并安装；最终wheel21项、收尾40项、原失败12项精确复验、安装后23项、133模块+34资源一致性及CLI17工具通过。真实视觉服务HTTP503；业务质量/耗时和独立审查不作通过声明。细节见diagnostics/2026-10-19-native-workflow-review-and-plan.md。

H轮 implementation_in_progress（2026-10-19 用户已授权）：森林火灾改1审图放大修复。H1稳定逐页验收合同/只送受影响页面，真实4页首次4次、只改2页则新增2次，换进度措辞0次；H2定向问题复核与有证据处置；H3分页文字/覆盖约束/原生排版改进；H4多层GPKG与分析视图身份；H5集成、独立审查、wheel/安装/CLI。诊断见diagnostics/2026-10-19-review-amplification-audit.md。保持high和用户配置、不修改业务输入与既有.env.example，不设时间/调用硬上限。

H轮 engineering_checked / business_acceptance_pending：H1–H4已实施并安装，H5工程集成完成、有效独立审查仍未完成。首轮wheel74项和安装14项通过；数量承诺补强后最终wheel14项、实际安装9项通过，134模块+34技能资源零差异，CLI17工具通过。配置SHA/high保持，制图与审图不启动QGIS、交付一次生成工程的真实链通过。模拟客户端请求数验证不替代真实视觉质量或完整业务速度验收；任意跨页文字/复杂符号检测、锁冲突全来源、未知中断显式恢复仍有边界，不称全局业务已收敛。

I轮 implementation_in_progress（2026-10-20用户授权）：以最新57分33秒会话为基线。I1目标节点依赖与同轮文件校验；I2纸面布局编译、真实区域合同；I3稳定页ID问题/区域迁移与退役；I4验收要求与修复备注分离、观察与裁决；I5真实链、回归、独立审查、wheel/安装/CLI。主模型high及视觉/用户配置保持，不设时间或调用硬上限。

I轮 engineering_checked / business_acceptance_pending：I1–I4实现并安装；I5工程链及发布检查完成，有效独立审查仍缺失。扩大源码141项、集成wheel24项、最终wheel14项、实际安装9项通过，计数重叠不合计；135模块+34资源零差异，CLI17工具通过。受控实际视觉三页通过、重建任务后零请求复用真实三份报告；此前总览warning原报告保留，不能称视觉模型零误判。真实完整high主模型业务对话耗时仍待新会话验收，20分钟不是硬预算。详见diagnostics/2026-10-20-review-system-repair-validation.md。

J轮 implementation_in_progress（2026-10-20用户新要求，覆盖H/I轮不设审图上限的旧约定）：仅严重error触发自动返修；warning/info保留建议且不阻断。默认同任务每页位最多3次实际图片请求、全任务12次，失败计数、缓存免费、任务SQLite持久化；达到上限停止自动绘图/重审并保留未通过草稿，不能伪造通过。更新工具/主模型提示词/调度停止门，验证并准备简单与复杂两个可复现案例。20分钟仍不是执行预算。

J轮 engineering_checked / full_business_acceptance_pending：严重error唯一返修/阻断门槛，warning/info保留建议；默认3次/任务页位、12次/任务的实际请求上限已持久化、原子预留并接入绘图与自动续跑停止。缓存/冷却免费、失败计数；封顶页最后一次通过不阻止其他有额度页及已通过候选交付。源码扩大121项、最终差异24项、最终实际安装18项通过，组间重叠不合计；136模块+34资源零差异，CLI17工具通过。实际视觉合成单页1请求通过，留白/轻微符号贴近为info无返修。简单/复杂两个合成案例及独立答案已生成，140条实体距离独立核对。配置只新增两个上限键，其余解析值相等、主模型high/视觉配置保持；.env.example/原始业务输入未修改。完整主模型耗时和有效独立审查仍待验收。报告diagnostics/2026-10-20-severe-review-limits.md。

K轮 engineering_checked / real_delivery_completed（2026-10-20紧急修复）：真实HookExecutor拒绝内部native_scene_export被冒用为geo_qgis_project参数的缺陷已精确复现并修复，事件归属geo_deliver且沿用公开契约，仍遵守钩子拒绝。真实业务进一步暴露定位框图例色未冻结，补新计划调色板及旧计划精确HEX兼容。11项定向回归45.29秒、最终实际安装3项26.09秒通过，136模块+34资源一致。恢复森林火灾改3的既有v2/11页交付成功，49文件/11页PDF/11PNG哈希不变，未重做分析或审图；工程校验passed。配置和业务源数据不变。细节diagnostics/2026-10-20-delivery-hook-hotfix.md。
