# GeoHarness

地理方向原生 Agent 系统 — 空间分析优先，制图为分析产物。

基于 OpenHarness 构建，通过直接构造 RuntimeBundle 实现 8 层配置隔离，不修改上游源码。

## 架构概览

```
用户输入
  → GeoHarness 系统提示词（6地理原则 + 技能目录 + 工具目录 + 级联状态）
  → LLM 调用（OpenAI 兼容 API）
  → 工具调度（2 原生 + 66 MCP + 5 白名单 = 73 工具）
  → 认知级联（L1感知 → L2理解 → L3推理，建议制，模型可跳过）
  → 制图输出（符号化 → QPT模板 → 导出）
```

**认知三级联**：

| 层级 | 技能 | 核心工具 | 触发条件 |
|------|------|----------|----------|
| L1 感知 | geo-perception | geo_data, geo_geocode, geo_spatial_relation | 用户请求空间分析 |
| L2 理解 | geo-comprehension | geo_moran_global, geo_moran_local, geo_getis_ord | L1完成 + 实体≥5 + 需判断分布模式 |
| L3 推理 | geo-reasoning | geo_cluster_detect, geo_od_flow, geo_causal_check | L2发现显著模式 (p<0.05) |

级联为建议制 — 模型保留跳过权限。L1 结果足够则停止，模式不显著则返回描述性结论。

## 前置条件

**必须安装**：

- Python 3.10+
- OpenHarness AI (`pip install openharness-ai>=0.1.9`)
- QGIS 3.40+（提供 qgis_mcp_server 和 PROJ 数据库）
- PostgreSQL 15+ with PostGIS 3.3+（空间数据存储）

**可选但推荐**：

- 高德开放平台 API Key（地理编码和行政区划服务，免费额度足够）
- Node.js 18+（React TUI 前端，无则自动降级到打印模式）

## 安装

```bash
# 1. 克隆项目
cd F:\Desktop
# （假设项目已在 F:\Desktop\GeoHarness\）

# 2. 安装 GeoHarness（开发模式）
cd GeoHarness
pip install -e ".[dev]"

# 3. 验证安装
geoh version
# 输出: GeoHarness v0.1.0
```

## 配置

### 第一步：初始化配置目录

```bash
geoh init
```

这会在 `~/.geoharness/` 下创建：

```
~/.geoharness/
├── config.yaml      # 主配置文件
├── .env             # 环境变量（密钥、连接串等敏感信息）
└── workspaces/      # 工作空间目录
```

### 第二步：编辑 .env 文件

打开 `~/.geoharness/.env`，填入实际值：

```bash
# ── LLM API ──────────────────────────────────
# 支持 OpenAI 兼容 API：DeepSeek、OpenAI、Moonshot、CNB 等
GEOH_MODEL=deepseek-chat
GEOH_API_KEY=sk-your-actual-api-key-here
GEOH_BASE_URL=https://api.deepseek.com/v1

# ── PostGIS 数据库 ────────────────────────────
# 如果你没有 PostGIS，可以暂时留空，仅使用文件数据源
GEOH_POSTGIS_DSN=postgresql://user:password@localhost:5432/geodb

# postgres MCP server 使用的连接参数（与上面 DSN 对应）
GEOH_POSTGRES_HOST=localhost
GEOH_POSTGRES_PORT=5432
GEOH_POSTGRES_DB=geodata
GEOH_POSTGRES_USER=postgres
GEOH_POSTGRES_PASSWORD=your-password

# ── 制图模板 ──────────────────────────────────
# QPT 模板文件路径，用于地图制图导出
# 可用默认模板，也可自定义
GEOH_DEFAULT_TEMPLATE=F:/Desktop/OpenHarness/mapping_knowledge/mapping_resources/07_制图模板库/BASE/BASE-REF_A3H.qpt
```

### 第三步：编辑 config.yaml

打开 `~/.geoharness/config.yaml`，根据实际环境调整：

