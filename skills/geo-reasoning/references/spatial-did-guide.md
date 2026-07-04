# 空间DID (Spatial Difference-in-Differences) 离线指南

> 适用条件：面板数据 + 政策干预 + 处理组/对照组
> 前置：geo_causal_check确认前提满足

## 决策流程

```
1. 确认面板数据结构（T≥2期，N≥30单元）
2. geo_causal_check检验前提
3. 选择DID变体：
   ├─ 标准DID：无空间溢出
   ├─ 空间DID (SDID)：含空间滞后项
   └─ 多期DID：渐进处理（staggered adoption）
4. 估计 + 稳健性检验
5. 结果回填MCP工具验证
```

## 代码模板1：标准空间DID

```python
import numpy as np
import pandas as pd
from libpysal.weights import Queen
from spreg import GM_Lag

# 数据准备：面板数据需有 id, time, y, treat, post, covariates
# df = pd.read_csv("your_panel_data.csv")

# 构建空间权重
w = Queen.from_dataframe(gdf)  # gdf = GeoDataFrame with geometry
w.transform = 'r'

# 标准DID
df['did'] = df['treat'] * df['post']
did_result = smf.ols('y ~ did + treat + post + cov1 + cov2', data=df).fit(cov_type='cluster', cov_kwds={'groups': df['id']})

# 空间DID (含空间滞后)
# 需要spreg库
# y_vec = df['y'].values
# X = df[['did', 'treat', 'post', 'cov1']].values
# sdid = GM_Lag(y_vec, X, w=w, name_y='y', name_x=['did', 'treat', 'post', 'cov1'])
```

## 代码模板2：多期DID (Staggered DID)

```python
# 使用Callaway-Sant'Anna估计量（避免负权重问题）
# pip install did

from did import DID

# 数据格式：unit, time, treat_time(首次处理期), y, covariates
# treat_time = inf 表示从未处理
did_cs = DID(
    y='y',
    unit='unit',
    time='time',
    treat='treat_time',
    data=df,
    covariates=['cov1', 'cov2']
)
att = did_cs.fit()
print(att.summary())
```

## 稳健性检验清单

| 检验 | 方法 | 通过标准 |
|------|------|----------|
| 平行趋势 | 事件研究法 | 干预前系数不显著 |
| 安慰剂检验 | 随机分配处理 | DID系数不显著 |
| 空间溢出 | 残差Moran's I | p>0.05 |
| 排除特定区域 | 逐个排除 | 结论稳健 |

## MCP协作

- **前置**：`geo_causal_check` → 确认前提
- **后置**：`geo_moran_global` → 检验残差空间自相关
- **后置**：`geo_semantic_label` → 标注因果发现

## 常见陷阱

1. **负权重问题**：多期DID中，双向固定效应可能赋予已处理单元负权重 → 用Callaway-Sant'Anna
2. **空间溢出**：处理效应跨边界扩散 → 标准DID低估真实效应，需空间DID
3. **时间趋势差异**：处理组/对照组趋势不同 → 平行趋势检验必须通过
4. **MAUP**：DID结果随空间聚合尺度变化 → 多尺度稳健性检验
