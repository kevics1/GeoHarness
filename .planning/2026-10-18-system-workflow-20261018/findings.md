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
- G轮最新森林火灾改：会话144b38e6451c，任务6ed3a200-ac52-4783-a3c8-2e8ff9860fa7，1204.219秒；模型74次774.162秒，工具时间并集402.905秒，89次可见调用20错。geo_qgis_project37次含18 help/14 build/5 spec，10错；Python25次8错；图片15次13 issues/2 passed无缓存，无服务超时。native 11次=7成功预览+2冲突拒绝+2交付工程，并非14套qgz。
- G轮真实问题：已声明input_refs的fire别名仍被read_vector判未登记；AST对if False分支INPUTS['ev']硬报缺输入；同一模型批次Python与publish_analysis因submit_plan在异步锁外先取OS锁而自相冲突。字段/库API猜测、headers字典/labels必填/patch动词错误、帮助分页返回结构多形态仍有问题。
- G轮事实与审查：metric_fix_4543脚本从原pts重建，仅当原pts含elev_m才复制，否则np.nan，导致旧抽样4缺失变最终234缺失；事实包未纳入elev_m也未声明nearest_checks。独立重算最终234条蔓延/避难距离及避难名称0错，未证明其他全部科学结论。最终页脚标EPSG:32648而Python主要结果EPSG:4543，来源被混合展示。
- G轮页级断链：v6与后续仅改名册版本主图/detail PNG的SHA相同，record_map_version新建版本却不迁移未变页审查；最终只名册passed、全册unverified。27问题状态为22 needs_recheck/5 open，无confirmed/dismissed/fix_verified；15份repair_progress全not_established，虽均有bbox仍没识别反复同类问题。模型猜书房不能是学校属于数据语义疑点，不是像素可证缺陷。最后交付缺准入硬门槛，issues/unverified仍生成工程。
- G轮新增直接台账证据：v7创建于15:55:40，主图被设为旧roster_page_3.png且无package，发生在geo_map_review读取该页，不是新渲染。首次交付迁移package到最终目录后，原预览次级页定位失效触发单图登记。审图不能创建版本，需持久稳定页归属，不能只按当前包路径猜。

- G3收尾自查新增：页面layers声明顺序被原生绘图的全局图层遍历覆盖，而工程按页面顺序导出，存在实质跨渲染器不一致；已改为显式逐层zorder。VRT的底层TIFF变化未进入页缓存/控制器指纹，已将实际依赖SHA纳入每层库存，并添加真实像素/文件变更回归。全量快照1680项1668通过/12失败，旧兼容用例需经过绑定审查才能物化；不得为了迁就旧测试允许未审查生成。独立审查此次仍未获得有效报告，不计通过。

- 最终原生登记恢复回归先失败：渲染落盘后record_map_version抛错，下一次render缓存返回reused而永不补登记；已根据实际artifact_registered决定是否补登记，复用图片且只产生一个版本。原12失败当前wheel精确复验12通过，收尾集成40通过，最终wheel21通过。实际安装133模块/34资源零差异；geoh实际版本命令为version而非手册所述--version。


H轮确认：逐页 checks 随自由措辞漂移、all_pages 忽略已继承页结论，以及旧问题缺少逐项明确核验，是96份逐页报告中44份重复像素报告的主要系统原因。已用稳定逐页要求和持久页调度、同次请求定向核验闭环修复；不降低像素 warning、不用随机 passed 或硬次数限制。coverage 采用显式对象/几何约束，不使用标题数字正则推断对象全集，避免河流/行政区等多层合计造成新误报。分页自动文字、符号/引线避让和多层GPKG视图绑定已纳入回归。真实受控数据回放与真实AI/业务验收分开，独立审查尚无有效报告。

H轮收尾补强：详图标题的明确数量仅用于要求提供coverage，以及核对唯一覆盖约束数量；不据此推断实体全集。最终wheel14项通过，134模块+34技能文件与实际安装一致，配置SHA保持基线；实际CLI17工具通过。任意跨页自由文字、复杂符号遮挡、全部Python错误和该次单锁冲突不作已彻底修复声明，真实业务验收仍待完成。
