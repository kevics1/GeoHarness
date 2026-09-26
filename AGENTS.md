# AGENTS.md — GeoHarness 工作手册

## 项目概述
GeoHarness 是基于 OpenHarness 二次开发的地理方向原生 Agent 系统。
核心理念：空间分析为主，制图是分析的产物。

## 技术栈
- Python 3.10+, async-first
- OpenHarness v0.1.9 (pip 依赖，不修改源码)
- Pydantic v2, Typer CLI, Rich TUI
- PostGIS, GeoPandas, Shapely
- 原生制图渲染: matplotlib (Agg) + folium (Leaflet) + mapclassify（无需 QGIS）
- geo-mcp-server (空间分析), postgres MCP (数据库)

## 项目独立性
GeoHarness 是独立项目，位于 `F:\Desktop\GeoHarness\`，不在 OpenHarness 子目录中。
配置目录: `~/.geoharness/` (与 `~/.openharness/` 完全隔离)

## 编码规范
- ruff: line-length=100, target py310
- 异步优先: 所有工具 `async def execute()`
- Pydantic v2 用于所有数据模型
- pytest-asyncio auto mode
- uv 包管理

## 配置优先原则
所有路径、模板、默认参数、敏感值必须存储在 config.yaml 或 .env 中。
源码中不包含任何硬编码路径或域名特定常量。

## 认知梯级 (L1→L2→L3)
- L1 geo-perception: 空间实体提取、关系判定、尺度锚定
- L2 geo-comprehension: Moran's I、LISA、Getis-Ord、语义标注
- L3 geo-reasoning: 聚类检测、OD流、因果前置条件、空间叙事
基于 PromptHook 的建议式梯级，模型保留跳过权限。

## 开发流程
每个阶段: 实现 → 测试 → ruff + pytest → 提交 → CLI验证
绝不批量多个阶段后测试。

## CLI 入口
- `geoh --version`: 显示版本
- `geoh init`: 创建配置目录
- `geoh run`: 启动 TUI
- `geoh dry-run`: 验证运行时组装
