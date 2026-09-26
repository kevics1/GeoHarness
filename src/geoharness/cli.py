"""GeoHarness CLI — Typer-based command interface."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from geoharness import __version__

app = typer.Typer(
    name="geoh",
    help="GeoHarness — 地理方向原生 Agent 系统",
    no_args_is_help=True,
)
console = Console()


@app.command()
def version() -> None:
    """显示版本信息。"""
    console.print(f"GeoHarness v{__version__}")


@app.command()
def init(
    force: bool = typer.Option(
        False, "--force", "-f", help="覆盖已存在的配置文件"
    ),
) -> None:
    """初始化 GeoHarness 配置目录 (~/.geoharness/)。"""
    config_dir = Path.home() / ".geoharness"
    config_file = config_dir / "config.yaml"
    env_file = config_dir / ".env"

    config_dir.mkdir(parents=True, exist_ok=True)

    # config.yaml
    if config_file.exists() and not force:
        console.print(
            f"[yellow]配置文件已存在: {config_file}[/yellow]"
            " 使用 --force 覆盖"
        )
    else:
        template = _generate_config_template()
        config_file.write_text(template, encoding="utf-8")
        console.print(f"[green]已创建配置文件: {config_file}[/green]")

    # .env
    if env_file.exists() and not force:
        console.print(
            f"[yellow]环境变量文件已存在: {env_file}[/yellow]"
            " 使用 --force 覆盖"
        )
    else:
        env_template = _generate_env_template()
        env_file.write_text(env_template, encoding="utf-8")
        console.print(f"[green]已创建环境变量文件: {env_file}[/green]")

    # workspaces 目录
    ws_dir = config_dir / "workspaces"
    ws_dir.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]已创建工作空间目录: {ws_dir}[/green]")

    console.print("\n[bold]GeoHarness 初始化完成。[/bold]")
    console.print("请编辑 .env 文件填入 API 密钥和数据库连接信息。")


@app.command()
def dry_run(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="指定配置文件路径"
    ),
) -> None:
    """验证运行时组装（不启动 TUI）。"""
    import asyncio

    from geoharness.runtime import build_geo_runtime

    async def _check() -> None:
        try:
            bundle = await build_geo_runtime(
                cwd=Path.cwd(),
                config_path=config,
            )
            table = Table(title="GeoHarness 运行时验证")
            table.add_column("组件", style="cyan")
            table.add_column("状态", style="green")
            table.add_row("RuntimeBundle", "OK")
            table.add_row("API Client", type(bundle.api_client).__name__)
            table.add_row("ToolRegistry", f"{len(bundle.tool_registry.list_tools())} tools")
            table.add_row("MCP Manager", "connected")
            table.add_row("QueryEngine", type(bundle.engine).__name__)
            console.print(table)
        except Exception as e:
            console.print(f"[red]运行时组装失败: {e}[/red]")
            raise typer.Exit(1)

    asyncio.run(_check())


@app.command()
def run(
    cwd: Optional[Path] = typer.Option(
        None, "--cwd", help="工作目录"
    ),
    prompt: Optional[str] = typer.Option(
        None, "--prompt", "-p", help="初始提示词"
    ),
    print_mode: bool = typer.Option(
        False, "--print", help="使用打印模式（不启动 React TUI）"
    ),
) -> None:
    """启动 GeoHarness 交互式会话（默认 React TUI）。"""
    import asyncio

    from geoharness.launcher import launch_geo_tui

    work_cwd = cwd or Path.cwd()
    exit_code = asyncio.run(
        launch_geo_tui(
            cwd=work_cwd,
            prompt=prompt,
            print_mode=print_mode,
        )
    )
    if exit_code != 0:
        raise typer.Exit(exit_code)


def _generate_config_template() -> str:
    """生成 config.yaml 模板。"""
    return """# GeoHarness 配置文件
# 所有 ${VAR} 引用 .env 中的环境变量

model: ${GEOH_MODEL}
api_key: ${GEOH_API_KEY}
base_url: ${GEOH_BASE_URL}

# MCP 服务器配置
mcp:
  geo-mcp-server:
    command: python
    args: ["-m", "geo_mcp_server"]
    env:
      PYTHONUTF8: "1"
  postgres:
    command: python
    args: ["-m", "postgres_mcp_server"]
    env:
      PGHOST: ${GEOH_POSTGRES_HOST}
      PGPORT: ${GEOH_POSTGRES_PORT}
      PGDATABASE: ${GEOH_POSTGRES_DB}
      PGUSER: ${GEOH_POSTGRES_USER}
      PGPASSWORD: ${GEOH_POSTGRES_PASSWORD}

# 制图配置
cartography:
  outputs_dir: ${GEOH_OUTPUTS_DIR}
  bridge_rules:
    moran_local:
      render_method: categorized
      color_scheme: RdBu
      n_classes: 5
    getis_ord:
      render_method: graduated
      color_scheme: YlOrRd
      n_classes: 6
    cluster_detect:
      render_method: categorized
      color_scheme: Set1
    od_flow:
      render_method: flow
      color_scheme: Blues

# 认知配置
cognition:
  cascade:
    l1_to_l2_tools: ["geo_spatial_relation", "geo_all_relations"]
    l2_to_l3_tools: ["geo_moran_local", "geo_getis_ord"]
    suggestion_prompt: "分析结果已完成，建议加载更高级认知技能以深入理解空间模式。"

# 数据配置
data:
  default_crs: EPSG:4326
  analysis_crs: EPSG:3857
  postgis_dsn: ${GEOH_POSTGIS_DSN}

# 权限配置
permissions:
  allow_write_paths:
    - ~/.geoharness/workspaces/
  deny_paths:
    - ~/.ssh/
    - ~/.gnupg/
"""


def _generate_env_template() -> str:
    """生成 .env 模板。"""
    return """# GeoHarness 环境变量
# LLM API
GEOH_MODEL=deepseek-chat
GEOH_API_KEY=your-api-key-here
GEOH_BASE_URL=https://api.deepseek.com/v1

# PostGIS
GEOH_POSTGIS_DSN=postgresql://user:password@localhost:5432/geodb
GEOH_POSTGRES_HOST=localhost
GEOH_POSTGRES_PORT=5432
GEOH_POSTGRES_DB=geodata
GEOH_POSTGRES_USER=postgres
GEOH_POSTGRES_PASSWORD=postgres

# 制图输出目录（原生渲染的 PNG/PDF/SVG/HTML 落盘位置）
GEOH_OUTPUTS_DIR=~/.geoharness/exports
# 可选：PostGIS / MCP 相关变量
# GEOH_GEO_MCP_PATH=/path/to/geo-mcp-server
"""


if __name__ == "__main__":
    app()
