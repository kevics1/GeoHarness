# geo-mcp-server 运维参考

## GeoHarness MCP Server 配置方式

GeoHarness 通过 `~/.geoharness/config.yaml` 管理 MCP Server 配置。

### geo-mcp-server 配置

```yaml
mcp:
  geo-mcp-server:
    command: python
    args: ["-m", "geo_mcp_server"]
    env:
      AMAP_API_KEY: "${AMAP_API_KEY}"
      PYTHONUTF8: "1"
```

### 其他工具配置格式

```json
"geo-mcp-server": {
  "command": "python",
  "args": ["-m", "geo_mcp_server"],
  "env": {
    "AMAP_API_KEY": "<key>",
    "PYTHONUTF8": "1"
  },
  "timeout": 120000,
  "disabled": false
}
```

## MCP超时配置

MCP超时是**客户端**设置的，不是server端。默认通常30秒，GWR等计算密集工具需要更长。

建议geo-mcp-server的timeout：**120000ms（2分钟）**，覆盖大多数GWR场景。

## Skills 位置

GeoHarness 的 skills 位于项目目录 `skills/` 下：

```
skills/
├── geo-perception/
│   ├── SKILL.md
│   ├── references/       (6个文件)
│   ├── templates/        (1个文件)
│   └── plugins/air-pollution/
│       ├── SKILL.md
│       └── references/   (4个文件)
├── geo-comprehension/
│   ├── SKILL.md
│   └── references/       (7个文件，含本文件)
└── geo-reasoning/
    ├── SKILL.md
    └── references/
```

Skills是纯知识文件，不依赖MCP Server也能加载——只是没有计算工具时只能指导推理。

## geo-mcp-server 工具网络依赖

| 工具 | 网络需求 | 说明 |
|------|----------|------|
| geo_geocode | **在线** | 高德API |
| geo_reverse_geocode | **在线** | 高德API |
| geo_bbox_from_places | **在线** | 高德API |
| 其余13个工具 | **纯本地** | Shapely/pyproj/esda/mgwr |

GWR等计算超时是**本地计算瓶颈**，不是网络问题。

## 已知问题与修复

### 权重JSON传递超时（已修复）

**问题**：L2工具(geo_moran_global/local/getis_ord)原设计要求先调`geo_spatial_weights`获取序列化权重JSON，再传入统计工具。5个点尚可，300+区域权重JSON可达数百KB，MCP通信超时。

**修复**：三个L2工具新增`geometries`参数，直接传坐标/几何，内部自动构建权重。`weights`参数保留为fallback。

**调用方式对比**：

| 之前（超时风险） | 之后（推荐） |
|---|---|
| ① 调geo_spatial_weights拿权重JSON | ① 直接调geo_moran_global |
| ② 把权重JSON传给geo_moran_global | 传 values + geometries + weight_type |
| ③ 把权重JSON再传给geo_getis_ord | 一步到位，无需中间步骤 |

### GWR超时问题（已修复）

**问题**：mgwr的`Sel_BW`带宽自动搜索耗时20-30秒（即使n=50），加上GWR fit耗时9-10秒，总计30秒+超出MCP默认超时。小带宽(如bw=16)导致mgwr内部迭代次数暴增——bw=20只需0.07秒，bw=16却需25秒。

**修复**（三层）：
1. **n<30**：跳过GWR，返回OLS fallback（全局系数+R²+说明），0.01秒完成
2. **n≥30**：跳过`Sel_BW`搜索，用经验带宽 `bw=max(n*0.6, 30)`，避免自动搜索的20-30秒开销
3. **用户指定bw**：直接使用用户给的带宽值（如`bandwidth="20"`），无需搜索

### Queen/Rook权重对点数据（已修复）

**问题**：Queen/Rook邻接需要多边形共享边界，对点数据触发Voronoi镶嵌——5个点构建Queen权重耗时8秒。

**修复**：`_build_or_rebuild_weights`检测点数据（无Polygon/MultiPolygon），自动将Queen/Rook降级为KNN（k=min(5,n-1)），构建耗时<0.01秒。

### 小样本permutations陷阱（已修复）

**问题**：esda的`G_Local`和`Moran_Local`，`permutations<99`触发慢代码路径（5点49次置换反而37秒，199次0.04秒）。

**修复**：
- n<30：用解析p值（`permutations=0`），0.002秒完成
- n≥30：`permutations` clamp到≥99，避免慢路径
- `permutations=0`时esda无`p_sim`属性：G_Local用`p_norm`，Moran_Local用scipy.stats.norm从z-score计算p值

返回JSON增加`inference`字段：`"analytic"`(perm=0) 或 `"permutation"`(perm≥99)

### esda 2.7 API陷阱

- `Moran_Local` 不支持 `alternative` 参数
- `G_Local` 用 `star=0.5` 而非 `star=True`（row-standardized权重下）
- LISA象限编码：1=HH, 2=LH, 3=LL, 4=HL（非HH→HL→LH→LL）
- libpysal: `use_index=False` 避免FutureWarning; `w.islands` 非 `w.isolates`
