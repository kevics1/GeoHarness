# 空间查询构造模板

## 模板1：距离查询

```
问题：A距B多远？
Step 2: geo_geocode(A), geo_geocode(B) → 坐标
Step 3: geo_calculate_distance(A, B, metric="geodesic") → 距离+方向
Step 4: 尺度锚定（距离本身即尺度指标）
Step 5: 输出
```

## 模板2：包含查询

```
问题：A是否在B内？
Step 2: geo_geocode(A), geo_bbox_from_places(B) → 几何
Step 3: geo_spatial_relation(B_geom, A_geom, "contains") → bool
Step 5: 输出
```

## 模板3：邻近查询

```
问题：A附近N公里有什么？
Step 2: geo_geocode(A) → 坐标
Step 3: geo_buffer(A, N_km) → 缓冲区
        geo_spatial_relation(buffer, targets, "intersects") → 命中实体
Step 4: 尺度锚定（N_km决定分析尺度）
Step 5: 输出
```

## 模板4：区域对比

```
问题：A和B的空间关系？
Step 2: geo_bbox_from_places([A, B]) → 几何
Step 3: geo_calculate_distance(A, B) → 距离+方向
        geo_spatial_relation(A, B, relation) → 拓扑
Step 4: geo_scale_analysis → 尺度+MAUP
Step 5: 输出
```

## 模板5：空间分布描述

```
问题：X在Y区域如何分布？
Step 2: geo_bbox_from_places(Y) → 区域几何
        提取X的空间位置数据
Step 3: 计算X在Y内的分布特征（密度/覆盖/聚集）
Step 4: geo_scale_analysis → 尺度+MAUP
Step 5: 输出（含分布模式初步判断）
```
