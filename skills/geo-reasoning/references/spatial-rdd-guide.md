# 空间断点设计 (Spatial Regression Discontinuity) 离线指南

> 适用条件：地理边界 + 连续性假设
> 前置：geo_causal_check确认前提满足

## 决策流程

```
1. 识别空间断点（行政边界/自然边界/政策边界）
2. 检验连续性假设（边界两侧协变量是否连续）
3. 选择带宽（MSE最优 vs CCT）
4. 估计局部处理效应
5. 稳健性检验
```

## 代码模板1：基础空间RDD

```python
import numpy as np
import pandas as pd
from rdrobust import rdrobust, rdbwselect

# 数据准备：每个观测需有
# - y: 结果变量
# - distance: 到边界的距离（正=处理侧，负=对照侧）
# - covariates: 协变量

# 带宽选择
bw = rdbwselect(y=df['y'], x=df['distance'])
print(f"最优带宽: {bw.bws}")

# RDD估计
rdd = rdrobust(y=df['y'], x=df['distance'], covs=df[['cov1', 'cov2']])
print(rdd)
```

## 代码模板2：二维空间RDD

```python
# 边界不是单一截断点，而是空间曲线
# 方法：计算每个观测到边界的最短距离

from shapely.geometry import Point, LineString
import numpy as np

def distance_to_boundary(geom, boundary_line):
    """计算点到边界的最短距离，处理侧为正"""
    pt = Point(geom.x, geom.y) if geom.geom_type == 'Point' else geom.centroid
    dist = pt.distance(boundary_line)
    # 判断在哪一侧（用边界法向量）
    # 简化：用经度差判断东西侧
    return dist if pt.x > boundary_line.centroid.x else -dist

# 构建距离变量
boundary = LineString([(116.0, 30.0), (116.0, 32.0)])  # 示例：经度116度线
df['distance'] = gdf.geometry.apply(lambda g: distance_to_boundary(g, boundary))

# 然后用rdrobust估计
```

## 连续性假设检验

```python
# 检验边界两侧协变量是否连续
# McCrary密度检验：边界处人口密度是否连续
from rdrobust import rddensity

density_test = rddensity(x=df['distance'])
print(density_test)

# 协变量连续性检验
for cov in ['cov1', 'cov2']:
    result = rdrobust(y=df[cov], x=df['distance'])
    print(f"{cov}: coef={result.coef[0]:.4f}, p={result.pv[0]:.4f}")
    # p>0.05 → 连续性假设成立
```

## 稳健性检验清单

| 检验 | 方法 | 通过标准 |
|------|------|----------|
| 密度连续性 | McCrary检验 | 边界处密度无跳跃 |
| 协变量连续性 | RDD on covariates | 系数不显著 |
| 带宽敏感性 | 多带宽估计 | 结论方向一致 |
| 安慰剂边界 | 虚假边界 | 处理效应不显著 |
| 空间溢出 | 边界附近Moran's I | 无显著空间自相关 |

## MCP协作

- **前置**：`geo_causal_check` → 确认前提
- **前置**：`geo_spatial_relation` → 确认边界关系
- **后置**：`geo_moran_global` → 检验残差空间自相关

## 常见陷阱

1. **边界不精确**：行政边界与实际政策边界不一致 → 用政策实际执行边界
2. **空间溢出**：边界两侧存在交互 → 缓冲区排除边界附近观测
3. **选择性迁移**：个体为获取处理而跨边界迁移 → McCrary密度检验
4. **带宽选择**：过小→方差大，过大→偏误大 → 用CCT最优带宽
