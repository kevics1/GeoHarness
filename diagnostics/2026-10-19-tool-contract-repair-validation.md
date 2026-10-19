# 工具契约与交付修复验证（2026-10-19）

本轮在隔离工作树实施并更新本机实际安装；不修改上游、指令文件、原始业务数据、历史地图、用户配置或凭证。主模型思考强度仍为 high。工程验证、原生图片审查和真实业务验收分开记录。

## 实现与影响

- 普通原生单步自动登记、校验权限/依赖并持久化；显式 execute 一次登记执行，多步 plan 默认增量。未变化成功节点复用，同参失败不机械重发，未知中断不自动重发。
- 模型可直接读取业务 result，不必先 plan/run 再 evidence。证据 operation_id/target_operation_id 保留被读目标，execution_id 标识读取执行；next_call 原样分页原目标。
- 已发出数据引用随任务持久化，上下文轮换不撤销旧引用；恢复仍核验文件与来源，不授权任意目录。
- Python 两种 backend 支持 CONSOLE_RESULT 有界 JSON；不能返回原生对象、NaN或无限值。报告助手支持 .md/.json，并成对保存两份；已知错误后缀在运行前拒绝。
- 12 个 geo 工具的全部公开/嵌套字段有中文语义、单位与条件说明，Schema 本身保留类型、默认和范围。复杂QGIS布局/编辑按需 help，运行时继续严格校验。
- 地图交付必须有真实 .qgz/.qgs；从登记主图恢复已有工程，或按明确 spec_ref 在交付前生成工程。无工程和草案明确 missing_project；分析地图不能用手写事实文件绕过真实事实包。纯数据兼容，草稿也保留工程。
- 持久审图问题支持 confirmed/dismissed/fix_verified。未再次报告保持 needs_recheck；修复核验要求同图同页、新像素、定向原生 passed、SHA一致，仅改PNG元数据不能证明修复。未解决问题传播到草稿交付，处置不直接认证业务/视觉质量。
- 回归另外发现证据身份被续跑误认成进展：相同结果可从预期2次膨胀至100次。已在进展指纹中排除执行回执身份，同时保留真实业务值变化。

## 工程证据

- ruff：src/tests 全部通过。
- 字段帮助、调用等定向回归：76 passed；续跑与调用：20 passed；审图相关：59 passed。计数重叠，不相加。
- 最终 wheel 关键回归：43 passed，包含字段、证据、Python结果、审图状态、交付与续跑；无付费模型请求。
- 源码完整回归：1637项，1636 passed / 1 failed，耗时1457.74秒。唯一失败为上述续跑回执身份问题；修复后对应真实引擎/调用20项通过，最终wheel43项及实际安装42项均覆盖该行为。没有把修复前整套结果改写为全绿，也未将重叠回归计数累加。
- 最终实际安装回归：42 passed（无PYTHONPATH），包括直接调用、重复失败保护、字段帮助、证据分页、交付、审图状态与真实引擎续跑。
- 同一默认 GeoConfig 的17工具 Schema，按 json.dumps(ensure_ascii=False) 计数：安装前83616字符，修复后49652字符；QGIS工程工具30578→8215字符。不是tokens数，也不是实际对话耗时承诺。

最终wheel实际调用真实PyQGIS/Processing：分析→发布事实包→保存草案→交付时生成工程，4节点成功，第二次执行0节点，原始来源SHA不变。复制工程后实际 Project.read 成功，所有图层有效，数据源位于新工程目录，1个布局存在。产物是合成工程草稿，不代表西昌业务地图或原生图片视觉审查通过。

证据文件位于 .scratch：final-contract-suite.log/xml、contract-help-final.log/xml、contracts-progress-final.log/xml、contracts-review-final.log/xml、contracts-wheel-final.log/xml、contracts-installed-final.log/xml、contracts-native-final.log、contracts-parity.json、contracts-schema-installed-before.json、contracts-schema-source-after.json。可迁移工程验证为 relocated-project-ed2677b6/open-verification.json。

## 安装与使用

wheel 已构建为 .scratch/contracts-build/geoharness-0.1.0-py3-none-any.whl。源码/wheel/实际安装126个Python模块和34个技能文件逐字节一致；实际 D:/Python312/Scripts/geoh.exe dry-run 识别17工具。

常规 force-reinstall 遇到旧 geoh.exe 被占用的 WinError32，pip已移除模块而未完成重装。随后用同一wheel的 --no-deps --upgrade --target D:/Python312/Lib/site-packages 恢复并更新模块，保留既有启动器，未终止用户进程。已通过实际启动器运行dry-run及安装一致性核对。已在运行中的旧会话应退出后重新 geoh run。

安装前后 config.yaml SHA256 一致，agent.reasoning_effort=high。审图模型、地址、凭证与思考参数未修改。

## 尚未通过的验收

真实主模型/图片模型的同输入对照、视觉问题关联正确率、实际端到端耗时和p50/p95尚未验收；不承诺任意任务零错或固定20分钟完成。没有采用时间/次数硬截停。

独立代码审查代理此前未产生有效报告，不能计为独立审查通过。全部文字与任意符号的精确几何检查仍未完成。真实业务仍按 TODO.md 和 system-workflow-redesign.md 的验收矩阵核对。
