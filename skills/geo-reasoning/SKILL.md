---
name: geo-reasoning
description: "空间推理认知流程：从L2模式结果到因果识别、空间叙事。6步决策链实现聚类识别、流向分析、因果前提检验、叙事生成。"
version: 0.1.0
author: agent
created_by: agent
metadata:
  hermes:
    tags: [geoai, spatial, reasoning, causal, narrative, cluster, od-flow]
---

# 空间推理 (Geo-Reasoning)

> L3空间推理：从L2模式结果到因果识别与空间叙事。
> 依赖：geo-mcp-server（L1+L2+L3工具层）提供计算能力。
> 前置：geo-comprehension（L2）完成6步理解流程。

## 认知决策链（6步）

### Step 1: 推理需求识别

**目标**：判断L2输出是否需要因果/机制解释

**自动触发条件**（满足任一即进入L3）：
- L2全局Moran's I显著（p<0.05）
- L2 LISA有显著HH/LL/HL/LH
- L2 Gi*有显著hot/cold
- 用户明确询问"为什么"空间模式存在

```
输入：L2理解结果 + 用户问题
判断：存在显著空间模式需要因果解释？
├─ NO → L2输出即为最终结果
└─ YES → Step 2
```

### Step 2: 空间聚类识别

**目标**：发现自然空间分组，为因果分析提供空间结构

**双模式自动选择**：

| 输入几何类型 | 聚类方法 | 适用场景 |
|-------------|----------|----------|
| 点（Point） | DBSCAN | 监测站/POI密度聚类 |
| 面（Polygon/MultiPolygon） | 空间约束层次聚类 | 城市带/区域分组 |

**DBSCAN参数指南**：

| 参数 | 含义 | 推荐值 |
|------|------|--------|
| eps | 邻域半径（度） | 0.5-2.0（市级），0.1-0.5（区级） |
| min_samples | 最小簇大小 | 3-5 |

**空间约束聚类参数指南**：

| 参数 | 含义 | 推荐值 |
|------|------|--------|
| n_clusters | 目标分组数 | 3-7（探索性） |
| connectivity | 空间邻接矩阵 | 从L2 weights复用 |

**MCP工具**：geo_cluster_detect

**输出**：每单元簇标签 + 簇统计（大小/均值/方差） + 空间连续性

### Step 3: OD流向分析

**目标**：识别空间交互主导方向（如有OD数据）

**何时跳过**：无OD数据时直接进入Step 4。

**输入约束**：
- 区域数 n ≤ 50
- 返回 top_k ≤ 20 条主流向

**MCP工具**：geo_od_flow

**输出**：主流向列表（起终点+流量+占比）+ 净流入/流出排名 + 主导方向

### Step 4: 因果前提检验

**目标**：检验因果推断的基本假设是否满足

**3个快速检验**：

| 检验 | 内容 | 判定标准 |
|------|------|----------|
| 空间自相关 | 残差是否存在空间依赖 | Moran's I p>0.05 → 通过 |
| 样本量 | 是否有足够统计功效 | 处理组+对照组各≥15 → 通过 |
| 共同支撑 | 处理组和对照组协变量是否重叠 | SMD<0.25 → 通过（需covariates） |

**检验结果处理**：

```
空间自相关未通过 → 存在空间溢出，需用空间计量模型
样本量不足 → 因果推断不可靠，回L2描述性结论
共同支撑不足 → 处理组/对照组不可比，需重新分组
全部通过 → 可进入Step 5因果分析
```

**MCP工具**：geo_causal_check

**输出**：3项检验结果 + 推荐因果策略 + 注意事项

### Step 5: 因果分析（离线）

**目标**：选择并执行因果识别策略

**⚠️ 因果分析不在MCP中执行**。原因：因果推断需要研究者判断力（假设合理性、工具变量选择、带宽设定），计算耗时数分钟级，不适合LLM实时调用。

**策略选择指南**：

| 策略 | 适用条件 | 模板 |
|------|----------|------|
| 空间DID | 面板数据+政策干预+处理/对照 | `references/spatial-did-guide.md` |
| 空间断点设计(RDD) | 地理边界+连续性假设 | `references/spatial-rdd-guide.md` |
| 空间工具变量(IV) | 内生性+有效工具变量 | `references/spatial-iv-guide.md` |

**MCP协作**：
- 前置：Step 4确认前提满足
- 分析后：用L2工具（moran/semantic_label）验证残差和标注发现

### Step 6: 空间叙事生成

**目标**：整合L1+L2+L3→结构化空间叙事

