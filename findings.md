# 初步核查

## 已核实的实现
- `src/geoharness/runtime.py` 直接组装 OpenHarness RuntimeBundle、QueryEngine、MCP manager、工具注册表、DataCatalog 与 CascadeManager。
- `src/geoharness/tools/__init__.py` 注册四个原生工具：geo_db_data、geo_vector_data、geo_raster_data、geo_cartography；另保留五个上游辅助工具，并动态注册 MCP 工具。
- `src/geoharness/tools/geo_raster_data.py` 公开 list、inspect、stats；读取栅格元数据与波段统计，不提供语义图片理解。
- 全仓 src/tests 的 image、vision、multimodal、attachment、base64、image_url 搜索仅命中制图输出格式；未找到项目自身的图片输入、视觉模型路由或图片理解工具。
- 系统提示词的主线是 L1/L2/L3 认知级联；空间统计计算依赖外部 MCP，制图原生实现。
- `skills/spatial-analysis/SKILL.md` 引用 proximity-analysis、overlay-analysis、zonal-statistics，文件清单中没有对应技能目录。
- README 仍写三个认知技能；实际还存在 spatial-analysis 和 cartography。文档存在与代码不同步的迹象。

## 初步判断
当前项目较侧重数据读取、外部工具编排和地图导出。图片理解缺少项目层面的实现入口；是否需要遥感专业处理、地图读图或一般照片理解，须由用户确认。

## 未核实
- 外部 geo-mcp-server 的实际可用工具和当前安装状态。
- 上游 OpenHarness 当前版本是否支持多模态消息及相关 TUI 入口。
- 用户模型服务、成本要求与典型输入数据。
- 现有测试与 CLI 在当前环境中的运行结果。

## 用户决定
- 选择 A：地图、专题图、统计图理解；主要检查自己生成的地图是否正确。
- 选择 A：检查发现问题后自动修正并重绘。
- 首期选择 A：多图层叠加与影响范围分析；B、C 后续再做。
- 分析执行选择 A：GIS 原生工具 + 自主编写和运行 Python。
- 用户同意同步更新项目自带 spatial-analysis 与 cartography 两份技能说明。
- 用户指定本地聚合 API：http://localhost:20128/v1；模型 ID：cbcn/deepseek-v4.1-flash(max)。用户链接尾部中文逗号视为标点。

## 聚合服务实时验证
- GET /v1/models 成功，返回 35 个模型；cbcn/deepseek-v4.1-flash 的 capabilities.vision 和 tools 均为 true。
- 列表未包含带 (max) 的完整 ID；随后 POST /v1/chat/completions 用用户完整 ID 和合成红色矩形 PNG 成功，回答 Red，finish_reason=stop。
- 该请求不含真实数据，未提供用户密钥；仅证明当前请求支持图片，并不证明复杂地图检查准确率。

## 成图检查链路
- `geo_cartography._render_blocking` 与 `_render_layers_blocking` 渲染后返回文本路径、文件大小、要素/图层数量及 metadata；当前工具内部没有成图视觉检查或修正重绘环节。
- `renderer.py` 提供字体选择、图例、近似比例尺、指北针和图层 CRS 转换；这些是需要核验的现有制图行为。
- 已阅读的 `test_cartography.py` 静态输出测试主要验证文件存在、格式标识和大小；中文标题测试以文件大小断言，不能证明文字可读。

## 待确认建议
- 用渲染配置、源数据和分析结果进行程序核验，用视觉模型查看最终成图检查可读性和呈现问题。
- 自动修正已确定开启；最大轮次和分析口径变更边界仍是建议。

## 本地上游支持
- 当前 Python 安装路径为 `D:/Python312/python.exe`，OpenHarness 安装版本为 0.1.9，与 uv.lock 一致。
- 已读取上游 `api/openai_client.py`：支持 `ImageBlock`，可将文本与图片转换为 OpenAI-compatible 的 image_url 消息；无需为图片消息能力修改上游源码。
- 当前 GeoConfig 只有 model、api_key、base_url 一组模型配置；没有独立视觉模型配置。尚未核实用户实际使用的服务是否支持图片输入。

## 首期分析执行能力
- `geo_vector_data` 只公开 list/inspect/load，load 返回数量、字段、CRS、范围，没有公开叠加、缓冲、距离、空间连接或统计结果落盘接口。
- `FileLoader.query_bbox` 有内部 intersects 筛选，但未形成首期影响范围分析工具链。
- 当前项目白名单不包含脚本执行或文件编辑工具；没有项目自身的 Python 分析执行入口。
- 本地上游存在 bash_tool.py，但这不等于当前 GeoHarness 已开放脚本执行。
- `FileLoader.load_geodataframe` 对缺失 CRS 的文件直接赋 EPSG:4326，需在分析正确性设计中处理这一未经确认的假设。
- 已阅读的空间工作流测试用字符串存在断言检查技能引用，未验证引用技能实际存在；不能据此证明工具链完整。

## 执行方式
- 用户已确认首期提供原生 GIS 运算，同时允许 Agent 根据任务生成并执行 Python 分析脚本，保留脚本、参数及输出。
- 保留现有 OpenHarness 与 CLI/TUI 入口是当前实施判断；优先重构分析执行和成图检查链路。

