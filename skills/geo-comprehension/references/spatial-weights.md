# 空间权重矩阵选择指南

## 权重类型

| 类型 | 定义 | 适用数据 | 参数 |
|------|------|----------|------|
| Queen | 共享边界或顶点 | 面状（行政区） | 无 |
| Rook | 仅共享边界（非顶点） | 面状（规则网格） | 无 |
| KNN | 最近K个邻居 | 点状/面状 | k值 |
| Distance Band | 阈值内所有邻居 | 点状 | 阈值距离 |
| Kernel | 距离衰减函数 | 连续影响 | 带宽+核函数 |

## 选择决策树

```
数据是面状？
├─ YES → 边界是否规则（网格）？
│        ├─ YES → Rook
│        └─ NO → Queen（最常用）
└─ NO（点状）→ 需要距离衰减？
         ├─ YES → Kernel
         └─ NO → 样本量？
                  ├─ 大(>200) → KNN(k=5-8)
                  └─ 小(<200) → Distance Band
```

## 关键参数

### KNN的k值选择
- 经验值：k = √n（n为样本量）的整数部分
- 常用范围：4-8
- 过小(k<3)：邻居不足，局部估计不稳定
- 过大(k>12)：过度平滑，丢失局部特征

### Distance Band阈值
- 基于最小距离：确保每个单元至少有1个邻居
- 基于平均最近邻距离的1.5-2倍
- 检查：孤立单元(islands)数量应为0

## 权重标准化

| 标准化 | 代码 | 含义 |
|--------|------|------|
| 行标准化 | w.transform = "r" | 每行权重和=1，最常用 |
| 二值 | w.transform = "b" | 邻居=1，非邻居=0 |

**行标准化**是默认选择，使Moran's I等指标可比。

## 孤立单元处理

孤立单元（islands）= 无任何邻居的单元
- 检查：`w.islands`（注意不是`w.isolates`）
- 处理：改用KNN确保连通，或删除孤立单元
- 影响：孤立单元的局部指标无意义

## MCP工具使用

```
geo_spatial_weights(
  geometries='[WKT1, WKT2, ...]',
  weight_type="queen",    # queen/rook/knn/distance_band
  k=5,                    # KNN参数
  use_index=False         # 消除FutureWarning
)
→ {weights: [...], islands: [], n_neighbors: [...]}
```
