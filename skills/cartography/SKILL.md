---
name: cartography
description: 制图工作流 — 单图层与多图层合成渲染（PNG/PDF/SVG/HTML），图层顺序、颜色搭配与常见错误。当用户要求制图/出图/地图/态势图/专题图时加载。
---

# 制图工作流（geo_cartography）

## 工具选择

| 需求 | 做法 |
|---|---|
| 单个图层出图 | `action=export` + `source_type=file` + `source_name=图层名` |
| **多图层合成图（态势图/专题图）** | `action=export` + **`layers` JSON 数组**（一次调用，勿逐层调用） |
| 叠加高程/栅格底图 | 加 `basemap_raster='<tif路径>'`（静态 PNG 有效） |
| 仅想了解符号化方案 | `action=symbolize`（需 `layer_name`；`analysis_type` 可省略） |

## 多图层用法（推荐）

```json
geo_cartography(
  action="export",
  output_format="png",
  title="西昌市3·30森林火灾应急态势图",
  layers='[
    {"source_type":"file","source_name":"西昌市行政区划","label":"行政区划","color":"#4c78a8"},
    {"source_type":"file","source_name":"水系","label":"水系","color":"#2b7bba"},
    {"source_type":"file","source_name":"城际公路","label":"城际公路","color":"#333333"},
    {"source_type":"file","source_name":"预测蔓延范围","label":"预测蔓延","color":"#fc8d59"},
    {"source_type":"file","source_name":"当前火场范围","label":"当前火场","color":"#d7301f"},
    {"source_type":"file","source_name":"受威胁居民点","label":"受威胁居民点","color":"#e4572e"},
    {"source_type":"file","source_name":"应急避难场所","label":"应急避难场所","color":"#2ca02c"}
  ]',
  basemap_raster="example/数据/西昌市高程.tif"
)
```

要点：
- **数组顺序 = 绘制顺序（先下后上）**。想突出某图层就把它放后面（后画的盖前面）。
  火场范围应放在预测蔓延之后、点图层之前。
- 每层可选 `label`（图例文字，默认用 source_name）与 `color`（缺省按几何类型配色：
  面=蓝、线=红、点=橙）。
- 层内也支持 `geojson` 内联数据代替 source_name。
- 输出位置：省略 `output_path` 时写入 `~/.geoharness/exports/`（文件名取自
  title/layer_name）。

## 常见错误（勿重蹈）

1. **不要把目录或通配符传给 file_path** —— `数据/*.shp`、`数据/` 均无法渲染。
2. **不要逐图层多次调用 export 再指望自动拼接** —— 每次调用只出一层。
3. **不要用 load 的输出手工拼 GeoJSON** —— load 只返回统计摘要，不含几何坐标。
4. `analysis_type` 仅在空间分析成果制图时需要（如 moran_local）；专题制图直接省略。

## 图层顺序模板（应急态势图）

底 → 顶：栅格底图(basemap_raster) → 行政区划 → 水系 → 道路 → 预测蔓延 →
当前火场 → 受威胁居民点 → 应急避难场所。
