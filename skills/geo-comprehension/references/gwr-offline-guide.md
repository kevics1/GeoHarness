# GWR 离线分析指南

> GWR（地理加权回归）已从MCP工具中移除，因其计算耗时16-30秒，违反MCP快速响应设计原则。
> 请在本地Python/Jupyter环境中完成GWR分析，本指南提供完整的决策流程和代码模板。

## 为什么GWR不在MCP中？

| 对比维度 | MCP工具 | GWR |
|----------|---------|-----|
| 响应时间 | <2秒 | 16-30秒 |
| 调用模式 | 单次问答 | 批处理分析 |
| 数据规模 | 轻量参数 | 完整数据集(n≥30) |
| 适用场景 | LLM实时推理 | 研究者离线探索 |

GWR需要研究者反复调整带宽、解释局部系数、迭代验证——这些需要**人类判断力**而非LLM推理。

## 决策流程

```
空间关系是否随位置变化？ → 不确定 → 运行GWR
                                    ↓
                              数据量 ≥30？
                          ├─ 否 → 用OLS，记录局限性
                          └─ 是 → 按下方模板运行GWR
                                    ↓
                              选带宽策略：
                          ├─ 探索性 → Sel_BW自动搜索（慢但最优）
                          └─ 快速 → 经验规则 bw = n×0.6（秒回）
                                    ↓
                              解读局部系数：
                          ├─ 空间非平稳 → 变量关系因地而异
                          └─ 空间平稳 → 全局OLS足够
```

## 代码模板

### 基础GWR（经验带宽，秒回）

```python
import numpy as np
import geopandas as gpd
from mgwr.gwr import GWR

# 1. 准备数据
gdf = gpd.read_file("your_data.geojson")  # 或从CSV构建
coords = np.array([(g.centroid.x, g.centroid.y) for g in gdf.geometry])
y = gdf["target_var"].values.reshape(-1, 1)
X = gdf[["covariate1", "covariate2"]].values
X = np.column_stack([np.ones(X.shape[0]), X])  # 加截距

# 2. 设置带宽（经验规则：60%的样本量）
n = len(y)
bw = max(int(n * 0.6), 30)

# 3. 运行GWR
model = GWR(coords, y, X, bw=bw, kernel="bisquare", fixed=False)
results = model.fit()

# 4. 输出结果
print(f"Global R²: {results.R2:.4f}")
print(f"Bandwidth: {bw}")
print(f"Local R² range: {results.localR2.min():.4f} — {results.localR2.max():.4f}")

# 5. 保存局部系数到GeoDataFrame
gdf["local_R2"] = results.localR2
for i, name in enumerate(["intercept"] + [f"x{j+1}" for j in range(X.shape[1]-1)]):
    gdf[f"coef_{name}"] = results.params[:, i]
gdf.to_file("gwr_results.geojson", driver="GeoJSON")
```

### 带宽自动搜索（慢但最优）

```python
from mgwr.sel_bw import Sel_BW

selector = Sel_BW(coords, y, X, kernel="bisquare", fixed=False)
bw = selector.search()  # 耗时20-30秒
print(f"Optimal bandwidth: {bw}")

model = GWR(coords, y, X, bw=bw, kernel="bisquare", fixed=False)
results = model.fit()
```

### 显著性检验

```python
# 哪些区域的系数显著异于零？
t_vals = results.filter_tvals(alpha=0.05)
for i, name in enumerate(["intercept"] + [f"x{j+1}" for j in range(X.shape[1]-1)]):
    sig_count = np.sum(t_vals[:, i] != 0)
    print(f"  {name}: {sig_count}/{n} 区域显著")
```

## 常见陷阱

1. **小样本(n<30)**：GWR不稳定，带宽选择可能报错。先用OLS。
2. **带宽太小**：mgwr迭代次数暴增，计算时间爆炸。经验规则bw=n×0.6是安全下限。
3. **共线性**：GWR对局部共线性敏感，检查VIF>10的变量。
4. **坐标系统**：确保坐标是投影坐标系（UTM），不是WGS84经纬度。GWR基于距离计算。
5. **缺失值**：mgwr不处理NaN，需提前清洗。

## 与MCP工具的协作

GWR分析前，可用MCP工具完成前置步骤：

1. `geo_moran_global` → 判断全局空间自相关是否存在
2. `geo_moran_local` → LISA识别HH/LL聚类区域
3. `geo_spatial_weights` → 获取权重矩阵结构
4. `geo_scale_analysis` → 确定分析尺度级别

GWR分析后，可用MCP工具补充：

1. `geo_semantic_label` → 为GWR发现标注语义（如"系数空间非平稳"→"关系因地而异"）
2. `geo_buffer` → 为显著区域生成影响范围