```yaml
# ── LLM 配置 ──────────────────────────────────
# ${VAR} 引用 .env 中的环境变量
model: ${GEOH_MODEL}
api_key: ${GEOH_API_KEY}
base_url: ${GEOH_BASE_URL}

# ── MCP 服务器 ────────────────────────────────
# 3 个领域 MCP 服务器，提供 66 个工具
mcp:
  geo-mcp-server:
    # 空间分析核心：地理编码、空间统计、聚类、因果检验
    command: python
    args: ["-m", "geo_mcp_server"]
    env:
      PYTHONUTF8: "1"              # 必须！修复 Windows 中文路径 .pth 问题
      AMAP_API_KEY: your-amap-key  # 高德 API Key，用于地理编码

  qgis:
    # QGIS 制图：项目管理、图层、符号化、布局、导出
    command: python
    args: ["-m", "qgis_mcp_server"]
    env:
      PROJ_LIB: "E:/Program Files/QGIS 3.40.8/share/proj"  # QGIS PROJ 数据库路径

  postgres:
    # PostgreSQL 查询：schema 检查、查询分析
    command: python
    args: ["-m", "postgres_mcp_server"]
    env:
      PGHOST: ${GEOH_POSTGRES_HOST}
      PGPORT: ${GEOH_POSTGRES_PORT}
      PGDATABASE: ${GEOH_POSTGRES_DB}
      PGUSER: ${GEOH_POSTGRES_USER}
      PGPASSWORD: ${GEOH_POSTGRES_PASSWORD}

# ── 制图配置 ──────────────────────────────────
cartography:
  default_template: ${GEOH_DEFAULT_TEMPLATE}
  # 桥接规则：分析类型 → 符号化映射
  bridge_rules:
    moran_local:
      render_method: categorized    # 分类渲染
      color_scheme: RdBu            # 红蓝色带（适合居中数据）
      n_classes: 5
    getis_ord:
      render_method: graduated      # 分级渲染
      color_scheme: YlOrRd          # 黄橙红色带（适合热度数据）
      n_classes: 6
    cluster_detect:
      render_method: categorized
      color_scheme: Set1             # 分类色板
    od_flow:
      render_method: flow
      color_scheme: Blues

# ── 认知级联配置 ──────────────────────────────
cognition:
  cascade:
    # L1→L2 触发工具：这些工具完成后，建议加载 L2
    l1_to_l2_tools: ["geo_spatial_relation", "geo_all_relations"]
    # L2→L3 触发工具：这些工具完成后，建议加载 L3
    l2_to_l3_tools: ["geo_moran_local", "geo_getis_ord"]
    suggestion_prompt: "分析结果已完成，建议加载更高级认知技能以深入理解空间模式。"

# ── 数据配置 ──────────────────────────────────
data:
  default_crs: EPSG:4326             # WGS84（GPS/国际标准）
  analysis_crs: EPSG:3857            # Web Mercator（面积/距离计算用）
  postgis_dsn: ${GEOH_POSTGIS_DSN}
  # 行政区划 API（高德 DataV，免费）
  admin_kg_api_url: https://geo.datav.aliyun.com/v2/district
  admin_kg_cache_dir: ~/.geoharness/workspaces/admin_kg_cache

# ── 权限配置 ──────────────────────────────────
permissions:
  allow_write_paths:
    - ~/.geoharness/workspaces/
  deny_paths:
    - ~/.ssh/
    - ~/.gnupg/
```

### 配置项详解

**LLM API**：支持所有 OpenAI 兼容 API。常用选项：

| 服务商 | model | base_url |
|--------|-------|----------|
| DeepSeek | deepseek-chat | https://api.deepseek.com/v1 |
| OpenAI | gpt-4o | https://api.openai.com/v1 |
| Moonshot | moonshot-v1-32k | https://api.moonshot.cn/v1 |
| CNB | hy3-preview | https://api.cnb.cool/{repo}/-/ai |

**MCP 服务器路径**：`command` 和 `args` 中的路径必须指向实际安装位置。如果 MCP 服务器安装在虚拟环境中，使用完整 Python 路径：

```yaml
geo-mcp-server:
  command: C:/Users/xxx/AppData/Local/Programs/Python/Python310/python.exe
  args: ["E:/Administrator/hermes/geo-mcp-server/run.py"]
```

**PROJ_LIB**：QGIS 安装目录下的 `share/proj` 文件夹。不设会导致 CRS 转换失败。

**坐标系说明**：
- `default_crs` (EPSG:4326)：WGS84，GPS 数据和国际标准
- `analysis_crs` (EPSG:3857)：Web Mercator，用于面积和距离计算
- 中国发布地图需使用 GCJ-02 坐标系，分析时用 WGS84

### 第四步：验证配置

```bash
geoh dry-run
```

输出类似：

```
GeoHarness 运行时验证
├──────────────┬──────────────────────────┐
│ 组件         │ 状态                     │
├──────────────┼──────────────────────────┤
│ RuntimeBundle │ OK                      │
│ API Client   │ OpenAICompatibleClient   │
│ ToolRegistry │ 0 tools                 │
│ MCP Manager  │ connected               │
│ QueryEngine  │ QueryEngine             │
└──────────────┴──────────────────────────┘
```

如果显示错误，检查 `.env` 中的 API Key 和 `config.yaml` 中的路径。

## 使用

### 启动交互式会话

```bash
# 默认 React TUI（复用 OpenHarness 前端，环境变量隔离配置）
geoh run

# 带初始提示词启动
geoh run --prompt "分析武汉市 PM2.5 空间分布"

# 指定工作目录
geoh run --cwd /path/to/project

# 打印模式（不启动 React TUI，stdin/stdout 交互）
geoh run --print
```

### 其他命令

```bash
geoh version        # 显示版本
geoh init           # 初始化配置目录
geoh init --force   # 覆盖已有配置
geoh dry-run        # 验证运行时组装
```

### 典型工作流

```
用户：分析武汉市空气质量监测站的空间分布

GeoHarness：
1. [L1] 调用 geo_data 加载监测站数据
2. [L1] 调用 geo_spatial_relation 计算空间关系
3. → 级联建议：L2理解（Moran's I 空间自相关）
4. [L2] 调用 geo_moran_global 全局空间自相关
5. [L2] 调用 geo_moran_local LISA 局部指标
6. → 级联建议：L3推理（因果检验）
7. [L3] 调用 geo_cluster_detect 聚类检测
8. [制图] 调用 geo_cartography 符号化+导出
9. 输出：分析结论 + 空间分布地图
```

