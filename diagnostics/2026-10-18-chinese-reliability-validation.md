# 2026-10-18 中文交互与最新测试可靠性修复验收

## 范围

用户批准直接实施中文系统提示词、默认中文回答、未知业务询问、工具易错防护、逐页审查、分析事实一致性与版式改进。保留业务原数据/地图、主模型与审查模型设置、凭证、上游和指令文件；.env.example既有修改保持SHA256 b2665ac001462f3915270c2f11259bafe3e8ed30a9e882a163b3edaa69c6fbcd。

## 实现

- 中文system prompt与重点工具帮助，统一spec/build与旧冻结创建的适用边界。未知可查先查，业务选择明确提问。
- GeoAskUserQuestionTool在共享澄清状态记录待答问题；引擎在下次请求前暂停，保留完整tool_use/result。空答和单独继续不算答案；明确聊天回答恢复原任务。未修改OpenHarness源码。
- first_coverage要求显式时序和边界政策，未覆盖为空；make_leader_lines按身份连接，省略零长度引线。score_risk显式renormalize仍不证明科学正确性。
- artifact_facts分组统计，write_analysis_report从落盘源生成同源报告并验证显式claims及权重；交付只在包内按SHA256匹配源并复算，不跟随任意报告路径，不对自由文本作全面语义认证。
- 原生页面记录role；名单只按表格用途审查。完成但无法判断与服务失败区分，不自动升为passed。图片/用途/任务/裁剪相同才复用像素结论，包路径本身不触发重审。
- 三张不同图片出现同组完全相同且可定位的错误/警告才建议用户决策；匹配保守，不能识别所有换措辞重复问题。
- RGB默认不进图例，可显式恢复；辅助图层可隐藏图例。名单页有可配置表题/说明/page_columns，保留全部行和顺序；表题按所属页参与几何检查。
- quality_notice列明草稿、剩余问题和分析正确性；各维度分别报告。

## 验证证据

- 分阶段26、19、18、70、78、33、43项检查通过，彼此有重叠，不累加。
- 跨模块回归296项通过（197.16秒）；后续表题/边界与声明权重修订相关22项通过（38.01秒）。Ruff和git diff --check通过。
- 实际原生QGIS合成RGB/32行名单：角色、图例、表题、行数、保存后再导出检查通过；不重绘生产地图。名单受容量限制可分为29+3行，不能把第一页29行当32行完整名册。
- 真实图片请求：首次误用旧安装包，同图7.141秒返回issues，仍将缺地图要素当名单缺陷。随后明确PYTHONPATH=src、打印模块路径，同图相同任务5.141秒返回passed，识别role=roster，说明不要求本页地图/图例/比例尺。两次图像SHA256一致且输入未修改。报告位于.scratch/chinese-reliability-live/report-installed-before.json和report-source-after.json。单次对照不证明所有模型/业务地图准确，也不证明端到端20分钟目标。
- 独立子审查多次仅返回确认语，未取得有效逐行报告；不计为独立代码审查通过。主Agent已人工检查控制流、路径边界、缓存和原生保存/导出。

## 安装与后续边界

最终wheel已安装：.scratch/chinese-reliability-final-wheel/geoharness-0.1.0-py3-none-any.whl；SHA256 8b8a92365bbc4320038020ffa586f334d695e854a52073cf9a9bf845c6c760db。源码/wheel/安装143项（含108个Python模块）无缺失、无差异，记录在.scratch/chinese-reliability-final-parity.json。最终安装版28项回归通过（31.49秒），version/dry-run/离线review-doctor通过，确认加载C:/Users/Administrator/.geoharness/config.yaml，中文提示及助手已生效。后续交付逐页问题汇总37项、取消等待15项检查通过（均与大回归重叠，不累计）。既有业务输出未重新生成，旧版手写报告错误不会被回溯自动改写。新机制提供明确助手/检查和生成路径，不能保证任意模型生成脚本从不出错。