**触发条件**：L3发现显著空间模式时自动触发

**叙事框架**：

```
[空间事实] → [机制解释] → [过程推演] → [意义建构]
```

| 环节 | 内容来源 | 质量标准 |
|------|----------|----------|
| 空间事实 | L2模式结果 | 有统计量支撑，非主观判断 |
| 机制解释 | 领域知识+L3因果分析 | 有因果证据链，非相关混因果 |
| 过程推演 | 逻辑推理 | 时序合理，非事后归因 |
| 意义建构 | 领域判断 | 有决策含义，非空泛评论 |

**叙事模板**：`templates/narrative-template.md`

**质量检查**：`references/reasoning-checklist.md`

## L2→L3衔接协议

```
[L2 geo-comprehension] 输出:
  全局模式(I值) + 局部模式(LISA/Gi*) + 异质性标记 + 语义标注

         │
         ▼
Step 1: 存在显著空间模式？
  ├─ NO → 返回L2描述性结论
  └─ YES ↓

[L3 geo-reasoning] 输入:
  L2模式结果（直接消费，不重复计算）

         │
    Step 2→3→4→5→6
         │
         ▼
空间叙事（事实→机制→过程→意义）
```

## MCP工具索引（L3新增）

| 工具 | 功能 | 关键参数 |
|------|------|----------|
| geo_cluster_detect | 双模式空间聚类 | geometries, values, method, eps, min_samples, n_clusters |
| geo_od_flow | OD流量聚合+主导流向 | origins, destinations, flows, top_k |
| geo_causal_check | 因果前提检验 | values, geometries, treatment, covariates(可选), weight_type, k |

> **重要约束**：
> - `geo_od_flow`：区域数 n≤50，top_k≤20
> - `geo_causal_check`：covariates可选，有则算SMD，无则跳过共同支撑检验
> - 因果分析（DID/RDD/IV）不在MCP中执行，见 `references/` 离线模板

## MCP工具设计原则（延续L2）

| 原则 | 说明 |
|------|------|
| 响应<2秒 | MCP工具快速响应，因果分析走离线 |
| 几何优先 | 传geometries而非序列化权重 |
| 点面双模式 | cluster_detect按几何类型自动选算法 |
| 降级优雅 | 工具不可用时返回fallback+说明 |
| 硬约束限流 | od_flow n≤50, top_k≤20 |

## 性能陷阱

| 陷阱 | 症状 | 处理 |
|------|------|------|
| DBSCAN eps过大 | 所有点归一簇 | eps取平均最近邻距离的1.5倍 |
| 空间约束聚类n_clusters过大 | 碎片化分组 | n_clusters≤7 |
| OD矩阵n>50 | MCP传参超时 | 硬约束拒绝，建议聚合后重试 |
| causal_check无协变量 | 跳过共同支撑 | 返回提示"补充协变量可提升检验完整性" |
| numpy类型序列化 | TypeError: Object of type bool_ is not JSON serializable | spatial_engine中所有bool/int返回值用`bool()`/`int()`包装，numpy的`bool_`和`int64`不是标准JSON类型 |
| DBSCAN eps过小 | 所有点变噪音 | eps<0.01度(≈1km)在城市尺度可能过小，尝试0.5-2.0 |
| causal_check样本量不足 | 无法进入因果分析 | 这是正确行为——样本不足时回L2描述性结论 |
| 空间约束聚类connectivity | 孤立区域导致AgglomerativeClustering报错 | Queen权重可能有islands，需预处理或改用KNN |

## 运维参考

- `references/spatial-did-guide.md` — 空间DID完整代码模板
- `references/spatial-rdd-guide.md` — 空间断点设计代码模板
- `references/spatial-iv-guide.md` — 空间工具变量代码模板
- `references/spatial-narrative-guide.md` — 叙事生成框架+质量清单
- `references/reasoning-checklist.md` — L3推理自检清单
- `references/mcp-server-ops.md` — MCP Server L3工具实现细节、numpy序列化陷阱、DBSCAN参数调优

## 领域扩展

L3是通用推理层，**不预留领域插件**。领域特定推理规则应在L1/L2的领域插件中定义，L3消费其输出。

## 使用方式

1. 先完成L2理解：`skill_view(name="geoai/geo-comprehension")`
2. 判断需要L3时加载：`skill_view(name="geoai/geo-reasoning")`
3. 按L2→L3衔接协议执行
4. L3工具调用geo-mcp-server
5. 因果分析在本地Jupyter运行，参考 `references/` 模板
