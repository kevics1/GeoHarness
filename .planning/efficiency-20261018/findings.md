# 核实依据

- 上次实测119工具调用、36build/22native生成、27审图/37逐页请求；原生任务+逐页视觉约12分钟，另约21分钟未精确拆分。
- 时间目标错误地在engine的MaxTurnsExceeded分支停止自动续批，需要移除；用户不接受以20分钟截停。
- review_progress将完整问题文字集合严格比较，37份报告均未触发实际收敛判断。
- 最新编号几何与引线分别修改，12/13号方向偏差90度；计算正确的避难距离在最终文字中失真。
- 报告JSON+MD已生成，交付缺JSON，analysis_correctness仍unverified。
- GitHub预检了LangGraph ToolNode/InjectedState的状态注入与缓存模式；已有OpenHarness适配/SQLite/执行契约可完成，避免引入LangGraph及其依赖；本轮只用现有依赖。
- 初始工作树.env.example已修改，另有.scratch/诊断/db未跟踪；全部保留，不计本轮变更。
