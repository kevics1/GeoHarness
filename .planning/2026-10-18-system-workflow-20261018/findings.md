# 依据

- 业务72ea66212d55：67次工具、30次Python/11失败、13build、37页面报告/39视觉请求，20分23秒。详见diagnostics/2026-10-18-latest-test-review.md。
- jobs._map_page_files要求frames非空造成名单排除；record_map_review未知页静默返回旧receipt，批量页成功不等于登记成功。
- task/stage已具有依赖哈希与session checkpoint，复用并建立唯一权威引用，不复制事实。
- Github预检LangGraph checkpoint/Pydantic；现有依赖足以实现，不新增框架、不移植上游代码。
- 初始.env.example既有改动及.scratch/旧诊断/db均保留。
- 2026-10-19 架构复核：TaskRecord.operations按tool:action覆盖，只记录最近观察，不能授权下一步或约束质量；engine._run_query_batches仍依据成功结果指纹自动续跑；geo_task目前主要是评分入口。应把执行权提升为任务契约和阶段依赖，而非再添加提示。设计详见 docs/system-workflow-redesign.md（待实施）。
