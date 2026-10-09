# 会话进度

## 2026-10-07
- 读取用户指定 grill-me 技能与 planning-with-files 技能。
- 轻量检索 MEMORY.md 无相关记录；没有采用历史项目结论。
- 核查项目指令、依赖、README、源码及测试清单。
- 阅读工具注册、运行时组装、系统提示词、栅格工具与空间分析技能。
- 确认当前没有项目自身的多模态输入/图片理解实现；开始澄清图片理解的输入类型。
- 尚未修改功能代码、运行测试或提交。
- 用户确认图片理解优先用于检查自己生成的地图。
- 追踪渲染工具的输出与现有测试，确认项目尚无成图视觉检查闭环；下一步澄清 GeoHarness 的自动纠错行为。
- 用户确认启用自动修正并重绘。
- 核查本地安装的 OpenHarness 0.1.9，确认客户端已有 ImageBlock 多模态消息转换支持。
- 接下来选择空间分析的首个完整验收任务；尚未修改功能代码或发起真实模型调用。
- 用户选择首期多图层叠加与影响范围分析，空间热点统计和栅格分析后续实施。
- 核查矢量工具、文件加载和 MCP 配置，确认原生运算入口不足；开始澄清固定工具与自主脚本执行的取舍。
- 用户选择原生 GIS 工具与自主 Python 分析组合。
- 核查上游 ToolResult、ConversationMessage 与图像预处理，确认返回成图路径不会自动让模型看图；必须显式构建视觉检查调用。
- 当前需求已明确首期场景、分析执行方式和自动重绘；接下来确认实际模型服务的图片输入兼容性。
- 完成聚合服务可达性与完整模型 ID 的图片输入验证，模型正确识别合成红色矩形。
- 用户授权同步更新两份项目技能；需求访谈阶段完成，进入实施。
- 根据项目委派约定，将互不重叠的原生分析和 Python 执行模块分别委派；主 Agent 负责质检、整合与最终验证。

## 实施与验证进展
- 原生 geo_analysis 已覆盖 buffer/clip/intersection/distance/count/impact，结果、CRS、参数与来源保存至独立目录。
- geo_python 保留脚本、日志、manifest 与产物；常规文件/GIS 接口限制写入，清理凭证环境，超时终止子进程。它用于可信 Agent 脚本，不是 OS 沙箱。
- geo_cartography 显式将真实像素送入模型，采用严格呈现参数白名单和最多两轮修正；所有检查/预览/快照使用唯一目录，复用矢量和栅格数据。
- 独立审查发现并修复：公开 GDAL/Arrow 入口 guard 缺口、VRT 间接敏感引用、PDF 预览覆盖底图，以及单图层参数修正不生效。最终针对性复验 15 passed，无 Critical/Important 遗留。
- 有效基线测试 379 passed；测试必须指定工作区 PYTHONPATH 和工作区 basetemp，避免全局 editable 安装指向其他 checkout 与受限系统临时目录。
- 针对性整合 178 passed；最终制图/配置启动 46 passed；Python 执行最终 29 passed。完整测试正在运行。
- 真实模型验收：合成三对象距离 0/100/300 米，200 米筛出两个对象，分区计数 2/1、受影响计数 2/0，输入文件哈希不变；实际生成 PNG 的视觉检查返回 passed，无需重绘。
- 此服务的 (max) 模型在 2400 与 8192 token 预算下发生截断；示例配置提高为 12288 并缩短检查回复要求，成功完成 JSON 检查。截断状态保持 unverified 并附配置建议。
- CLI version、dry-run --config、run --help 均通过；实际 print 模式分析验收进行中。
- 已更新项目两份技能（用户授权）、README、user-guide.md、配置示例，长期待办记录在本地 TODO.md。
- 最后实际 CLI 暴露新的工具串联缺口：模型不能读取 ToolResult.metadata，且 Python 默认禁止 .geoharness/analysis 产物；已在工具文本提供准确 paths 和最多20行属性预览，并将配置产物子目录作为精确只读根。源文件写、.env/secret和配置根认证仍拒绝。新增针对性10 passed、原生/工作流链路82 passed。
- 首次最终全量运行在406 passed后遇到旧提示词断言失败；该断言仍要求点数据必须走MCP Moran聚合，与用户确认的首期原生影响分析方向不符。已更新为原生影响分析和真实成图检查指导，重新运行完整检查。
- 完整CLI分析→中文多图层制图→实际图像检查已跑通：inside/nearby受影响，west总数2/受影响2，east总数1/受影响0；未再猜测输出路径。
- 比例尺按实际画布范围计算，正确计入留白和US survey foot换算；相关60项渲染回归通过。
- 默认地图输出目录集中在CartographyConfig，旧配置省略路径时Python也可读默认地图；结构化record_map_review仅解析单个返回，不执行工具，严格拒绝数据参数变更。最新配置/运行时/启动/制图回归138 passed，独立窄审6 passed，无阻断问题。
- (max)视觉模型思考长度有波动，12288仍出现截断；服务模型列表声明maxOutput=128000，示例调整32768/180秒。修正比例尺后的最终中文地图实际检查passed，报告cli-impact-map.review-30n9qiir/report.json，0轮修正。
- 之前全量462 passed；最终全量含新增回归正在运行。ruff与diff检查通过。
- 最终全量验证：467 passed, 33 warnings in 208.00s；警告为上游Typer/pyproj弃用提示与未知CRS测试夹具警告。ruff check和git diff检查通过。
- 最终交付材料齐备：原生空间分析、自主Python、像素审查/最多两轮重绘；配置示例、两份项目技能、README、使用指南与规划记录。真实CLI与修正后的中文地图均验收通过，独立审查无阻断问题。
- 运行产物和合成数据保留于被Git忽略的 .geoharness/；本地TODO按仓库既有忽略规则保留。仅实现、测试、技能与文档纳入提交。

