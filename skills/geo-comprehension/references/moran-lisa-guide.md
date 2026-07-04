# Moran's I 与 LISA 原理与解读

## 全局 Moran's I

### 公式
```
I = (n / S₀) × ΣᵢΣⱼ wᵢⱼ(xᵢ - x̄)(xⱼ - x̄) / Σᵢ(xᵢ - x̄)²
```
- n: 观测数
- wᵢⱼ: 空间权重
- S₀ = ΣᵢΣⱼ wᵢⱼ: 权重矩阵总和
- x̄: 均值

### 取值范围
- I ∈ [-1, 1]（近似，实际范围取决于权重矩阵）
- I → 1: 完美正空间自相关
- I → -1: 完美负空间自相关
- I ≈ E[I] = -1/(n-1): 零假设（随机）

### 显著性检验
- 随机化检验（permutations）：打乱值999/9999次，构建参考分布
- z-score = (I - E[I]) / √Var(I)
- p < 0.05: 拒绝H₀，存在显著空间自相关

### 解读规则
| I值 | p值 | 结论 | 空间含义 |
|-----|-----|------|----------|
| I显著>0 | p<0.05 | 正自相关 | 相似值聚集（高-高、低-低） |
| I显著<0 | p<0.05 | 负自相关 | 相异值交替（高-低、低-高） |
| I不显著 | p>0.05 | 随机 | 无空间模式 |

## 局部 Moran's I (LISA)

### 公式
```
Iᵢ = (xᵢ - x̄) / m₂ × Σⱼ wᵢⱼ(xⱼ - x̄)
m₂ = Σⱼ(xⱼ - x̄)² / n
```

### 象限分类（Moran散点图）

| 象限 | 编码esda.q | 含义 | 散点图位置 | 俗称 |
|------|-----------|------|-----------|------|
| Q1 | 1 | 高值被高值包围 | 右上 | 热点/HH |
| Q2 | 2 | 低值被高值包围 | 左上 | 异常低/LH |
| Q3 | 3 | 低值被低值包围 | 左下 | 冷点/LL |
| Q4 | 4 | 高值被低值包围 | 右下 | 异常高/HL |

**注意**：esda象限编码顺序是 HH→LH→LL→HL，不是 HH→HL→LH→LL！

### 显著性
- 每个单元独立检验
- p < 0.05 的单元才标注聚类类型
- 不显著单元标记为"NS"（Not Significant）

### LISA聚类地图解读
1. **HH聚集**：核心高值区，政策关注重点
2. **LL聚集**：低值稳定区，可作为对照
3. **HL异常**：高值孤岛，局部特殊因素
4. **LH异常**：低值洼地，可能受高值区溢出影响

## API陷阱

### esda Moran_Local alternative参数
```python
# esda 2.x 默认 alternative='directed'（单侧），未来将改为 'two-sided'
# 单侧p值约为双侧的一半
lisa = esda.Moran_Local(y, w, permutations=999, alternative='two-sided')
```

### G_Local star参数与行标准化
```python
# 行标准化权重 + star=True 会触发 UserWarning
# Gi*需要自权重，行标准化后自动推断
gi = esda.G_Local(y, w, permutations=999, star=True)
# 消除警告：显式设置 star 值
gi = esda.G_Local(y, w, permutations=999, star=0.5)
```

## MCP工具使用

```
geo_moran_global(
  values="[35, 42, 80, ...]",
  weights="{...}"  # geo_spatial_weights输出
)
→ {I: 0.45, p_value: 0.001, z_score: 3.2, pattern: "clustered"}

geo_moran_local(
  values="[35, 42, 80, ...]",
  weights="{...}",
  permutations=999
)
→ {quadrants: [1,3,1,4,...], p_values: [...], labels: ["HH","LL",...]}
```