## 成图送入模型的接口约束
- 上游 `ToolResult.output` 和 `ToolResultBlock.content` 均为字符串，返回地图文件路径不会自动成为图片输入。
- 上游 ConversationMessage 支持 ImageBlock，但 GeoHarness 必须显式接通读取成图并调用视觉模型的链路。
- 上游有基于 vision_model_config/image_to_text 的图片转文本路径；GeoHarness 当前没有该配置或工具，因此不能假定现有默认链路已经可用。
- 视觉质检应携带用户目标、渲染参数与源数据摘要，输出可用于修正的具体问题；不能仅生成一般图片描述。

## 实现发现与校验边界
- 旧版 mapclassify 分级索引减一会丢失第一类，已修正；多图层必须统一 CRS，未知 CRS 不能静默假定。
- PostGIS 制图/分析必须读取全量空间数据，不能继承检查工具默认 5000 行上限；读取实际 SRID，未知/混合 SRID 拒绝。
- 视觉检查只能验证图层、符号、文字和布局；数值及计数口径必须程序核验。PDF/SVG 使用同参数 PNG 预览，HTML 无实际截图仍未验证。
- VRT 可以间接读取其他文件；可信脚本的常规 GDAL 入口也必须递归检查依赖，单独检查入口路径不足。
- 最终验收只使用合成数据和本地已授权聚合服务，未发送真实业务数据；真实 PostGIS、大型数据与质检准确率仍属后续验收。

## 用户实际运行诊断（2026-10-07）
- example/.geoharness/runs 共26份manifest，23次exit_code0、3次exit_code1；另有一次geo_python空参数在Pydantic校验阶段失败，无run目录。
- list_data将SOURCE_CWD当环境变量，但旧runner只注入脚本全局；fire_analysis试图向RUN_DIR外创建analysis目录，被正确拒绝；fire_map_v1的DEM读取窗口越界，属于生成脚本错误。
- example/.openharness只有autopilot/plugins空子目录，没有文件；需修复上游初始化副作用的GeoHarness适配层，不能修改上游依赖。
- 当前全局cartography.outputs_dir=~/.geoharness/exports；脚本产物保存在工作区内部runs，缺少独立交付接口。两者造成交付目录偏离用户可见工作区。
- 同一会话包含fire_map_v1..v7及fire_map_final，部分相同图片重复送审；Python审查工具每次调用独立，不执行config.max_repairs跨调用限制。需共享任务预算和内容缓存，而不是仅增加Prompt建议。
- 用户确认可编辑工程主要用QGIS；工程必须携带可移动的数据副本、图层样式和布局，而非只交付PNG或外部数据路径。
- 实际会话有16次地图审查，其中9次因token预算耗尽而未验证；7次报告问题。同图重复审查与Python版本缺少共享预算，导致8个火情绘图版本。
- 本机QGIS 3.44.7实际读取、图层有效性、打印布局和PNG导出验证通过。Windows Qt offscreen字体库为空，验证脚本加载本机字体后正常；QGIS按范围比例恢复地图框高度，生成XML需匹配纸张地图框比例以防超页。
- 未改图片的失败审查也缓存，整个请求默认4次/120秒、每图最多3版本；通过或未验证均停止自动完善。非max视觉模型的合成空图探测约7秒返回有效结论，但复杂合成地图4096token仍可能耗尽，不能承诺全部通过。
- 已有两张业务地图补成QGIS工程，原PNG/PDF字节完全保留。详细Python注记和特殊符号不自动等价还原，交付说明明确此限制。

## test业务地图质量回归（2026-10-07）
- 真实脚本用覆盖主图的放大图、统一偏移的重点名称及图内图例；最后修正被预算阻断。首次失败绘图也消耗版本，审查总计时包含分析、写报告和工程生成的间隔，均会挤占有效检查。
- 非白像素48%及配色检出不能证明图层位置、分级或压盖正确；颜色可能只来自图例。工程生成时另画通用预览，分类字段/颜色未正确沿用，造成交付质量退化。
- 本机9Router 0.5.95实测：flash配thinking.disabled或reasoning_effort.low仍耗尽4096/8192推理token；独立flash(none)配thinking.disabled返回reasoning_tokens=0、完整工具报告，业务图约9秒。主模型max保持不变；其他网关不假定具有相同后缀语义。
- 布局辅助模块按实际文字边界检查独立区域、标签避让、标题/说明裁切；结构通过只证明布局几何。视觉请求独立拥有SDK配置，预算计实际调用时间，缓存绑定图片和审查配置；执行失败返还自己预留的绘图版本。
- 地图、工程预览和审查绑定SHA256；分类配色、缺省类及锁定范围同步到原生/QGIS，禁止生成工程时偷偷换简化图。QGIS基础布局可编辑，但复杂Python放大图与编号需使用完整重绘脚本或在QGIS补充，不能声称逐像素复现。