## 追加：任意终端目录启动
- 现有 PATH 已包含 D:/Python312/Scripts，但 GeoHarness editable 原先指向旧 F: checkout；构建包含所有 Python 模块和 33 个技能资源文件的 wheel，并仅替换 GeoHarness 安装。
- 运行时优先使用安装包内技能，保留源码/开发安装回退；56 项技能/运行时回归通过，ruff 与 diff 检查通过。
- 不依赖 PYTHONPATH，在 C:/Users/Administrator 运行 geoh version、dry-run 与 run --print 均退出成功；在 D:/ 组装确认使用 site-packages 中的新包、5 个顶层技能、12 个工具和当前目录作为 cwd。
- 在用户主目录实际启动默认 React TUI，显示当前全局模型并正常交互待输入，再用 Ctrl+C 退出，退出码 0。
- 核对持久 Machine PATH 包含 D:/Python312/Scripts，geoh/geoharness 均解析到该目录，无需临时 PATH 或源码目录设置。
- 最初发现全局默认模型仍为旧 GLM 配置；最终按用户先前明确指定，将全局 YAML 的 model 同步为 cbcn/deepseek-v4.1-flash(max)，加入已验证的 map_review 32768 token/180秒预算。逐字段确认所有其他配置不变，保留 .env 与凭证；在用户主目录再次启动 print 模式，显示 DeepSeek 模型并正常退出0。
- 现有配置的 geo-mcp-server/postgres 模块缺失，连接警告后继续启动；未擅自移除配置。
- 更新用户指南中的安装/更新和任意目录启动操作；保留用户原有 .env.example 改动，不纳入本次提交。
- 独立只读审查核对 wheel 文件内容及上游 loader，5个顶层技能均从安装目录加载；没有本次引入的 Critical/Important/Minor 问题。既有认知技能旧调用提示不影响本次入口，未扩展修改范围。

