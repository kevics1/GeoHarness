# PostgreSQL MCP 启动修复与验收（2026-10-09）

## 原因与修复

原配置使用 `python -m postgres_mcp_server`，本机该模块不存在，直接执行复现
`No module named postgres_mcp_server`。现有 PG 参数与原生 PostGIS DSN 的主机、端口、
数据库、用户、密码五项一致；原生只读查询成功，PostGIS 扩展存在。

复用官方 `postgres-mcp==0.3.0`，安装到独立的
`~/.geoharness/servers/postgres-mcp` Python 3.12 环境，未改变 GIS 主环境的依赖。
发布包未限定 MCP 上限，首次安装选中 2.3.0，实际 `--help` 复现 FastMCP 导入失败；
限定 `mcp>=1.25,<2` 后使用 1.30.0，实际命令正常。官方主干的依赖限制和入口见
[项目 pyproject.toml](https://github.com/crystaldba/postgres-mcp/blob/main/pyproject.toml)。

本机全局 YAML 仅修改 postgres 的 command、args，并增加 DATABASE_URI、PGCONNECT_TIMEOUT。
真实入口为独立环境的 `Scripts/postgres-mcp.exe`，使用 `--access-mode=restricted`，
DATABASE_URI 引用已有 `${GEOH_POSTGIS_DSN}`。其余配置和凭证不变；原配置在同目录备份。
安装方法与可复用配置见 [使用指南](../user-guide.md)。

## 诊断保护

MCP 启动错误、TUI 异常与审图持久化记录增加 URI 敏感值识别。MCP 诊断先按长度降序
遮蔽完整连接串，再遮蔽密码，避免重叠值破坏整体匹配。新增参数化回归先复现三处
URI 遗漏及密码先替换的缺陷，修复后全部通过。

## 实际验收

- MCP 握手约 15.53 秒，发现 9 个数据库工具。
- 通过 MCP execute_sql 查询常量、事务状态与 pg_extension：connection_ok=1，
  transaction_read_only=on，postgis_present=true；未执行数据库写入。
- 本轮相关 pytest 88 passed；ruff 全仓检查通过；独立只读审查指出的重叠值问题已关闭。
- 构建并安装全局 GeoHarness wheel，66 个 Python 模块逐一与工作区源码一致。
- 从 `F:/Desktop/Project/GeoHarness/example/test` 实际执行 `geoh run --print`：
  Geo MCP 与 Postgres MCP 均完成握手，主模型正常回复并 exit 0。
- `geoh dry-run` 组装成功，15 个原生/辅助工具；该命令本身不连接 MCP。
- 主模型仍为 `cbcn/deepseek-v4.1-flash(max)`；工作区未创建 `.openharness`。

Windows 的 `Signal handling not supported on Windows` 是外部服务对 Unix 信号处理的提示，
本机已在该提示之后完成握手及 SQL 请求。高级索引分析、健康检查及查询统计的所有扩展
能力未逐项验收；本轮结果只证明启动、发现工具和只读查询可用。