## QGIS 原生执行与工程集成（2026-10-08）
- 本机QGIS 3.44.7具有独立Python环境，默认自动发现无需写入机器路径；真实registry为440个算法。查询目录不等于全部算法已业务验收，执行限制随help返回。
- Processing算法会存在源文件修改行为，不能仅检查最终输出路径。独立审查真实复现了属性索引、投影侧文件及metadata保存模式，现执行前拒绝；extent/CRS必须显式声明，模型参数来源也检查类型。
- 主进程不导入PyQGIS；每次任务通过独立Python/JSON和文件交换，Windows Job Object用于超时及取消清理，profile和环境不继承用户插件或API凭证。
- 真实合成流程：500米buffer+clip得到2个对象；梯度DEM的内层坡度约45度；本地.model3产生1个EPSG:32647缓冲对象、宽度1000米。源数据SHA256保持不变。
- QGIS创建真实工程及同布局PNG/PDF；用户保存标题、紫色分类及布局名后的再导出保留修改，输入工程摘要不变。基础布局不代表已实现前期复杂布局与质量决策升级。
- 交付必须验证规范相对路径和最终目的目录，不能只验证源resolve后的路径；回归覆盖源路径重新进入、absolute和preview路径变体。
- Windows受管工作区测试临时目录出现间歇性WinError5目录rename失败；60次独立合成rename未复现，改系统临时目录后相关126项通过。原因尚未确定，未据此改动既有分析/工程发布逻辑。
- wheel环境验证发现将主site-packages放入worker.sys.path会使QGIS导入主NumPy2、产生GDAL ABI错误。改为importlib限定加载GeoHarness包、不改sys.path；源码/wheel两种真实子进程回归与最终安装版完整流程通过，QGIS doctor stderr为空。
- 原生资源验证：内置SVG按QGIS官方资源目录解析，现代Option和旧prop格式兼容。真实导出后活动符号及打印布局锁定样式引用包内SVG，副本摘要和其他符号属性保持一致。
- 修复图实际模型报告issues，其中水体名称和北箭头样式意见需要结合任务/实际参数判断；保留原始报告及draft，不为了样式建议无限重画。原风险GeoJSON、两个CSV及附图字节保留，原始234点风险分类未重算。
# 2026-10-08 启动与会话诊断

- 用户实际 `.geoharness/runs` 中 check_analysis_dir、tree_check、tree_check2 均有成功脚本输出；并非工具返回为空。
- 上游 QueryEngine.submit_message 只在 AssistantTurnComplete 同步 query_messages；工具结果在该事件之后追加，下一次空模型消息直接返回时，刚完成的 tool_result 丢失。真实工具循环回归先失败，修复后结果留存与继续不重执行通过。
- 连续失败会话的 output_tokens 每次约增加16,384；本机9Router模型后缀优先于 reasoning_effort，max模式低参数仍产生推理。none别名真实请求有正文且reasoning_tokens=0；64token合成算术请求真实触发一次恢复，返回333833500，合计67输出token。
- MCP错误模块不存在；本机实际 D:/Python312/python.exe -m src.server、cwd E:/Administrator/hermes/geo-mcp-server，18工具握手及CRS转换成功。
- PostGIS数据库layout2与rasterio自带layout6不兼容，pyproj使用自己layout4正常。修复按rasterio实际数据库选择，成功使用debug日志，机器环境与PostGIS数据不改。
- React ErrorEvent同时发送error与同文system transcript，前端都会渲染；本地后端只抑制紧邻重复文本，错误仍保留在历史。
- 独立评审要求补充取消恢复时费用结算、带工具的stream_options.include_usage及部分流式答案中断诊断；已修复并复核关闭，API21项和引擎/模型26项测试通过。
- 全量681项通过；最后初始化模板改为空MCP（不再生成不存在的模块入口）后，CLI/配置/会话36项通过。真实Geo MCP注册后共33工具，map-making实际读取cartography，rasterio EPSG:4326正常。
- 真实geo_python一次执行print(17*23)，成功结果391进入历史，随后模型正确回答。后续gateway请求出现HTTP502，原始SDK的1024token合成请求也复现；无正文/推理，不属于空消息丢历史缺陷，不盲目重复相同请求。
- 原全局配置的独立postgres_mcp_server未安装，保留已有配置与凭证；原生geo_db_data仍注册。全局Geo MCP改为实际入口、主模型仍max，空回复仅配置一次none恢复，配置已备份。

# 2026-10-08 TUI 首回复与上下文回收回归

- 跨任务关闭与跨 Context 继续生成器均准确复现 Token was created in a different Context；API请求状态的token原先跨yield持有，后台asyncgen finalizer任务无法reset。现每次推进/关闭时局部set/reset，yield前恢复context。
- 仅关闭上游外层生成器不能同步关闭SDK连接；真实close spy复现边界，现登记实际transport并显式关闭，观察流与外层共享一次关闭机制。
- 实际用户目录提交“根据数据文件夹制图”，4.16秒复现AttributeError: ApiRetryEvent has no attribute text。USER_PROMPT_SUBMIT认知Hook max_tokens512触发恢复，上游Hook把所有非final事件误当正文；以前测试用MagicMock Hook绕过了此路径。
- 本地Hook API代理只过滤重试状态，继续复用上游JSON解析；普通失败返回尊重block_on_failure的HookResult，取消不吞掉。Hook调用纳入engine既有finally，取消时仍结算已确认用量。
- 上游React后端只捕获取消，普通异常会丢error/line_complete并退出；前端assistant_complete本身不清busy。本地后端补普通异常唯一error及结束事件，返回输入循环。异常详情先对已配置凭证脱敏。
- 修复后实际用户目录源码首回复5.64秒、安装版首回复8.88秒，均为assistant_delta；随后停止、再输入和exit0正常，stderr无Context和Task回收异常。真实探测停止于首段正文，未据此声称完整业务制图通过。

# 2026-10-08 业务制图诊断

