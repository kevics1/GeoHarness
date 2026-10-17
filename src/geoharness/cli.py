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
            table.add_row("MCP Manager", "configured (not connected)")
            table.add_row("QueryEngine", type(bundle.engine).__name__)
            from geoharness.cartography.review_config import review_configuration
            from geoharness.config.settings import load_geo_config

            review = review_configuration(load_geo_config(config))
            table.add_row("Image reviewer", f"{review['model']} ({review['model_source']})")
            table.add_row("Review contract", review["response_format"])
            table.add_row("Review reasoning", review["reasoning_effort"] or "provider default")
            console.print(table)
        except Exception as e:
            console.print(f"[red]运行时组装失败: {e}[/red]")
            raise typer.Exit(1)

    asyncio.run(_check())


@app.command()
def review_doctor(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="指定配置文件路径"),
    live: bool = typer.Option(False, "--live", help="显式发送一次图片测试请求"),
    image: Optional[Path] = typer.Option(None, "--image", help="检查指定图片，需同时使用 --live"),
    output: Path = typer.Option(
        Path(".geoharness/diagnostics"), "--output", help="独立诊断报告目录，不写地图台账"),
) -> None:
    """查看独立审图配置；--live 测试图片能力，--live --image 审查实际图片。"""
    import asyncio
    import json

    from pydantic import ValidationError

    from geoharness.cartography.review_config import review_configuration
    from geoharness.cartography.review_doctor import run_review_probe
    from geoharness.config.settings import load_geo_config, resolve_config_path

    if image is not None and not live:
        raise typer.BadParameter("--image 需要 --live；默认配置检查不发送图片。")
    try:
        settings = load_geo_config(config)
        summary = review_configuration(settings)
        summary["config_path"] = str(resolve_config_path(config).resolve())
        console.print(
            json.dumps(summary, ensure_ascii=False, indent=2), markup=False, soft_wrap=True)
        if summary["configuration_errors"]:
            raise typer.Exit(1)
        if live:
            result = asyncio.run(run_review_probe(settings, output.resolve(), image))
            # Full findings and diagnostics stay in the report, keeping CLI output bounded.
            keys = ("status", "probe_kind", "review_completed", "image_capability_verified",
                    "business_quality_verified", "report_path")
            console.print(json.dumps({key: result[key] for key in keys},
                                     ensure_ascii=False, indent=2), markup=False, soft_wrap=True)
            if result["status"] != "succeeded":
                raise typer.Exit(1)
    except ValidationError as exc:
        details = [{"field": ".".join(str(part) for part in error["loc"]),
                    "type": error["type"]} for error in exc.errors(
                        include_input=False, include_context=False, include_url=False)]
        console.print(json.dumps({"configuration_errors": details}, ensure_ascii=False),
                      markup=False, soft_wrap=True)
        raise typer.Exit(1) from exc
    except (OSError, ValueError) as exc:
        # Configuration validation can contain private input values in exception messages.
        console.print(f"审图诊断无法完成 ({type(exc).__name__})；请检查配置及图片路径。")
        raise typer.Exit(1) from exc


@app.command()
def qgis_doctor(
    config: Optional[Path] = typer.Option(None, "--config", "-c", help="指定配置文件路径"),
) -> None:
    """实际检查 QGIS 环境和可用 Processing 提供者（不打开桌面）。"""
    import asyncio
    import json

    from geoharness.config.settings import load_geo_config
    from geoharness.qgis.runner import execute_qgis

    settings = load_geo_config(config).qgis
    if not settings.enabled:
        console.print("[yellow]QGIS 集成已禁用。[/yellow]")
        raise typer.Exit(1)
    try:
        result = asyncio.run(execute_qgis("doctor", {}, cwd=Path.cwd(), settings=settings))
    except (OSError, ValueError, RuntimeError) as exc:
        console.print(f"[red]QGIS 检查失败: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(json.dumps(result, ensure_ascii=False, indent=2), markup=False)
    if result.get("status") != "succeeded":
        raise typer.Exit(1)


@app.command()
def run(
    config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="指定配置文件路径"
    ),
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
            config_path=config,
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

agent:
  max_tokens: 16384
  internal_todo_path: .geoharness/tasks/TODO.md

# 可选 MCP 扩展：安装服务后按其实际入口填写 command、args、cwd 和 env。
# 默认使用原生空间分析与数据库工具，避免启动未安装的模块。
mcp: {}

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

# 原生空间分析（无需 MCP；metric_crs 为空时选择适用的本地 UTM）
analysis:
  outputs_dir: .geoharness/analysis
  metric_crs: ""
  timeout_seconds: 120

# 自主 Python 分析：源数据默认只读，输出限于单次运行目录
execution:
  enabled: true
  outputs_dir: .geoharness/runs
  timeout_seconds: 120
  max_output_chars: 12000

# 独立 QGIS 环境；留空自动发现，或在 install_dir 设置实际安装目录
qgis:
  enabled: true
  install_dir: ""
  python_executable: ""
  prefix_path: ""
  outputs_dir: .geoharness/qgis
  timeout_seconds: 180
  max_output_chars: 12000
  max_search_results: 20

# 独立图片审查；直接填写下列字段，留空的 model/base_url/api_key 各自复用主配置。
map_review:
  enabled: true
  model: ""
  base_url: ""
  api_key: ""
  reasoning_effort: null  # null 不强制思考参数；max 仅用于支持它的审图模型。
  request_extra_body: {}
  response_format: tool  # tool: 函数调用；json: JSON文本（网关需支持json_object）。
  timeout_seconds: 180
  read_timeout_seconds: 90
  max_tokens: 8192
  layout:
    legend_outside: true

# 最终交付目录必须位于当前工作目录内
delivery:
  outputs_dir: exports

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
GEOH_OUTPUTS_DIR=exports
# 可选：PostGIS / MCP 相关变量
# GEOH_GEO_MCP_PATH=/path/to/geo-mcp-server
"""


if __name__ == "__main__":
    app()