## 追加：实际运行诊断、有限审查与QGIS交付
- 检查example的26次Python记录（23成功、3失败）和对应会话；另一次空参数在校验前未执行。SOURCE_CWD/DATA_DIRS环境缺失、RUN_DIR外写入及DEM窗口越界分别有脚本/日志证据。16次图片审查中9次因token耗尽未验证，存在重复图片检查及8个火情版本。
- 上游项目状态函数在GeoHarness进程内适配到用户.geoharness/workspaces；保留cwd，实际插件/自动任务初始化不再创建工作目录.openharness。仅清理example已核实为空的autopilot/plugins及父目录，不修改上游源文件。
- Python同时提供Path全局变量与环境变量；修补Windows CopyFile2/copytree绕写保护，保留源数据只读。工具结果提供status、exit_code及error_summary，空code报明确未执行错误。
- 所有制图工具共享用户请求预算：每图最多初版加2修正、所有图片4次/120秒；缓存未变图片（含失败）及合并并发同图请求。处理常见绘图导入别名，passed/unverified/info-only/上限均停止完善。
- 原生制图自动附带工程，project动作给既有Python成图打包实际来源而不重绘；QGS真实数据/样式/CRS/画布/基础布局及相对引用，GPKG/TIFF副本、JSON、重绘脚本和说明完整。零影响空图层可保留，既有用户编辑不覆盖。
- geo_deliver交付明确选定的文件和工程到cwd/exports唯一目录，文件重名（含Windows保留名大小写）不覆盖。默认配置、初始化模板、系统提示、两份已获授权技能及用户指南同步。
- 独立只读审查确认六项P2边界问题，均修复并复验；最终无新直接相关问题，16项相关测试通过。
- 全量509 passed；实际QGIS发现地图框高度按extent比例恢复后超出纸张，已匹配地图框比例，并验证真实尺寸在纸张内；相关18 passed。最终CLI/端到端17 passed，ruff及diff检查通过。初期技能测试依赖真实用户home导致权限失败，改用隔离配置和合成凭证；测试目标不存在的调用已按真实test_e2e文件纠正，不作为通过记录。
- 使用本机QGIS3.44.7实际读取、验证有效图层、关联比例尺和导出中文布局；Windows Qt offscreen无系统字体，验证进程显式加载本机字体后正常，不修改桌面字体设置。
- 实际本地模型完整流程核验影响数2，地图/报告/QGIS工程/交付成功；该次4096token审查耗尽，正确报告unverified并停止，无伪报passed。非max模型的空图探测约7秒返回有效检查，不能保证业务图每次通过。
- 用户既有两张PNG/PDF哈希完全保留，补充实际图层与原分类工程到 F:/Desktop/Project/GeoHarness/example/exports/20261007-补充交付-18310bde，含原脚本/manifest、分析报告、诊断、QGIS真实布局预览。详细Python注记和特殊符号可能需参照原图手动补充，已说明。
- wheel核对新模块/工具/技能完整，已更新D:/Python312全局安装与用户YAML审查/交付配置，主模型仍为指定max，其他配置与凭证保留。example中geoh version、dry-run、实际run --print退出成功，无.openharness再创建。真实安装包init在隔离home验证输出为相对exports；用户原.env.example改动不纳入提交。

## 追加：test业务地图质量回归
- 读取test实际地图、Python脚本、QGIS预览及对应会话，确认固定文字偏移、放大图压盖、失败绘图耗预算、审查token耗尽和工程预览降级。探测9Router真实业务图，确认独立(none)配置才能在本机返回有效审图结论。
- 加入独立分区与真实文字边界避让、布局报告先检、失败绘图返还、累计有效调用时间和缓存配置指纹；实际视觉请求独立配置且关闭内部重试。错误/警告才触发修正，不因无对应问题的纯样式patch否定passed。
- 主图、工程预览和审查绑定SHA256；按真实分类字段/配色打包工程，锁定范围，恢复旧工程兼容；交付明确passed/draft，不以像素或工程创建代替质量证据。
- 整合全量554 passed；独立审查指出分类图例缺省/兼容、布局标题边界、运行时审图配置旁路，全部修复。最终相关108 passed，ruff和diff检查通过；不将较早全量运行误记为最后小改后的全量结果。
- 沿用原风险输出重绘，分区、标签和文字检查全部通过，无重点编号省略。QGIS 3.44.7读取10个有效图层并导出基础布局，主图与工程预览SHA一致。视觉模型成功返回约9秒，仍有需判断的意见，交付如实保留draft。