- verify_crs_spread真实manifest: NumPy/Pandas先计算float减string后astype，TypeError；首次map_v1_build把Affine.height/width当作存在的属性，AttributeError；map_v5_final写RUN_DIR外fire_map_out，应用guard禁止。均有具体保存脚本和日志，不等同QGIS故障。
- 对照v3与final实图：v3详图只有晕渲且矢量全不可见；final详图图层/标签恢复。v3 EXT采用(xmin,ymin,xmax,ymax)却直接传imshow，final纠正为(xmin,xmax,ymin,ymax)。当前布局passed报告scope仅layout_geometry，视觉及数据正确性均unverified。
- QGIS doctor真实记录成功3.44.7/440算法。两个视觉审查条目中第二个review_cached=true，复用首次8.156秒失败，并非两个独立网络请求。具体ValidationError字段需进一步取证。
- 实际字段先转换后最大几何/属性差3.98e-13度；只证明内部一致。保持equal aspect只修正范围顺序即可恢复详图点，模型将问题归因于set_aspect不成立。
- 原生impact metric_crs=EPSG:32648；最终显示投影32647却沿用distance_m并把计算方法写为32647。234点两个投影距离最大差10.23m，2km受影响分类变化0；事实是出处说明错误，不据此判定47个受影响结果错误。
- Windows子文件被进程持有可产生相同WinError5目录rename，释放后成功；普通关闭写入rename100/100、原生impact30/30及工程30/30通过。实际失败句柄持有者未记录，不能确定进程或永久权限问题。
- 真实解析器把截断JSON和显式{}都变成空工具输入；实际两次原始参数未保留，无法区分。timeout错误实际请求280>允许120，脚本未启动；不同于运行满120秒的超时。
- 视觉错误发生于本地MapReview校验，未保存字段错误；一次同配置纯合成审图9.422秒通过。只能证明管线可用，不能证明复杂地图识别能力或原业务具体错误字段。
- 会话79次工具、34次geo_python、一次geo_qgis doctor及打包后一次geo_qgis_project inspect，没有该工具创建/导出调用；QGIS虽已集成，本次实际制图仍是模型自由编写Matplotlib。报告提出先修接口/发布/诊断，再补QGIS复杂布局主路径；正式模型基准保持用户延后的优先级。
- 追加真实QGIS工程核验：10图层valid，但项目/地图框CRS invalid、单位unknown、比例尺约1:19.98亿。直接原因是project.py:700-703手写properties缺SpatialRefSys/ProjectionsEnabled；临时副本只补type=int/value=1，读回有效EPSG32647、单位米及比例尺约1:16.108万。原生PyQGIS工程自带该项。原业务工程未改，读回验收缺口纳入优先修复建议。

# 2026-10-08 四阶段实施：阶段1

- 原生QGIS读回回归先失败于crs为空，修复ProjectionsEnabled后通过；检查器现验证项目/地图框坐标与范围/比例尺，新包装工程重新打开验收。
- 新project_output_dir要求未创建目录，旧output_path输入语义保留；不删除已有空目录，也不通过删目的目录规避发布错误。
- 发布与审查/参数改动已整合；相关348项通过，另统一PRE_TOOL_USE实际零副作用guard回归通过。独立审查发现发布重试等候期间deadline/cancel会触发通用清理删除完整stage，正在保留该中断的发布诊断和产物，维持TimeoutError语义。
- 受管工作区pytest临时目录ACL错误改为明确系统TEMP子目录后测试正常；未据此改变产品权限。一次文档patch匹配失败未写入，使用实际段落重新应用成功。

- QGIS模板保存重开会归一化比例尺尺寸；最终PNG改为由已保存并读回的布局导出。applyDefaultSize需显式Kilometers，避免默认米配km标签；NULL category value不能被exclude_none序列化丢掉。仅模板自身区域互压受结构检查约束，用户地图内新增注记保留。

# 2026-10-09 PostgreSQL MCP 启动入口

- 缺失模块可直接复现；现有PG参数与原生PostGIS DSN五项一致，数据库连接本身正常。
- 官方postgres-mcp0.3.0真实入口为console script，启动必须提供DATABASE_URI。发布包未设MCP上限，首次安装选择2.3.0后FastMCP导入失败；隔离环境限制mcp>=1.25,<2得到1.30.0并正常启动。
- Windows入口已采用Selector事件循环支持psycopg异步连接；服务另提示Unix信号处理不支持，但其后真实握手、9工具发现与只读SQL均成功。
- MCP错误脱敏需要先替换长的完整URI，否则旧PGPASSWORD会提前改变连接串使整体匹配失效；两种环境变量插入顺序已回归。
- 本机全局配置已最小切换并备份，主模型、原凭证与Geo MCP保持不变；安装版实际CLI双服务握手和正常回复通过。索引/健康/统计高级扩展能力未逐项验收。

# 2026-10-09 连续制图错误的修复

