# MCP Server L3 工具运维参考

> geo-mcp-server L3工具实现细节、陷阱与修复记录

## 工具清单（L3新增3个）

| 工具 | 函数 | 计算引擎 | 依赖 |
|------|------|----------|------|
| geo_cluster_detect | server.py | cluster_dbscan / cluster_spatial_constrained | sklearn.cluster.DBSCAN, sklearn.cluster.AgglomerativeClustering |
| geo_od_flow | server.py | od_flow_aggregate | numpy (纯计算) |
| geo_causal_check | server.py | causal_prelim_check | esda.Moran, libpysal, numpy |

## numpy类型序列化陷阱（已修复）

**问题**：`causal_prelim_check`返回dict中含numpy `bool_`/`int64`类型，`json.dumps`抛出：
```
TypeError: Object of type bool_ is not JSON serializable
```

**根因**：Python的`sum()`对list[int]返回int，但numpy比较运算返回`numpy.bool_`，`json.dumps`不识别。

**修复**：所有返回JSON的函数中，bool用`bool()`包装，int用`int()`包装：
```python
sample_ok = bool(n_treat >= 15 and n_control >= 15)  # 不是 n_treat >= 15
sa_ok = bool(mi.p_norm > 0.05)  # 不是 mi.p_norm > 0.05
support_ok = bool(all(s < 0.25 for s in smd_list))
all_passed = bool(all(...))
```

**适用范围**：所有返回dict后经`json.dumps`序列化的函数。L2工具(moran/getis_ord)已用`round(float(...))`规避，L3的`causal_prelim_check`是新增函数需显式包装。

## DBSCAN参数调优

| 场景 | eps推荐 | min_samples推荐 | 说明 |
|------|---------|-----------------|------|
| 城市监测站 | 0.5-2.0度 | 3-5 | 站间距通常0.5-5度 |
| POI聚类 | 0.01-0.1度 | 5-10 | 城市内POI密集 |
| 全国城市 | 2.0-5.0度 | 2-3 | 城市间距大 |

**eps过小**：所有点变噪音（label=-1）
**eps过大**：所有点归一簇

## 空间约束聚类注意事项

- 使用`sklearn.cluster.AgglomerativeClustering` + `connectivity=Queen权重稀疏矩阵`
- Queen权重可能有islands（孤立区域），导致connectivity不连通
- islands处理：预处理时将islands连接到最近邻居，或改用KNN权重
- n_clusters建议3-7，超过7容易碎片化

## OD Flow硬约束

- n_zones ≤ 50（超过拒绝并提示聚合）
- top_k ≤ 20（超过自动截断）
- 输入验证：origins/destinations/flows长度必须一致

## causal_check检验逻辑

1. **样本量**：处理组≥15 且 对照组≥15 → 通过
2. **空间自相关**：Moran's I p>0.05 → 无空间溢出，标准模型可用
3. **共同支撑**：SMD<0.25 → 处理组/对照组协变量重叠良好
   - 无协变量时跳过，返回提示
   - SMD = |mean_treat - mean_ctrl| / pooled_std

## 与L2工具的复用关系

- `causal_prelim_check`内部调用`esda.Moran`（与`geo_moran_global`相同计算）
- `cluster_spatial_constrained`内部调用`libpysal.weights.Queen`（与`geo_spatial_weights`相同）
- 不直接调用L2 MCP工具——避免MCP→MCP嵌套调用

## ADR记录

| ADR | 决策 | 理由 |
|-----|------|------|
| L3-1 | Ripley's K砍掉 | L2 Moran/LISA/Gi*已覆盖聚集/分散，K函数多尺度价值对LLM场景不刚需 |
| L3-2 | geo_od_flow保留 | 通用L3推理层有存在价值，硬约束n≤50+top_k≤20 |
| L3-3 | cluster_detect双模式 | 点→DBSCAN，面→空间约束聚类，按几何类型自动选择 |
| L3-4 | causal_check轻量版 | 3个快速检验，平行趋势走离线（需面板拟合，计算量大） |
| L3-5 | 6步决策链 | 砍Ripley's K后紧凑化 |
| L3-6 | 叙事自动触发 | 发现显著空间模式时自动触发，非每次都生成 |
| L3-7 | references精简5个 | DID+RDD+IV+叙事+自检，插值/KDE/Ripley's K砍掉 |
| L3-8 | L2→L3自动触发 | L2显著模式(moran p<0.05/LISA显著/Gi*显著)自动进入L3 |
| L3-9 | covariates可选 | 有则算SMD，无则跳过并提示 |