## QGIS 执行与工程集成（2026-10-08）
- 按用户当前范围优先复用PyQGIS/Processing，前期五项地图质量改进延后。
- 新增geo_qgis doctor/search/help/run/model，geo_qgis_project create/inspect/export；配置、工具注册、权限、提示词及获授权的两份内置技能已接入，原有能力保留。
- 本机真实buffer/clip/slope/model3及工程创建、PNG/PDF、读取、导出、交付已跑通。保存标题/分类色/布局名称后的再导出与输入不变验证通过。
- 独立审查发现的源修改算法、extent/CRS隐式文件读取、模型来源类型和交付复制路径均已修复并针对性复核。QGIS内置SVG、旧资源格式及布局锁定样式经真实导出后重读验证通过。
- 首次整合进程导入过时test helper产生7个FrozenInstanceError；当前构造式helper与config组合42项通过。全量首轮616 passed、6 failed，其中5项为目录rename访问错误，1项为旧提示词预期；预期已更新，系统临时目录中相关126项通过，最终全量636 passed。
- 安装版诊断暴露worker把主site-packages置顶产生NumPy/GDAL ABI错误，改为只加载GeoHarness包。新增源码/wheel布局的真实子进程隔离回归，独立审查通过。
- 最终wheel已构建并安装到现有D:/Python312环境，未更改模型、审查配置、凭证或主NumPy。安装版从F:/Desktop/Project/GeoHarness/example/test调用完整合成分析/工程/交付、model3与用户保存工程导出均通过，doctor无依赖stderr，geoh dry-run组装15工具；ruff全通过。
- 新交付位于 F:/Desktop/Project/GeoHarness/example/test/exports/西昌地图布局修复-z2rkt69i；风险GeoJSON、CSV与附图字节保持不变。完整Python分区图有可移植redraw-layout.py及数据副本，实际在该交付目录重绘成功，布局再次通过。
- 构建wheel并更新D:/Python312全局安装，保留主模型max及凭证，独立审图为none；test目录全局geoh version/dry-run成功，加载site-packages版本，未创建.openharness。用户原.env.example未纳入提交。
# 2026-10-08：启动与工具后空回复修复

- 复现真实工具成功后空消息漏存结果；会话现在保存最终失败前完成的工具结果，完整会话可恢复，继续处理不自动重执行工具。
- 主模型保留max；显式一次none空回复恢复已实测。用量在SDK chunk确认，取消补记，成功确认后不重复计费；部分正文中断保存并显示未完成，残缺工具不执行。
- Geo MCP真实模块入口握手18工具、CRS转换与rasterio EPSG读取通过；map-making兼容读取实际cartography。新init不再生成不存在模块入口。
- 全量681项通过；最后init模板更新后相关36项通过，ruff与diff检查通过；独立评审意见复核关闭。
- 已构建并更新D:/Python312全局安装，用户example/test目录geoh version/dry-run加载GeoOpenAICompatibleClient与GeoQueryEngine。全局配置备份并保留主模型/凭证/独立postgres配置。
- 真实geo_python一次计算391，结果保存且模型正确回复；后段聚合服务HTTP502原始SDK也复现，未将其宣称为通过。旧版本漏存的会话内容不伪造补齐，运行目录stdout/script/manifest保留。

# 2026-10-08：TUI无回复回归修复

