# 依据

- 业务72ea66212d55：67次工具、30次Python/11失败、13build、37页面报告/39视觉请求，20分23秒。详见diagnostics/2026-10-18-latest-test-review.md。
- jobs._map_page_files要求frames非空造成名单排除；record_map_review未知页静默返回旧receipt，批量页成功不等于登记成功。
- task/stage已具有依赖哈希与session checkpoint，复用并建立唯一权威引用，不复制事实。
- Github预检LangGraph checkpoint/Pydantic；现有依赖足以实现，不新增框架、不移植上游代码。
- 初始.env.example既有改动及.scratch/旧诊断/db均保留。
- 最新西昌测试18分38秒/108请求，Python33操作15失败，控制器36计划32单节点/34证据读取；证据包装operation_id覆盖使翻页读取自身；Matplotlib路径绕过QGIS工程与结果包，最终project=null。人工确认v3名单丢行、v6英寸误作米，后六版重复写相同分析图层。详见diagnostics/2026-10-19-latest-contract-review.md。整改按实际失败建回归，不以长提示或固定次数截停代替设计。
- 2026-10-19 架构复核：TaskRecord.operations按tool:action覆盖，只记录最近观察，不能授权下一步或约束质量；engine._run_query_batches仍依据成功结果指纹自动续跑；geo_task目前主要是评分入口。应把执行权提升为任务契约和阶段依赖，而非再添加提示。设计详见 docs/system-workflow-redesign.md（待实施）。

- 本轮实现完成后字段审计发现旧工具/嵌套契约仍有纯英文描述，已补齐中文并增加全12工具的字段/描述回归。最终回归另复现自动续跑将新证据身份视为进展，已修正；不采用时间/次数硬截停。

- 最新森林火灾审查：create将预览与完整工程物化耦合，交付前生成5套qgz（preflight不写qgz）；直接入口model_dump展开24字段破坏model_fields_set约束，最小build/create/inspect均本地复现；prefer_checked以历史背景覆盖排序将请求v5换成v1。更严重的是自由Python把gid索引的最近邻Series直接赋给RangeIndex，避难/道路字段各211错值+23空值，事实发布仅验证落盘自洽而漏过计算错配。报告 diagnostics/2026-10-19-forest-latest-output-review.md，本轮未修改实现。

- 物化逐页身份最初因PNG Created元数据、名单列宽XML舍入失败；关闭PNG元数据并只在内存规范化布局后真实双页图片SHA一致，未放宽审查绑定。用户工程没有GeoHarness自定义字体信息，需从真实布局标签字体绑定，不依赖info['family']。
- 原生控制台ConsoleGuard.path调用WorkerContext.read_path时，后者读取文件头触发同一audit递归；仅在可信路径/依赖验证期间暂时停用重入检查并在finally恢复，保留原始读写限制。真实用户工程创建/导出/交付专项已复现修复。
- F阶段收尾补测真实双页流程时，跨会话以同一spec_ref交付会绕过内存render cache并重复生成工程（1项真实失败，43.73秒）；已增加预览创建契约与首次物化记录中的spec身份，并在生成前查询当前版本的持久关联。仅同一草案、同一版本和完整文件绑定可以复用，不扫描或切换历史候选，复验中。
