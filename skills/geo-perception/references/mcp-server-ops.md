# geo-mcp-server 运维参考

## MCP Server 配置方式

GeoHarness 通过 `~/.geoharness/config.yaml` 管理 MCP Server 配置。

### 配置格式（GeoHarness config.yaml）

```yaml
mcp:
  geo-mcp-server:
    command: python
    args: ["-m", "geo_mcp_server"]
    env:
      AMAP_API_KEY: "${AMAP_API_KEY}"
      PYTHONUTF8: "1"
```

### 配置格式（其他兼容工具）

```json
"geo-mcp-server": {
  "command": "python",
  "args": ["-m", "geo_mcp_server"],
  "env": { "AMAP_API_KEY": "...", "PYTHONUTF8": "1" },
  "disabled": false
}
```

## Skills 位置

GeoHarness 的 skills 位于项目目录 `skills/` 下，包含 geo-perception/、geo-comprehension/、geo-reasoning/。Skills 是纯知识文件，不依赖 MCP Server 也能加载。

## L2工具权重传递（重要）

L2统计工具(moran/getis_ord)优先传`geometries`参数，内部自动构建权重。避免传`weights` JSON——序列化权重在LLM↔MCP间来回传输浪费token，大数据集会超时。详见 geo-comprehension skill。