- 已复现两条Token跨Context失败与SDK未及时关闭，最小本地修复后27项API/TUI通过，后端普通异常5项consumer通过。
- 增加SDK关闭spy和异常前无回复/下一输入覆盖，37项针对性回归通过；ruff通过。
- 实际用户目录探测揭示更早的触发源：认知PromptHook把ApiRetryEvent当正文；首轮失败后新的异常收尾使下一条输入正常，退出没有Context或后台Task异常。
- 新增真实Hook链回归已先失败；Hook兼容层及取消结算已落盘，相关回归与真实目录重新验证进行中。较早全量测试因新根因发现而停止，不作为最终验证。
- 包含实际Hook的最终相关回归108 passed；ruff通过。本地模型服务当前可返回正常正文。
- 源码后端在用户目录同一句制图请求5.64秒开始正文；stop后下一输入正常、exit0、无Context或unhandled Task异常。wheel五个修复模块与源码字节一致，内置技能在包内。
- 已更新D:/Python312全局wheel；移除PYTHONPATH后从用户目录核对所有五模块site-packages路径及SHA256匹配，geoh version/dry-run通过。安装版同请求8.88秒首回复，stop/新输入/exit0通过，主模型max及Default权限保持原有。
- 此次真实探测在首段正文后停止，未完成业务地图重绘；只验证用户报告的首回复和终端生命周期。最终全量回归仍在执行。
- 最终全量695 passed（540.26秒）；ruff和diff检查通过，独立复核费用边界关闭。全局安装及真实目录验证完成，本轮修复与使用说明提交，保留用户原有.env.example修改。

# 2026-10-08：业务制图与模型准确性诊断

- 只读核对用户实际会话、脚本、manifest、地图、工程及两份审图报告；独立探针验证字段类型、栅格范围、equal aspect、计算投影、Windows目录发布、无效工具参数和审查schema。
- 确认QGIS doctor成功；Python字符串/浮点运算和Affine属性错误、范围顺序错误、输出约束误用均有保存脚本；timeout是280秒参数超过120秒上限，被执行前拒绝。
- 发布句柄占用候选可复现，实际占用者未知；审图ValidationError缺字段细节，第二次是缓存。纯合成图同配置/schema请求通过，未重发用户业务图。
- 现有审图/缓存19项及API/预算6项通过；未修改生产源码、全局配置、用户数据或重新制图，未重新安装软件或运行全量业务验收。
- 完成diagnostics/2026-10-08-map-run.md，按用户当前分析请求交付诊断和优先级建议；相关长期未完成事项同步TODO，保留用户原有.env.example修改。
- 追加核对QGIS工程inspect并用实际运行时复现无效CRS；独立TEMP副本单变量补ProjectionsEnabled使项目/地图框CRS及比例尺恢复，定位手写QGS配置缺字段。未修改用户原工程或产品代码；结论补入报告。

# 2026-10-08：实施阶段1

- 用户授权依次实施四项，主线修QGS/读回/接口，独立子任务修目录发布、审图诊断与provider参数；保留用户.env.example修改。
- 真实读回先红后绿，73项工程/配置相关通过；目录发布/分析28项、审图60项、API/Python/恢复82项分别通过。整合348 passed，ruff通过，尚未因此宣称四阶段完成。
- 独立审查已关闭已有空目录删除及guard位置字段问题；追加发布中断完整stage保留边界修复，完成后再提交阶段1并进入2。

- 阶段1完成：348项整合回归、32项发布/空间分析专项通过；独立评审关闭所有问题，包括发布等待超时保留完整产物的边界。进入阶段2。

- 阶段2验证：58项旧静态/HTML兼容、132项工具契约、7项默认QGIS桥接和18项真实模板回归通过；中文、范围、用户编辑注记、hillshade、比例尺及保存重开PNG摘要匹配已验证。QGIS独立环境3.44.7；渲染成功仍不代表视觉/分析正确。

- 阶段2最终19项模板回归通过：隔离字体注册进程删除原字体后仍由工程资源恢复并同SHA导出；用户移动已有图例到主图保留位置并如实报告bounds issues，export不删或重排用户元素。