- 真实 session-3c23e6a7ba97 有40个assistant回合、59次工具调用、16次失败。原始分析文件未变：日期被读取为Timestamp导致序列化失败；shelter输出的GeoJSON Feature.id被GDAL加入属性，导致错误的缓存变化判断。不能通过删除真实业务id修复。
- values.py统一日期/null分类；facts.py从已核验字节读取对照数据，证明驱动编号后才忽略。影响评估与避难距离的结果/参考四份真实图层现均通过核验，摘要不变，业务字段/几何修改仍被拒绝。
- derivation显式声明原输出行位置、保留字段及新注记，系统生成副本并从原输出重建对照。真实23处避难场所的最近3处子集被核验，统计保持23，制图记录3/23；覆盖源字段或改动几何被拒绝。
- 原始两份QGIS布局的footer_text实际宽384.75mm，可用约354mm。现用QGIS实际字体测宽换行，同时考虑比例尺高度；两份原参数布局预检通过，不生成图片。过长文本仍被拒绝，已保存用户工程不自动改写。
- 直接QGIS入口先前会继承上一地图map_analysis_facts；现绑定本次来源，桥接副本显式携带原来源。相邻原生manifest可识别，普通专题地图无分析时为not_provided。真实计量CRS32648与显示32647分开，手写计量CRS冲突被拒绝。
- 同参数同文件状态的明确ValueError校验失败缓存，不重执行/耗版本；参数、读取根、关联分析输出或伴随文件变化失效。PermissionError/超时不缓存，无稳定身份的目录别名或VRT不缓存。缓存为可序列化对象，不替代输出摘要。
- 原session第65条的真实多图层/时间分类/布局已成功通过geo_cartography生成QGIS工程与同布局PNG，三份清单核验，源输出摘要不变；说明改为来源备注，统计/CRS由清单生成。回归产物位于用户工作目录exports/20261009-fire-regression，视觉未调用，保留draft。

- 最终独立评审复现并关闭：数字Feature.id作为FID并未产生id属性时不得忽略新增id；混合注记写入GPKG后统一文本/null；日期颜色字典键同步规范化且重复键拒绝；混合日期中的数字/布尔分类保留原类型。
- Windows同大小、恢复mtime后原stat-only缓存仍可能误命中，现追加有64MiB总读取预算的SHA256。已有工程依赖未枚举、栅格worldfile改变范围但主文件不变，故这两类请求不缓存失败；文件锁/超时仍正常重试。
- 完整组943 passed对应边界修复前版本；最终边界修复后的180项相关回归和ruff/diff检查通过，独立只读评审无未解决问题。最后版本全量回归正在执行，不将前一轮结果作为最终完整验证。
- 最终版本全量956 passed（553.86秒）；189条警告为依赖弃用及缺CRS测试夹具提示。安装版69个模块及6份技能与当前源码一致，实际双MCP启动和模型回复成功，QGIS3.44.7/440算法可用；真实派生预检及10层工程读回成功，未创建.openharness。

# 2026-10-09：test01 最新错误与工程布局差异诊断

- 最新session-bfde4744da4a：55次调用、13条错误，8真实执行失败/5参数拒绝。集中geo_python5/18、geo_cartography4/6、geo_qgis_project3/3、geo_analysis1/5；原生分析实际4/4成功。四次残缺参数均finish_reason=length，当前空回复恢复未覆盖非空残缺调用。
- 三次原生QGIS真正异常为图例超预留框，并非FT_New_Face警告；模型调整generic legend_font_size未影响显式qgis_layout内固定9pt。独立原生预检测得需63.50×87.10mm，预留58×82.6；仅字号7仍高不足，仅A3仍宽不足，A3+侧栏78通过。字体MicrosoftYaHei加载有效。
- PNG由自定义Python绘制，project action走旧基础QGS生成器，忽略完整qgis_layout；工程只有一个地图框和13个OGR层，缺详图、两栅格及底栏。preview复制PNG所以摘要相同，package只核验完整性，不能证明布局等价。真实QGIS重开导出已复现，原图/工程/preview摘要不变。
- v2硬布局通过，v3增加长名单后退化，预算拒绝v4但最终交付v3。时次面积相加48.1105km²与去重覆盖33.1496km²不同，Python手写说明未受原生事实绑定。
- 诊断报告已保存，未实施产品改动。手工探针先因缺analysis窄读取根、随后缺discover_qgis的cwd参数失败，修正探针后完成复验，不将探针错误算作用户工具失败。

## 2026-10-09 四项实施结果

- 原生图例实测自适配使原12层A4请求预检、新建及重导出通过，9pt和类别保留；保存后的工程为导出源，旧Python补工程明确reference。
- 截断且JSON不完整的调用最多补全一次；完整调用、原ID和既有工具结果保留，不追加无界重试或改主模型。
- Python执行契约返回Path/list类型，只授予合法最近QGIS任务五项诊断文件精确只读；链接、目录、凭证、越界写入继续拒绝。
- 真实预测蔓延数据面积sum48.110519km²、union33.149636km²。原生area及地图事实复算可避免将累计时次相加称为覆盖。实际三图层保存QGZ重开导出PNG与首次PNG同摘要，未视觉审查，不宣称业务图完全正确。
- 最新问题版本不再必然作为默认交付：优先摘要未变化且全检查passed候选，再选布局passed草稿及其工程/真实报告；用户明确selected仍可保留指定问题图，未登记另存图保留自身。

## test02 前置错误与剩余质量（2026-10-09）

