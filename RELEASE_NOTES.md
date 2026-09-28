# GeoHarness v0.1.0 — 首个公开版本

地理方向原生 Agent 系统：空间分析优先，制图为分析产物。基于 OpenHarness 构建，通过直接构造 RuntimeBundle 实现 8 层配置隔离，不修改上游源码。

## 亮点

### 三源独立数据工具
- `geo_db_data` — PostGIS 数据库（list / inspect / load）
- `geo_vector_data` — 本地矢量 SHP/GeoJSON/GPKG/CSV
- `geo_raster_data` — 本地栅格 GeoTIFF/IMG/ASC/VRT（rasterio，波段统计/nodata 掩膜）

每个工具只访问自己的后端：数据库查询不会被文件扫描或网络 API 拖累。

### 多图层合成制图
一次调用将行政区划、水系、道路、火场、居民点等多层叠加到一张图：

```json
geo_cartography(
  action="export", output_format="png",
  title="西昌市3·30森林火灾应急态势图",
  layers='[{"source_type":"file","source_name":"西昌市行政区划","label":"行政区划","color":"#4c78a8"}, ...]',
  basemap_raster="西昌市高程.tif"
)
```

- 支持静态 PNG/PDF/SVG 与交互式 HTML（folium/Leaflet）
- 自动统一 CRS（矢量重投影到栅格底图坐标系）
- 画布范围裁剪到底图足迹；按几何类型自动配色 + 图层图例

### 无 QGIS 依赖的原生渲染
matplotlib（Agg 后端，无头安全）+ folium，纯 Python 出图。

### 生产级鲁棒性
- 逐工具超时预算（DB 30s / 文件与栅格 60s / 渲染 300s）
- PostGIS：连接串行化（RLock）+ TCP keepalive + 断线重连 + statement_timeout
- 目录扫描单飞锁 + 详情缓存（TTL）——并行 inspect 不再蜂拥
- PROJ 环境自愈（自动回退 rasterio 自带 proj.db，修复 PostgreSQL 旧版 proj.db 污染）
- 智能体轮次上限默认 40（`GEOH_MAX_TURNS` 可覆盖）
- **371 项自动化测试**，ruff 全绿

### 认知级联
L1 感知 → L2 理解 → L3 推理，建议制（模型保留跳过权限）。

## 安装

```bash
git clone https://github.com/kevics1/GeoHarness.git
cd GeoHarness
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"   # Windows
.venv\Scripts\geoh version              # GeoHarness v0.1.0
```

详见 [README](README.md)。

## 依赖

Python ≥ 3.10；geopandas / rasterio / matplotlib / folium / psycopg 随依赖自动安装。PostGIS 可选（无数据库时仅用文件数据源）。

## 许可证

MIT