- 2026-10-09阶段3完成：52项预检/工具边界、85项制图集成、96项广义回归（含真实QGIS）、13项配置通过；独立审查关闭索引selector、实际渲染后端、single范围及HTML工程CRS问题，最终27项portable工程重绘与25项相关回归通过。ruff/diff检查通过，进入阶段4。

- 2026-10-09阶段4完成：持久UUID、当前请求预算、清单输出统计/方法、分阶段回执和交付摘要已接入。预检与QGIS worker使用同一冻结GPKG/GeoTIFF；旧图审查原位更新，不伪造成图版本；同图布局/视觉报告变更会失效。独立评审发现的问题全部闭环，最终核心82项、兼容97项通过。
- VRT兼容与交付状态专项31项通过；本地VRT依赖先校验并物化GTiff，保持原支持。首轮全量914通过，1条旧视觉单独通过断言已按新数据/布局/视觉合同更新；最终全量正在执行。
- 实际西昌业务案例完成八层QGIS成图、真实视觉服务和工程交付：服务约8秒返回issues，正确保留draft，不以像素替代审查；原数据及既有分析文件SHA保持不变。新增验证产物在独立visualizations工作区，不覆盖用户旧地图。
- 两份授权项目技能与使用/架构文档已更新；wheel包含六份技能且源码核对一致。全局D:/Python312已安装当前修复版，跨目录验证尚在执行；主模型、凭证、全局配置及用户.env.example保持不变。
- 最终全量918 passed（598.27秒）；ruff和diff检查通过。安装版66个Python源码模块及六份技能逐项与工作区字节匹配；用户test目录geoh version/dry-run/真实run --print正常回复并exit0，没有创建.openharness。安装版QGIS doctor succeeded，持久回执重读保留data/layout/packaged/delivered passed、visual issues、quality draft。
- 四项实施及文档、独立审查、全局安装全部完成。现存外部限制：用户原postgres MCP配置引用的postgres_mcp_server未安装，真实启动仍提示连接失败；未修改该配置或安装未知依赖。空间分析/制图验证不依赖此服务。

# 2026-10-09：PostgreSQL MCP 遗留启动故障

- 用户继续指令后复现缺失模块；核对PG参数与原生DSN一致，原生只读查询及PostGIS扩展检查通过。
- 在独立Python3.12环境安装官方postgres-mcp0.3.0。首次解析到MCP2.3.0实际FastMCP导入失败，限定mcp>=1.25,<2后使用1.30.0，真实入口正常。
- 本机全局配置保留GeoMCP、模型、原连接信息，只切换postgres入口与restricted参数，增加引用既有DSN的DATABASE_URI和5秒连接超时，保存原配置备份。
- 诊断URI脱敏三处及重叠凭证先红后绿；最终88项相关回归、ruff通过，独立审查问题闭环。MCP真实15.53秒握手、9工具发现及只读SQL通过。
- 全局wheel已更新，66个模块与源码一致；用户test目录实际geoh run --print中两个服务均握手，正常模型回复并exit0，dry-run通过，无.openharness。高级数据库扩展工具未逐项验收。
- 诊断报告：diagnostics/2026-10-09-postgres-mcp.md；README与使用指南已移除错误入口并说明隔离安装及MCP版本兼容要求，原有.env.example保持不动。

# 2026-10-09：按序修复连续制图错误

- 阶段1提交065d263：日期/null统一、证明驱动生成id后排除、业务字段/几何变化仍拒绝。158项相关回归及混合日期/时区补充通过，实际四份原生图层核验通过且摘要不变。
- 阶段2提交31a8f07：可核验derivation筛选/字段选择/新增注记；119项相关回归及9项子集专项通过。实际避难点23→3视图核验成功，统计保持23。
- 阶段3提交46557af：真实字体测量换行、比例尺占用、布局预检；原始两份失败布局原参数复验通过。后续整合修复了小底栏比例尺高度和ruff空行问题；完整86项QGIS/图层/预算/版式相关回归通过。
- 阶段4：显式桥接来源、禁止继承上次地图事实、自动识别相邻原生清单、同输入确定性失败去重及变化后恢复。152项相关回归和59项专项通过，ruff/diff检查通过。使用指南、架构及两份已授权项目技能已更新。
- 真实会话第65条请求用原数据/时间分类生成地图和工程成功，三份清单参与核验，原输出摘要不变；说明栏改用工具生成事实，纠正计量32648/显示32647。未调用视觉审查，质量为draft。新工程在用户test/exports/20261009-fire-regression。
- 全量pytest运行中，独立只读review子任务已恢复运行；等待结论后构建安装全局wheel并跨目录验证。用户.env.example与主模型/凭证/原数据保持不动。

