---
name: geo-perception
description: "空间感知认知流程：让LLM获取、理解空间信息。5步决策链实现坐标/距离/方向/几何/拓扑的空间感知。"
version: 0.1.0
author: agent
created_by: agent
metadata:
  hermes:
    tags: [geoai, spatial, perception, gis, coordinate, topology]
---

# 空间感知 (Geo-Perception)

> L1空间感知：从原始地理信号到结构化空间感知结果的认知流程。
> 依赖：geo-mcp-server（MCP工具层）提供计算能力。

## 认知流程（5步决策链）

### Step 1: 空间维度识别

**目标**：判断问题是否涉及空间维度

**判断标准**（满足任一即YES）：
- 包含地名/地点/区域描述
- 涉及距离/方向/范围
- 涉及空间关系（包含/相邻/穿越）
- 涉及空间分布/空间模式
- 涉及尺度/分辨率/比例尺

```
输入：用户问题/事件描述
判断：是否涉及空间维度？
├─ NO → 退出，非空间问题
└─ YES → Step 2
```

### Step 2: 空间实体提取

**目标**：识别并结构化空间实体

**提取要素**：
| 要素 | 示例 | MCP工具 |
|------|------|---------|
| 地名 | 武汉市、长江 | geo_geocode |
| 坐标 | 30.59°N, 114.31°E | 直接使用 |
| 几何 | 点/线/面 | geo_bbox_from_places |
| 范围 | 边界框、行政区 | geo_bbox_from_places |

**输出**：`spatial_entities[]`

### Step 3: 空间关系判定

**目标**：量化实体间空间关系

**关系类型**：
| 类型 | 问题 | MCP工具 |
|------|------|---------|
| 距离 | A距B多远？ | geo_calculate_distance |
| 方向 | A在B的哪个方向？ | geo_calculate_distance (含方位角) |
| 拓扑 | A是否包含B？ | geo_spatial_relation |
| 邻近 | A附近N公里有什么？ | geo_buffer + geo_spatial_relation |

**输出**：`spatial_relations[]`

### Step 4: 尺度锚定

**目标**：确定分析尺度，评估MAUP风险

**尺度层级**：
| 层级 | 粒度 | 典型场景 |
|------|------|----------|
| local | 建筑/街道 | 选址、微环境 |
| urban | 区/街道 | 城市内部差异 |
| regional | 市/省 | 区域对比 |
| national | 省/大区 | 全国格局 |
| global | 国家/大洲 | 全球模式 |

**MAUP风险校验**：
- 聚合粒度是否合理？→ 检查是否有更细粒度数据
- 分区方案是否有偏？→ 检查边界效应
- 尺度是否匹配问题？→ 局部问题用局部尺度

**MCP工具**：geo_scale_analysis

**输出**：`analysis_scale + scale_warning`

### Step 5: 感知输出

**目标**：结构化输出感知结果，传递下游

**输出格式**（JSON）：
```json
{
  "spatial_entities": [
    {
      "name": "string",
      "type": "point|line|polygon|administrative",
      "geometry": "GeoJSON or WKT",
      "crs": "EPSG:xxxx",
      "bbox": [xmin, ymin, xmax, ymax]
    }
  ],
  "spatial_relations": [
    {
      "entity_a": "string",
      "entity_b": "string",
      "relation": "contains|within|crosses|touches|intersects|disjoint|near",
      "distance_km": "number or null",
      "direction": "N|NE|E|SE|S|SW|W|NW or null"
    }
  ],
  "scale": {
    "level": "local|urban|regional|national|global",
    "resolution": "string",
    "maup_risk": "low|medium|high",
    "note": "string"
  },
  "perception_confidence": 0.0-1.0
}
```

**几何格式选择**：
- 简单几何（点、短线）→ WKT（紧凑）
- 复杂几何（多边形、多部件）→ GeoJSON（结构清晰）
- LLM推理用WKT，工具交互用GeoJSON

## 领域扩展插件

| 插件名 | 领域 | 状态 | 加载方式 |
|--------|------|------|----------|
| air-pollution | 空气污染 | active | skill_view |

插件加载：`skill_view(name="geoai/geo-perception", file_path="plugins/<name>/SKILL.md")`

## MCP工具索引

所有计算由 geo-mcp-server 提供：

**位置**：`E:\Administrator\hermes\geo-mcp-server\`

| 工具 | 功能 | 关键参数 | 返回格式 |
|------|------|----------|----------|
| geo_transform_crs | 坐标系转换 | geometry, from_crs, to_crs, output_format | WKT/GeoJSON |
| geo_calculate_distance | 距离+方位角 | point_a, point_b, metric | JSON |
| geo_buffer | 缓冲区分析 | geometry, distance_km, crs, output_format | WKT/GeoJSON |
| geo_bbox | 边界框 | geometry, output_format | JSON/WKT |
| geo_spatial_relation | 拓扑关系判定 | geom_a, geom_b, relation | JSON |
| geo_all_relations | 全部8种拓扑关系 | geom_a, geom_b | JSON |
| geo_geocode | 地名→坐标（高德） | address, city | JSON |
| geo_reverse_geocode | 坐标→地名（高德） | location | JSON |
| geo_bbox_from_places | 批量地名→坐标 | places[] | JSON |
| geo_scale_analysis | 尺度+MAUP分析 | geometry, target_scale | JSON |

**启动方式**：
```powershell
# PowerShell（推荐，run.py自动处理PROJ_LIB和依赖安装）
cd E:\Administrator\hermes\geo-mcp-server
python run.py
```

**环境变量**：
- `AMAP_API_KEY`：地理编码必需，[高德开放平台](https://lbs.amap.com/)申请
- `PROJ_LIB`：run.py自动检测修复，用户无需手动设置

**注意**：
- 高德API返回GCJ-02坐标，与WGS84偏移100-600米
- 几何操作工具返回WKT（默认）或GeoJSON，通过output_format切换
- 距离/拓扑/尺度工具返回JSON字符串
- `python`（3.10）和`python3`（QGIS 3.12）指向不同安装，用`python run.py`

## 使用方式

1. 加载本Skill：`skill_view(name="geoai/geo-perception")`
2. 按5步决策链执行空间感知
3. 需要计算时调用对应MCP工具
4. 输出结构化感知结果

## 陷阱与注意

- **GCJ-02偏移**：高德API返回GCJ-02坐标，与WGS84偏移100-600米。做精确距离/拓扑计算时需先转换坐标系，否则结果有系统性偏差。
- **PROJ_LIB自动修复**：run.py和spatial_engine.py已内置按需检测修复。若pyproj初始化失败，自动调用`pyproj.datadir.set_data_dir()`。用户无需手动设置PROJ_LIB。
- **几何格式选择**：简单几何用WKT（紧凑，LLM友好），复杂几何用GeoJSON（结构清晰）。buffer操作产生的多边形通常较复杂，建议output_format="geojson"。
- **MAUP默认标注**：人口暴露相关问题MAUP风险默认标"high"，除非数据粒度已达区级或更细。
