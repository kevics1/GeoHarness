# Oct17 第三轮实测工具完善与验证

本轮按四阶段完成工具实现、定向回归、安装一致性和安装版合成流程验收。修复先在隔离分支 `codex/oct17-tool-contracts` 实现（基于 `aaf7f3c`），随后在用户确认以当前会话为准后，于2026-10-17保留备份并快进合并回6049工作区。新版已重新构建并安装至现有 `D:/Python312` 环境；最新集成证据见下文，早期验证批次与历史wheel哈希保留用于追溯。

## 修复后的行为

| 范围 | 行为与验收依据 | 实现提交 |
| --- | --- | --- |
| 台账存储 | 短JSON定位SQLite，版本独立保存、重复report按哈希去重，版本与任务头事务提交；保留既有字典API。当前状态读取不加载历史报告。旧台账只读查询不隐式迁移，显式recover保留精确备份并全量等值验证后切换。 | 16b0650 |
| 故障隔离 | 保留成功渲染的实际路径和执行状态。台账不可读时，显式review-only仍能保存本地报告，selected交付仍可复制草稿；均不提升地图质量。清理失败不覆盖原异常、不遗留运行时所有权。复制完成后才登记失败也返回真实目录和delivery.json。 | 16b0650、fae07b2 |
| 分析输入与事实 | input_requirements在子进程guard生效后检查实际schema、ID、CRS和几何；未绑定的literal INPUTS访问提前失败。extent_geometry明确范围顺序和CRS。artifact_facts从持久文件复算统计、缺失数、条件计数及面积，check_claims核对明确声明。非法几何报告有界行号和原因，提示目标投影中再次校验。 | a0222c6、fae07b2 |
| 布局和名录 | 尺寸不变候选不再递归，重复状态返回layout_no_progress；hard zoom和主体覆盖不放松。key_field独立于显示编号，筛选、排序、格式与分页保留原始身份。field_widths错误明确列出未显示字段。 | 06329ab |
| 审核与恢复契约 | 当前图框记录purpose、实际标签启用源（含保存的样式覆盖）与标签落位；矛盾的“未落位”断言不能授权改图，也不能自动变成像素通过。severity_reason仅为说明。修复另存工程恢复Path类型不一致、完整规格round-trip默认selector误拒；历史窗口锚点与JSON分页分开，长路径分段read_call可正确重组。 | 06329ab、fae07b2 |

没有增加累计制图或审核次数限制，没有新增运行依赖，SQLite使用Python标准库。没有修改AGENTS、Skills、主模型配置、凭证、原始GIS数据、旧地图、上游OpenHarness或既有.env.example状态，没有推送GitHub。

## 验证记录

- 阶段1：86项回归通过，覆盖台账、修改互斥、登记失败、审核与交付。
- 阶段2：65项执行回归通过，覆盖输入字段、CRS、持久事实、既有Python隔离和输出完整性。
- 阶段3：22项布局/逐页审核/语义进展回归通过；随后可选说明字段和非法field_widths检查通过。包含实际独立QGIS进程的双页/隐藏ID测试。
- 首次较大回归为334通过、2失败，定位并修复Path类型和默认selector问题。修正后的跨阶段回归为 **337 passed、0 failed、0 skipped**；JUnit：`.scratch/oct17-final-clean-regression.xml`。pyproj/NumPy有120条既有兼容性弃用警告，没有测试失败。
- 补充规格/冻结计划/绑定复验：21通过。新历史窗口、根分页、长路径read_call测试及4项规格检查共5通过；后续仅调整代码换行，最终全src及新增测试Ruff通过，git diff --check通过。这些批次有重叠，不累加为独立测试总数。
- 真实旧台账仅迁移副本：原文件16,874,939字节，68个版本；迁移后locator为100字节，68条版本、200条去重报告，恢复内容与旧JSON完整等值。健康回执213字符、恢复回执888字符。原始源SHA256保持 `60a7fe8955290471812eda6a904a735b8aea97b95ec895e2768bf6a114589a3f`。记录：`.scratch/oct17-real-ledger-copy-evidence.json`。未迁移真实业务目录本身。
- 构建wheel并以 `--no-deps` 安装。源码、wheel和安装目录 **96个Python模块、34个随包技能文件零差异**；wheel SHA256：`3fb156cd89b3d5bb46e3c2c8a5fa42a4005d462749d38441f4afaf1784aa836b`。记录：`.scratch/oct17-tools-parity.json`。初次无隔离构建因本机未装hatchling失败，随后使用项目声明的隔离构建成功；没有改变运行依赖。
- 安装版公开工具串联：合成输入契约 → 分析（缺失时间保留unknown）→ 持久事实 → 声明检查 → spec → 原生preflight → frozen create → 隐藏身份名录与逐页标签角色 → 实际交付。输出三页，名录成员a/b正确，facts/claims/method/result和可编辑工程均交付。模型请求0次，像素验收unverified，交付质量保持draft。记录：`.scratch/oct17-installed-acceptance.json`。
- 源码和安装版version/dry-run通过，16个工具组装正常；MCP为configured/not connected，此检查不证明外部服务连通。