- risk_analysis在EPSG32648几何上直接调用rasterio.mask，实际报Input shapes do not overlap raster；模型事后改32647。需核实发现/inspect上下文及实际栅格头，再验证统一几何到栅格CRS的正确处理，而非强制所有分析使用栅格CRS。
- prep_layers读取当前新RUN_DIR的上一轮蔓延并集文件，实际不存在；buffer输入路径的20261015与真实20261009任务目录不同，属于产物定位误用。
- read_receipt读取全局tool_artifacts被拒；read_project_json枚举QGIS目录被拒；measure_png读取实际QGISpreview.png也被拒。前两项需要正确诊断入口，最后一项说明现有精确五文件只读不足以支持实际图片测量。
- 三版均visual issues。第三版仍有编号/避难点重叠、主图大块空白和底栏文字过密；QGIS布局几何passed未证明地图标签正确。视觉patch给出width/height英寸及point_size等旧Matplotlib字段，需核实原生QGIS适用建议与模型修正链。
- 完整会话更正：模型在risk_analysis前已经inspect两张栅格并得到EPSG32647/30m/2202×2419，仍将32648 raw Shapely用于mask。因此不是仅漏查头信息，必须持续保留实际元信息并提供带CRS几何的操作入口。
- 数据发现/缓存/动态模型上下文、GEO_DATA/PREVIOUS_OUTPUTS、mask_raster与QGIS精确preview/project.json只读已经实现。真实test02两栅格均用32648几何裁出422×349，数据与原PNG摘要未变；证据临时geoharness-test02-prevention-y5w7neg2/verification.json。
- 新QGIS后端审图真实返回issues、毫米/pt qgis_patch及detail_map；原第三版缺陷仍存在，不把接口修复称为旧图修好。证据临时geoharness-test02-review-om64esqo/verification.json。
- PyQGIS saved-project重新inspect保持OverPoint/毫米偏移/priority10/无标签障碍factor3；真实labelingResults验证around_point忽略X/Y、使用dist，OverPoint反之，已补契约。
- 独立审查复现三项P2：patch缺图层类型/几何验证、共享预算UUID覆盖真实地图历史、metadata消费时新stat绑定旧CRS。三项均已修正并补回归；限额过大上下文亦已修复。后续独立闭环复核遇账户usage limit未完成，主Agent完成代码检查和回归，不将二次独立复核宣称通过。
- 首轮全量1044 passed/1 failed，唯一失败是旧测试以versions子串误拒新remaining_render_versions提示。断言改为解析证据，确认无完整版本列表、首轮history为空和剩余额度为0；最新相关71 passed，另过滤已核验点放置模式下无效distance/offset字段。最终源码已重新启动全量回归。
- 最后真实审图API返回HTTP502/InternalServerError，正确标记provider阶段unverified，不是模型JSON/本地ValidationError。旧PNG和receipt摘要均未变；证据geoharness-test02-review-wy3ob39o/verification.json。未对同图同配置机械重试，不把此前API成功当作当前网关健康。
- 最终1051项全量通过，安装版73模块/6技能一致，实际预算已同步五版/五次审图。test02的独立QGIS环境和实际数据裁剪/精确图片读取通过；网关模型列表同样502，主模型业务端到端尚未验证，原图未重绘。
# 2026-10-11：test04 新结果审查（诊断完成）

- 后续会话 session-017998194289 已保存，包含126条消息/62个assistant回合/76次工具调用/11条失败；初始两份快照只覆盖56次调用/8条失败，不相加重复统计。最新geo_python35/4、todo_write8/5、geo_qgis_project11/2失败；原生geo_analysis2/2成功。7次审图工具执行无工具错误，其中第6次返回unverified（invalid JSON），其余issues，仍不能称视觉通过。
- 后续新增Python错误为对 iterrows() 返回的 pandas.Series 调用to_crs；应在GeoDataFrame/GeoSeries层面投影。原4个失败运行均有真实manifest，35个运行中31成功、4失败，未运行超时。
- 流/API离线探针（0次网络请求）确认：(max)->(none)->(max)->(none)->(max)可复现，已完成工具只执行2次；持续接收reasoning chunks而无可见事件不触发逐读超时；及时收到SSE error.message='stream stall timeout'足以产生原样Network error文案。实际测试的远端错误来源尚无网关日志证明。
- 普通“继续”会保留工具结果但清空预算/失败缓存、换request_id、把最新任务目标设为“继续”；/continue保留原预算/目标/待处理检测。实际三次用户输入为“根据数据文件夹制图”“继续”“继续”，同一job累计7版与每请求5版并不矛盾，但恢复入口不统一会拉长任务。
- 独立本机QGIS3.44.7读回7个工程、14个地图框，extent与同比扩展公式/原result完全一致，中心不偏移；35份原工程证据文件SHA256前后一致。第4版审图说火场偏右下，但实际详图归一化位置(0.500309,0.499537)。审图具体建议并非都有事实依据。
- 最后7版黑边2.8mm是实际渲染参数；原7份request中的火场边宽依次0.7/1.2/2.0/1.6/2.5/2.8/2.8mm。多轮审图建议增粗边界，后续真的引入过粗描边问题；不能照单接受视觉patch。
- 图例也有真实语义冲突：6处“主要避难场所”与23处容量层重复同一几何，其中15000/20000/27500容量原为中档蓝色，被主要身份的深蓝大点覆盖后与>30000同色。工程节点各自正确并不等于叠加地图语义正确，应保持容量编码、单独表达主要身份。
- 最新最终说明把EPSG:32647写成CGCS2000；本机pyproj查得实际为WGS 84 / UTM zone 47N。需要由工程事实生成CRS说明。底栏原生读回7行，审图称3行也不准确。报告中用7版工程与完整会话作为最新证据，不将早期4版称固定最终。
- 本轮完整诊断及待实施四阶段方案已保存在 diagnostics/2026-10-11-test04-errors-and-workflow.md；优先请求恢复/续跑、冻结QGIS最终布局与范围、事实约束的视觉修正、工具/交付契约。原布局与图像共源导出保留，受影响的源码/安装包关键三模块已核对一致，不是只运行旧包造成。
- 当前两个保存会话中，用户粘贴的后续 geo_python 调用实际成功；推理预算耗尽和流中断发生在模型请求阶段，不能据此断定工具输出为空。
- 当前源码默认单次查询40回合，与最多5个成功地图版本是两个独立限制。普通“继续”和 `/continue` 的入口不同，正在核对预算、待处理结果与原任务上下文的保留行为。
- v2 实际布局把侧栏54mm扩为77.523875mm、底栏18mm扩为28.786364mm；实际主图比例约0.558231、详图约0.908802。范围由QGIS围绕请求中心扩展以适配实际地图框，尚未发现QGIS交换x/y或自行移动中心的证据。
- 已直接查看 v2 和后续最新 preview.png：主图火场主体占比很小，点符号与描边压住局部地物，详图存在边缘裁切与符号拥挤；底栏包含过量统计和来源技术文字。结构性布局通过不等于这些视觉问题解决。
- 主模型的空回复恢复仅作用于单次请求；下一次工具后续请求仍从配置的 `(max)` 开始，因此可以重复耗尽16384输出预算。实际流中断来自何处还需最小复现，不能仅凭界面的 Network error 判断本机网络故障。
- 本轮只读原始 test04 数据、脚本、地图及回执，未修改产品实现或配置；新证据与方案保存在工作区诊断文档。