## 项目结构

```
GeoHarness/
├── src/geoharness/
│   ├── cli.py                    # CLI 入口 (geoh 命令)
│   ├── runtime.py                # 运行时组装 (直接构造 RuntimeBundle)
│   ├── backend.py                # TUI 后端桥接
│   ├── launcher.py               # TUI 启动器 (React优先, 打印模式降级)
│   ├── config/settings.py        # 配置解析 (YAML + .env)
│   ├── data/
│   │   ├── catalog.py            # 数据目录 (统一接口)
│   │   ├── postgis.py            # PostGIS 连接器
│   │   ├── file_loader.py        # 文件数据源 (SHP/GeoJSON/GPKG/CSV)
│   │   └── admin_kg.py           # 行政区划 (高德 DataV API)
│   ├── tools/
│   │   ├── geo_data.py           # 数据加载工具
│   │   ├── geo_cartography.py    # 制图工具 (符号化+导出)
│   │   └── __init__.py           # 工具注册 (白名单+MCP适配)
│   ├── hooks/cascade.py          # 认知级联钩子 (CascadeManager)
│   ├── mcp/config.py             # MCP 服务器配置构建器
│   └── prompts/system_prompt.py  # 系统提示词构建器
├── skills/                       # 3 个认知技能
│   ├── geo-perception/           # L1: 5步感知流程
│   ├── geo-comprehension/        # L2: 6步理解流程
│   └── geo-reasoning/            # L3: 6步推理流程
├── tests/                        # 207 个测试
│   ├── test_config.py            # 配置解析
│   ├── test_runtime.py           # 运行时组装
│   ├── test_data.py              # 数据层
│   ├── test_tools.py             # 工具层
│   ├── test_skills.py            # 技能集成
│   ├── test_mcp.py               # MCP 集成
│   ├── test_hooks.py             # 级联钩子
│   ├── test_prompts.py           # 系统提示词
│   └── test_e2e.py               # 端到端测试
└── pyproject.toml
```

## 工具清单

**原生工具 (2)**：

| 工具 | 功能 | 只读 |
|------|------|------|
| geo_data | 加载/检视地理数据 (PostGIS/文件/行政区划) | 是 |
| geo_cartography | 符号化/排版/导出地图 (通过 QGIS MCP) | 视操作 |

**MCP 工具 (66)**：

| 服务器 | 工具数 | 核心功能 |
|--------|--------|----------|
| geo-mcp-server | 18 | 地理编码、空间统计、聚类、因果检验 |
| qgis | 39 | 项目管理、图层操作、地理处理、符号化、布局导出 |
| postgres | 9 | 数据库查询、schema 检查、查询分析 |

**白名单工具 (5)**：ask_user_question, skill, todo_write, tool_search, brief

## 测试

```bash
# 运行全部测试
python -m pytest

# 运行特定测试
python -m pytest tests/test_hooks.py -v

# 检查代码风格
python -m ruff check src/ tests/
```

## 常见问题

**Q: `geoh dry-run` 报 "No API key configured"**

A: 编辑 `~/.geoharness/.env`，填入 `GEOH_API_KEY=sk-xxx`。

**Q: MCP 服务器连接失败**

A: 检查 `config.yaml` 中 MCP 服务器的 `command` 和 `args` 路径是否正确。Windows 上需要用完整路径或确保 Python 在 PATH 中。

**Q: `PYTHONUTF8: "1"` 为什么必须设？**

A: Windows 上 Python 读取 `.pth` 文件时用 GBK 解码，遇到 UTF-8 中文路径会 crash。设 `PYTHONUTF8=1` 强制 UTF-8 模式。

**Q: PROJ_LIB 报错 / CRS 转换失败**

A: 在 qgis MCP 服务器的 env 中设置 `PROJ_LIB` 指向 QGIS 安装目录下的 `share/proj`。

**Q: 没有 PostGIS 数据库怎么办？**

A: `.env` 中 `GEOH_POSTGIS_DSN` 留空即可。GeoHarness 会使用文件数据源 (SHP/GeoJSON/GPKG) 和行政区划 API 作为替代。

**Q: L2 统计工具超时**

A: L2 工具 (moran/getis_ord) 优先传 `geometries` 参数（坐标列表），不要传 `weights` JSON。大数据集权重 JSON 可达数百 KB，导致 MCP 通信超时。

## 隔离机制

GeoHarness 通过直接构造 RuntimeBundle 实现 8 层隔离：

1. 不调用 `build_runtime()`，避免读取 `~/.openharness/settings.json`
2. 独立配置路径 `~/.geoharness/`
3. 工具白名单（移除 33+ 通用工具，保留 5 个辅助工具）
4. MCP 隔离验证（检查上游服务器泄漏）
5. API Key 直接传入（绕过 `resolve_auth()` profile 覆盖）
6. 不加载上游插件
7. 独立后端入口
8. 启动时验证隔离

## 许可证

MIT
