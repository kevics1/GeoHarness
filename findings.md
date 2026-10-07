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
