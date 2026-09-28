---
name: spatial-analysis
description: 空间分析总入口 — 点数据分布规律（Moran's I/核密度/热点）与多图层叠加、邻近、分区统计的完整工具链与工作流。用户要求"分析空间规律/分布/聚集/热点"时加载。
---

# 空间分析工作流

## 第一步：判断数据形态（决定整条技术路线）

| 数据形态 | 特征 | 走哪条路线 |
|---|---|---|
| **点数据** | 居民点/监测站/POI（每行一个点） | → 路线 A：点模式分析 |
| **面数据** | 行政区/网格（每行一个多边形+属性值） | → 路线 B：面聚合统计 |
| **线数据** | 河流/道路（拓扑/网络问题） | → 路线 C：网络/缓冲分析 |

## 路线 A：点数据空间规律（如"受威胁居民点空间规律"）

点数据无法直接算 Moran's I（需要面单元聚合）。标准流程：

1. **描述性空间结构**（L1，本地即可完成）
   - `geo_vector_data action=load` → 得到要素数、字段、CRS、范围
   - `geo_vector_data action=inspect` → 几何类型确认（Point?）
   - 用 `postgres` MCP 工具做 SQL 分组统计（按区县/字段分组计数）

2. **空间聚集证据**（L2，用 MCP 空间分析工具）
   - 核密度估计 / 热点：`geo-mcp-server` 的 `geo_cluster_detect`、
     `geo_getis_ord`（Gi*）
   - 全局/局部自相关：`geo_moran_global` / `geo_moran_local`（LISA）
     —— 注意：需先把点聚合到面单元（如 village/county 网格计数），
     再以 `geometries` 参数传入多边形坐标（避免大 payload 超时）
3. **邻近与背景关联**（L1+）
   - 到火场/道路/水系的距离：`geo_calculate_distance`、`geo_buffer`
   - 拓扑关系：`geo_spatial_relation`

4. **成果落图**
   - `geo_cartography` 多图层渲染（layers 参数），
     叠加 行政区划+火场+居民点（按密度/等级配色）

> ⚠️ `analysis_type`（cluster_detect/moran_local 等）只影响制图符号化方案，
> 不执行统计计算。统计必须调 MCP 工具；符号化只是把结果画出来。

## 路线 B：面数据统计

1. `geo_db_data action=load`（PostGIS 表）或 `geo_vector_data action=load`
2. `geo_moran_global` / `geo_moran_local`（直接传面几何 + 属性值）
3. `geo_getis_ord` 找热点/冷点
4. 成果图用 graduated 渲染（连续值分级）

## 路线 C：线/邻近/叠加

- 邻近/可达性 → 技能 `proximity-analysis`
- 多因子叠加/风险区划 → 技能 `overlay-analysis`
- 分区统计（面内点数/均值）→ 技能 `zonal-statistics`

## 相关技能

| 技能名 | 用途 |
|---|---|
| `geo-perception` | L1 五步感知（坐标/距离/方向/几何/拓扑） |
| `geo-comprehension` | L2 六步理解（自相关/热点/异质性） |
| `geo-reasoning` | L3 六步推理（聚类/OD/因果） |
| `proximity-analysis` | 邻近/覆盖/可达性 |
| `overlay-analysis` | 多图层叠加/风险区划 |
| `zonal-statistics` | 分区统计 |
| `cartography` | 分析成果落图（含多图层用法） |

## 报告规范

空间规律分析结论必须包含：
1. **全局格局**（聚集/离散/随机 + 统计量与 p 值）
2. **局部位置**（热点/冷点在哪里 —— LISA/Gi* 结果）
3. **尺度声明**（分析单元、MAUP 提醒）
4. ** CRS 声明**（WGS84 / CGCS2000 / 投影坐标）
5. **成果图**（geo_cartography 渲染，符号化与分析类型匹配）