# 2026-10-11：test04 实施中的确认

- 真实SDK离线工具循环重现备用模式每回合失效；将恢复状态绑定到query，并在每次anext/close时临时作用后，后续请求可沿用成功模式。只有已确认reasoning-only预算耗尽、配置备用且备用完整成功才激活。
- 精确续跑词在pending结果存在时必须提前路由到continue_pending；否则追加用户文字会改变上游pending检测和原goal。明确新指令保留新任务语义，40回合只限制一个批次。
- JSON保存后共享budget别名字典会失去对象身份，原预算合并逻辑按id去重会误累加；检查点需保存显式entry/alias关系并恢复共享引用。会话路径也不能依赖launcher先设置上游env，headless runtime同样应使用Geo专属目录。
- SDK在yield前已拒绝SSE error/非法JSON，仅计数已解析chunk会漏掉关键错误包；诊断在本地代理原始SSE解码器后可记录包数量与时刻，而不持久化正文。

# 2026-10-13：最新67次调用的剩余根因

- 顶层7次失败为geo_python 5/27和geo_qgis_project 2/9；类型假设、辅助函数返回格式、几何接口混淆、非法JSON、缺字段、不可读预检路径、解包赋值错误分别确认。最新栅格裁剪本身成功，不是再次CRS失败。
- 四次内部自动修图均被打包的_geoharness_fid误识别为业务变化；业务内容、几何和CRS未变，仅在内存去掉已确认内部字段后四次预检全通过。通用stop隐藏了具体原因。
- 实际max预算耗尽为两次；第一回退后非法JSON使query恢复模式未建立。零网络SDK回放确认该混合失败条件；普通续跑目标保持，不属于旧续跑清空缺陷。
- 分类脚本将距当前+预测合并范围50m内4个居民点命名为火场内；实际当前覆盖0、最终预测覆盖2、另2在预测外21.405/47.976m。原生距离正确，分类命名语义错误。
- 详图纸面比例尺比主图更小、图例挤占详图、编号名单被来源过滤、后两版删除关键标注；问题ID/精确ROI绑定和跨版本source_index比较阻碍有效策略。PNG/工程一致不意味着业务地图正确。
- 23.82分钟案例首create在17.77分钟开始，之前47次调用含19次Python；工具活动并集5.77分钟，其余为模型/服务等待及调度。具体证据与四步改进在diagnostics/2026-10-13-test04-remaining-errors-and-plan.md，本轮未修改产品。

## 后续实施证据

- 阶段1已实现并提交a357f04：真实QGIS不再把内部主键写成业务属性，用户fid和同名字段保留，业务修改仍拒绝；候选按稳定源身份比较，未知身份不伪造覆盖。地图版数、修正/审查次数及累积时长不限，旧配置仅兼容。
- 阶段2新增原生参考属性筛选、reference_relation和可选距离分级，并将实际参考集合/时刻筛选与结果清单绑定；制图事实独立从落盘几何复算关系与距离。向量inspect返回实际dtype和空值，原生派生输出登记schema。当前环境字符串dtype可为str而非object，测试不把一种Pandas版本的dtype名字写死。
- QGIS3.44官方API确认布局属性表可绑定向量层、字段及筛选，布局多框支持内容跨框；后续编号名单优先复用这些原生组件，保持工程可编辑：https://qgis.org/pyqgis/3.44/core/QgsLayoutItemAttributeTable.html 。
- QGIS实际表格宽度包括每列两侧cellMargin和所有gridStrokeWidth，不能只按列文本宽度分配；已按原生参数计入边框。createRenderContextForLayout需显式painter=None及DPI。QGS毫米序列化有约0.0005mm变化，已有几何容差可接受；比例尺单独记在实际布局中，不把其派生微差添加到冻结几何摘要。空业务属性列表在QGIS customProperty可读为None，按空列表处理。独立QGIS命名空间避免主Python dotenv/ABI依赖泄漏。