## 使用与剩余验证边界

重启已经运行的GeoHarness进程后加载新版。旧业务台账不会因本轮安装而被自动迁移；使用新版进程的 `geo_qgis_project(action="inspect",map_id="实际ID")` 查看健康，按返回的recover_call显式迁移。恢复前应结束同一工作区仍使用旧代码的写入进程。只读分页应原样保留返回的history_offset/history_limit锚点。

本轮证明的是上述工程契约和合成工具链通过，没有重跑234处居民点的真实分析或真实模型整图，未证明业务结论、权重科学性、逐页像素可读性或无人值守最终完成率。check_claims只核对显式结构化声明，不自动审计任意自然语言；面积是逐要素之和，重叠可能重复。像素占比不是视觉验收。

尝试派发独立代码审查，但审查任务返回了未执行任务的确认语，没有有效审查报告，因此本轮不计为独立审查通过。证据来自主执行者自检、故障注入、真实副本等值验证、定向回归及安装版合成串联。需要有效独立代码复核和真实业务验收才能扩大结论。

当前修改互斥仍限于同一运行时。兼容全历史receipt API仍会线性加载历史报告，低成本接口是任务头和分页健康查询；没有声称所有旧API已经变为恒定成本。

## 2026-10-17 当前工作区集成验收

- 代码已从aaf7f3c快进合并至f163ab8。合并前10个重叠文件及完整差异保存于 `.scratch/integration-backup-20261017-132813/`，逐文件SHA256核验一致；8项保留文件（包括.env.example及无关诊断文件）合并后哈希不变。源码、tests和skills与已验证分支一致。合并首次被Git的行尾/索引缓存状态阻止；确认实际diff为空后仅刷新对应文件索引，再快进成功，未覆盖额外改动。
- 追加修复复制后读取台账失败：返回真实交付路径和delivery.json，delivery_status=copied_unregistered、receipt_status=failed、quality_status=draft，保留ledger_errors/next_action，不写旧map-receipt。复制前默认校验仍拒绝不可读台账；显式selected可保存草稿。新增故障先复现为失败，修复后交付相关47项通过；最后guard调整后的故障隔离9项通过，合并后下述80项亦覆盖该路径。批次有重叠，不累计总数。
- 当前6049工作区：9个相关测试文件 **80 passed、0 failed**，53.77秒；`.scratch/oct17-integrated-regression.xml`。Ruff全src与新增测试通过、diff检查通过。没有运行全量pytest或真实业务整图。
- 最新wheel SHA256：`afacd66775c6dbfcd5c85c6c1fe208c3521564d40606893728f66e43f9b3b945`，源码/wheel/安装目录96个Python模块与34个随包文件零差异。证据：`.scratch/oct17-tools-parity.json`。仅重装GeoHarness，未修改运行依赖。
- 当前目录安装版合成串联通过，三页、隐藏名录身份a/b正确，实际交付工程和事实/声明/方法文件；模型调用0次、quality_status=draft、visual_acceptance=unverified。证据：`.scratch/oct17-installed-acceptance.json`。源码和安装版CLI version/dry-run均通过，16工具；MCP未连接。
- 独立审查再派发两次仍未执行任务，不能计为通过。真实数据/真实模型业务验收、逐页像素质量和无人值守完成率继续待验证；原始业务台账未被迁移，仍使用显式recover入口。