- 独立只读评审完成；ID证明、混合注记、日期颜色字典及mixed scalar、Windows同状态缓存、保存工程/栅格外部依赖问题均已关闭，无剩余Critical/Important/Minor。最终相关180 passed，ruff/diff通过；上一轮完整943 passed，最后版本全量正在执行。
- wheel已构建，包含全部六份项目技能；开始替换全局安装，随后核对源码与安装包、跨目录CLI与真实派生图层预检。

- 全局D:/Python312已安装最终wheel；69个Python模块和6份技能逐项字节一致，PATH解析geoh为D:/Python312/Scripts/geoh.exe。用户目录version/dry-run正常，test目录实际run --print双MCP连接、主模型max正常回复并exit0，未创建.openharness。
- 安装版QGIS doctor成功（3.44.7、440算法）；真实23→3派生图层预检通过，统计保持23且源摘要不变。安装版读回新增回归QGIS工程：10层有效、EPSG32647有效、布局bounds有效，工程与预览摘要不变。真实视觉未调用，回归地图仍为draft。
- 最后版本全量956 passed（553.86秒），ruff/diff与独立评审通过；四阶段修复、文档、安装和跨目录验证均完成。回归产物在test/exports/20261009-fire-regression；已运行会话需退出重启以加载新安装。原有.env.example修改仍独立保留。

# 2026-10-09：test01 工具错误与 PNG/QGS 差异（仅诊断）

- 核对最新18:47会话与交付，独立只读子任务统计55调用/13错误；主Agent直接用QGIS读回并重导出原QGS，确认跨渲染引擎后补工程造成差异，原文件摘要不变。
- 独立native图例探针复现真实尺寸不足，MicrosoftYaHei有效；A3+78mm侧栏同数据预检通过。未因字体警告扩大权限或修改原生代码。
- 完成报告diagnostics/2026-10-09-test01-errors-and-layout.md，提出原生版式/工程同源、残缺参数恢复、窄诊断入口、候选版本选择与统计口径四组改进。仅更新诊断/规划文档，没有产品修复或安装变更。

### 四项改进阶段1
- 原生 QGIS 测量图例必需尺寸，有限地加宽侧栏/缩短详图，保留纸张、字号与全部类别；失败返回内容尺寸/有效参数/字体证据。已保存工程导出不自动排版。
- 图例字号独立于底栏，常规字号能够进入原生布局；原生包 saved_layout，旧图片补工程 reference，工具/README/清单明确不同源。
- test01 原始12图层冻结请求以修复代码执行预检/新建/重新导出成功：侧栏58→63.998mm、详图比例0.4→0.3657；重导PNG摘要一致。证据：C:/Users/Administrator/AppData/Local/Temp/geoharness-test01-fixed-layout-t4oxuwdn/verification.json。原数据和旧交付未改。
- 相关原有55项通过，新增多图层适配回归通过；ruff/CLI dry-run通过。初始测试12个带重复分组的分类图层确实超过A4可用高度，不能作为应当适配成功的输入；缩小到可容纳的10层案例并保留关闭auto-fit的失败诊断检验。
- 阶段1独立评审补充：现存布局参数按实际元素读取，创建参数单列；页单位转换为mm，文字保留value/unit，仅Points提供pt，避免用户在QGIS换单位后返回错误尺寸。真实12x8inch、6mm图例的保存/重新导出回归通过；评审发现的边界已关闭。