## 2026-10-14 提示词与布局联合修复依据

- 用户最新真实会话已收到qgis_inputs但仍把分析manifest绑定原始shp；需提示词强调关联且错误指明替换建议，不自动替换。
- 195mm页面图例需求118.77mm/可用70.98mm；legend_auto_fit无minimum_zoom时仅试压缩详图，失败不增页高。主图/详图/名单联动需在原生量测内统一求解。
- 模型照用旧candidate_extent又改340→310→330mm页高及名单2列→1列，改变建议前提。最终坐标舍入到0.1m造成actual_zoom=1.9999953108881092，绝对容差拒绝但错误3位小数显示2.000；需微小相对容差、足够诊断精度和安全余量。
- 非法JSON提示Expecting ',' delimiter(position2699/3245chars)，无完整原始串不能猜具体引号；finish_reason=tool_calls不证明max_tokens不足。优先预检+plan_ref减少长参数生成，失败不猜修业务参数。
- 规划首次补丁因findings尾行上下文不全被拒，未产生修改；读取精确行后重新追加。

## 2026-10-14 最新Python三次失败与短引用误用

- inspect_attributes第7行统一encoding=utf-8，实际在GBK居民点解码失败；执行快照已包含逐层编码，因此是未采用已知信息。
- inspect_fields3第16行调用未定义print_stats；前面的read_vector/水系统计成功，runner未提供此函数，每次进程独立。
- fuel_hist_and_fields栅格和居民点统计成功，事件read_vector短名失败；该文件通过file_path加载，_load把绝对路径写成profile.name，工具注册命名不一致。需要规范名/旧记录唯一basename兼容，重名拒绝不猜，保留签名和hash检查。
- QGIS预检回执已经给出两字段create_call，模型仍补map_id/title/output_format；明确保存身份的一般规则不向短引用增加字段，改动应重新预检。Python失败前置error_summary/recovery_hints可避免关键原因被运行路径和堆栈挤到折叠区域。

## 2026-10-14 定位/样式与服务恢复依据

- 最新Python root=None后只检查DATA_DIRS各根目录直下shp，而文件在数据子目录；os.path.join(None,...)先失败，数据引用已齐全。恢复提示原未覆盖此模式，应指向read_vector/resolve_data而非猜目录。
- layers[6]误把renderer/field/categories放外层；仅这三个字段作白名单无冲突归并，冲突/其他未知字段继续拒绝；记录实际规范化。
- 原生分析确切路径原先已有manifest推断，不重复造轮子。引用增加明确analysis_manifest和生产清单摘要，入口保留严格校验，原始shp不会被静默替换。
- 实际四次审图工具调用为两次provider503、两次缓存未发请求。503不证明图像正确；成功审查缓存和短期transient故障缓存应分开，服务冷却不得靠改名/重导出绕过，准确回传真实调用与缓存计数。
- 首个style重复字段测试误用了single渲染下无效field，产品既有校验正确；将测试改为重复相同renderer验证兼容，不放松产品校验。


## 2026-10-15 撤回底图与GitHub发布

- 用户明确要求先删除新增在线底图，再发布当前项目到既有GeoHarness仓库。撤销46b7adc，活跃源码恢复baa50b0；原有本地栅格底图与QGIS仍保留。配置YAML仅移除basemaps段，本机.env仅移除专用token变量。
- origin为kevics1/GeoHarness公开仓库，默认master。Git全局代理127.0.0.1:7890不可达；本次Git命令以临时空proxy覆盖可连接，不修改全局配置。
- 原有栅格inspect测试断言旧尺寸文本；当前回执已分开提供Width/Height和source_detail。改为验证实际宽高显示与元数据，不修改工具行为。
- 发布仅使用已跟踪源码/文档/测试；本地.env.example修改和未跟踪.scratch、诊断探针、数据库不进入发布。保持开发历史，以origin/master为父发布当前快照，无强推。
- 清理两个.scratch底图测试目录被自动审批拒绝（仅返回blocked by policy）；保留这些本机临时产物，不发布，不改用其他删除方法。

---

# Oct17 实施发现

- 用标准库sqlite3保存独立版本记录与哈希去重的报告，无新增依赖；任务头与版本在同一事务提交，短locator与历史大小无关。
- 保留原JSON的显式迁移使用临时库全量校验后切换，备份哈希和版本身份必须保持。普通只读查询不能自动修改原业务台账。
- 原始工作区修改来源存在重叠，本轮采用隔离工作副本；不得把混合状态测试结果计作隔离版本验收。


- 跨阶段回归补获两处旧接口问题：另存恢复入口str/Path类型不一致，完整model_dump中的默认selector被误拒；已修正并复验。
- 健康查询的JSON分页必须携带独立history_offset/history_limit锚点，避免根分页停在第一页或将长路径read_call误读为另一版本；源台账保持只读。
- 已安装的合成串联通过不等于真实业务质量通过；此次独立审查派发未获得有效报告，必须保留未验证状态。

复制后的台账读取故障必须与复制前候选校验区别处理：复制前默认拒绝无台账的已核验交付；复制后保存实际目录和清单，明确未登记与质量未验证。失败登记不能把旧receipt写成此次交付凭据。
