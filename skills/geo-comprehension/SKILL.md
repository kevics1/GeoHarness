---
name: geo-comprehension
description: "空间理解认知流程：从空间感知结果到模式识别、语义标注、异质性检验。6步决策链实现拓扑语义/空间模式/尺度语义理解。"
version: 0.1.0
author: agent
created_by: agent
metadata:
  hermes:
    tags: [geoai, spatial, comprehension, pattern, moran, lisa, gwr, semantics]
---

# 空间理解 (Geo-Comprehension)

> L2空间理解：从L1感知结果到空间模式、语义、异质性的认知流程。
> 依赖：geo-mcp-server（L1+L2工具层）提供计算能力。
> 前置：geo-perception（L1）完成5步感知流程。

## 认知决策链（6步）

### Step 1: 模式需求识别

**目标**：判断L1感知结果是否需要模式/规律分析

**判断标准**（满足任一即YES）：
- 问题涉及"分布特征/空间格局/聚集/分散"
- 问题涉及"区域差异/空间异质性"
- 问题涉及"为什么这里高/低"
- 问题涉及"是否空间相关"
- L1感知结果中实体数量≥5且需判断分布模式

```
输入：L1感知结果 + 用户问题
判断：需要模式/规律判断？
├─ NO → L1输出即为最终结果
└─ YES → Step 2
```

### Step 2: 空间权重构建

**目标**：选择并构建空间权重矩阵

**权重选择规则**：

| 数据类型 | 推荐权重 | 理由 |
|----------|----------|------|
| 面状（行政区） | Queen邻接 | 共享边界/顶点即邻居，最常用 |
| 面状（规则网格） | Rook邻接 | 仅共享边界，更严格 |
| 点状 | KNN (k=5-8) | 点无边界，用近邻定义 |
| 距离衰减明显 | 距离带宽 | 距离越近权重越大 |
| 多种不确定 | Queen + KNN对比 | 稳健性检验 |

**MCP工具**：geo_spatial_weights

**输出**：权重矩阵 + 孤立单元检查 + 连通性

### Step 3: 全局模式检验

**目标**：检验空间分布是否随机

**假设检验**：
- H₀：空间随机分布（无空间自相关）
- H₁：空间聚集或分散

**Moran's I解读**：

| I值 | p值 | 结论 |
|-----|-----|------|
| I>0, p<0.05 | 显著 | 空间聚集（相似值相邻） |
| I<0, p<0.05 | 显著 | 空间分散（相异值相邻） |
| I≈0, p>0.05 | 不显著 | 空间随机 |

**MCP工具**：geo_moran_global

**输出**：I值 + p值 + z-score + 模式判断

### Step 4: 局部模式识别

**目标**：识别具体聚集位置和类型

**LISA象限分类**：

| 象限 | 编码 | 含义 | 俗称 |
|------|------|------|------|
| 1 | HH | 高值被高值包围 | 热点 |
| 2 | LH | 低值被高值包围 | 异常低 |
| 3 | LL | 低值被低值包围 | 冷点 |
| 4 | HL | 高值被低值包围 | 异常高 |

**Getis-Ord Gi***：热点/冷点检测，与LISA互补
- Gi*高+z显著 → 热点（高值聚集中心）
- Gi*低+z显著 → 冷点（低值聚集中心）

**MCP工具**：geo_moran_local / geo_getis_ord

**输出**：每单元分类 + 显著性 + 聚集统计

### Step 5: 空间异质性检验

**目标**：检验关系是否空间非平稳

**何时需要GWR**：
- 全局回归系数解释力不足（R²低）
- 理论预期关系存在空间差异
- LISA显示HL/LH异常区域（关系可能反转）

**⚠️ GWR已从MCP工具中移除**。原因：计算耗时16-30秒，违反MCP快速响应原则；带宽选择和系数解释需要研究者判断力，不适合LLM实时调用。

**替代方案**：在本地Python/Jupyter中运行GWR，参考 `references/gwr-offline-guide.md` 获取：
- 决策流程（何时用GWR vs OLS）
- 代码模板（经验带宽/自动搜索/显著性检验）
- 与MCP工具的协作流程（前置用moran/lisa，后置用semantic_label）

**样本量建议**：
- n<30：GWR不稳定，用OLS
- n≥30：正常GWR，经验带宽 bw=max(n×0.6, 30)

**MCP协作**：GWR分析前用`geo_moran_global`/`geo_moran_local`确认空间自相关存在；GWR分析后用`geo_semantic_label`标注发现。

### Step 6: 语义综合

**目标**：整合模式+机制+尺度→语义描述

