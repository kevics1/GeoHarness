# 空间工具变量 (Spatial Instrumental Variables) 离线指南

> 适用条件：内生性 + 有效工具变量
> 前置：geo_causal_check确认前提满足

## 决策流程

```
1. 识别内生性问题（遗漏变量/双向因果/测量误差）
2. 寻找空间工具变量
3. 检验工具变量有效性（相关性+外生性）
4. 2SLS估计
5. 稳健性检验
```

## 空间工具变量的常见选择

| 内生变量 | 工具变量 | 有效性论证 |
|----------|----------|-----------|
| 经济增长 | 地形起伏度 | 地形影响经济但不直接影响污染（除外生通道） |
| 产业结构 | 历史产业布局 | 历史布局影响当前结构但历史冲击已消化 |
| 人口密度 | 到河流距离 | 河流吸引聚居但河流本身不直接影响空气质量 |
| 交通流量 | 到高速公路距离 | 高速公路规划影响流量但距离本身外生 |

## 代码模板：2SLS空间IV

```python
import numpy as np
import pandas as pd
import statsmodels.api as sm
from linearmodels.iv import IV2SLS

# 数据准备
# y: 因变量（如PM2.5）
# endog: 内生变量（如GDP）
# instrument: 工具变量（如地形起伏度）
# exog: 外生控制变量

# 第一阶段：工具变量 → 内生变量
stage1 = IV2SLS(
    dependent=df['endog'],
    exog=df[['const'] + exog_cols],
    endog=None,
    instruments=None
).fit()

# 检查F统计量 > 10（弱工具变量检验）
print(f"第一阶段F: {stage1.f_statistic}")

# 2SLS估计
iv_result = IV2SLS(
    dependent=df['y'],
    exog=df[['const'] + exog_cols],
    endog=df[['endog']],
    instruments=df[['instrument']]
).fit()

print(iv_result.summary)
```

## 代码模板：空间滞后IV (Spatial Lag IV)

```python
from spreg import GM_Lag_IV

# 当内生性来源是空间滞后项时
# y = ρWy + Xβ + ε
# 工具变量：WX (空间滞后外生变量)

w = Queen.from_dataframe(gdf)
w.transform = 'r'

y = df['y'].values
X = df[['x1', 'x2']].values

# WX作为空间滞后项的工具变量
spatial_iv = GM_Lag_IV(
    y=y, x=X, w=w,
    name_y='y', name_x=['x1', 'x2']
)
print(spatial_iv.summary)
```

## 工具变量有效性检验

| 检验 | 方法 | 通过标准 |
|------|------|----------|
| 弱工具变量 | 第一阶段F统计量 | F>10 |
| 过度识别 | Sargan/Hansen J检验 | p>0.05（不拒绝外生性） |
| 内生性 | Hausman检验 | p<0.05（拒绝外生，需IV） |
| 空间溢出 | 残差Moran's I | p>0.05 |

## MCP协作

- **前置**：`geo_causal_check` → 确认前提（特别是空间自相关检验）
- **后置**：`geo_moran_global` → 检验残差空间自相关
- **后置**：`geo_semantic_label` → 标注因果发现

## 常见陷阱

1. **弱工具变量**：F<10时2SLS偏误比OLS更大 → 换工具变量
2. **排他性约束**：工具变量通过其他通道影响因变量 → 需理论论证+过度识别检验
3. **空间溢出**：工具变量的空间滞后也影响结果 → 用空间滞后IV
4. **有效性vs相关性**：外生性无法统计证明，只能论证 → 需领域知识