**语义综合规则**：
1. 全局模式 → 宏观判断（"全国PM2.5呈显著聚集"）
2. 局部热点 → 空间叙事锚点（"华北平原HH聚集"）
3. 异质性 → 机制线索（"东西部经济-污染关系反转"）
4. 尺度约束 → 结论可信度（"市级结论不可推至区级"）

**MCP工具**：geo_semantic_label

**输出**：结构化理解结果

## L1→L2衔接协议

```
[L1 geo-perception] 输出:
  spatial_entities[] + spatial_relations[] + scale

         │
         ▼
Step 1: 需要模式分析？
  ├─ NO → 返回L1结果
  └─ YES ↓

[L2 geo-comprehension] 输入:
  L1感知结果（直接消费，不重复计算）

         │
    Step 2→3→4→5→6
         │
         ▼
结构化理解结果
```

## MCP工具索引（L2新增）

| 工具 | 功能 | 关键参数 |
|------|------|----------|
| geo_spatial_weights | 构建空间权重矩阵 | geometries, weight_type, k |
| geo_moran_global | 全局Moran's I | values, geometries(推荐), weights(fallback), weight_type, k |
| geo_moran_local | 局部Moran's I (LISA) | values, geometries(推荐), weights(fallback), weight_type, k, permutations |
| geo_getis_ord | Getis-Ord Gi*热点分析 | values, geometries(推荐), weights(fallback), weight_type, k, permutations |
| geo_semantic_label | 拓扑→语义标注 | relation, entity_types, domain |

> **GWR已移除**：计算耗时16-30秒，不适合MCP快速响应。见 `references/gwr-offline-guide.md` 获取离线分析指南和代码模板。

> **重要**：L2统计工具(moran/getis_ord)优先传`geometries`参数，工具内部自动构建权重。避免传`weights` JSON——序列化权重在LLM↔MCP间来回传输浪费token，大数据集会超时。`weights`仅在geometries不可用时作fallback。

## MCP工具设计原则

| 原则 | 说明 |
|------|------|
| 响应<2秒 | MCP工具必须快速响应，批处理分析（GWR等）不属于MCP |
| 几何优先 | L2工具传`geometries`而非序列化`weights` JSON，避免大数据集超时 |
| 点数据用KNN | Queen/Rook对点数据走Voronoi（8s+），自动降级为KNN |
| 小样本用解析p值 | n<30时permutations=0用analytic inference，避免esda慢路径 |
| 降级优雅 | 工具不可用（缺依赖/超时）时返回fallback+说明，不崩溃 |

## 性能陷阱（geo-mcp-server实现细节）

| 陷阱 | 症状 | 处理 |
|------|------|------|
| Queen/Rook对点数据 | 构建权重8s+（Voronoi） | 自动降级为KNN |
| permutations<99 | getis_ord超时30s+ | n<30用解析p值(perm=0)，n≥30 clamp≥99 |
| permutations=0无p_sim | AttributeError | G_Local用p_norm，Moran_Local用scipy z→p |
| 权重JSON传参 | 大数据集MCP超时 | 优先传geometries而非weights |
| GWR在MCP中 | 16-30秒超时 | **已移除**，见`references/gwr-offline-guide.md`离线运行 |
| 小带宽GWR | bw=16需25秒，bw=20需0.07秒 | mgwr小bw迭代暴增，离线时用bw≥max(n*0.6,30) |
| MCP客户端超时 | moran/getis_ord超时 | 客户端设timeout:120000ms |

## 运维参考

- `references/mcp-server-ops.md` — MCP Server配置方式(Hermes API/其他工具)、Skills导入、权重传递bug修复记录、esda API陷阱
- `references/gwr-offline-guide.md` — GWR离线分析指南：决策流程+代码模板+与MCP协作方式

## 领域扩展插件

| 插件名 | 领域 | L2扩展内容 | 状态 |
|--------|------|-----------|------|
| air-pollution | 空气污染 | 污染模式解读+暴露异质性 | active |

## 使用方式

1. 先完成L1感知：`skill_view(name="geoai/geo-perception")`
2. 判断需要L2时加载：`skill_view(name="geoai/geo-comprehension")`
3. 按L1→L2衔接协议执行
4. L2工具调用geo-mcp-server
5. **L2发现显著空间模式时自动进入L3**：`skill_view(name="geoai/geo-reasoning")`

## L2→L3衔接

当L2输出满足以下任一条件时，自动进入L3空间推理：
- 全局Moran's I显著（p<0.05）
- LISA有显著HH/LL/HL/LH
- Gi*有显著hot/cold

L3工具：`geo_cluster_detect`（聚类）、`geo_od_flow`（流向）、`geo_causal_check`（因果前提检验）
详见：`skill_view(name="geoai/geo-reasoning")`
