"""GeoHarness configuration — YAML + .env driven, fully isolated from OpenHarness."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, field_validator


class AnalysisConfig(BaseModel):
    """Native analysis settings; an empty metric CRS selects a local UTM CRS."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    outputs_dir: str = ".geoharness/analysis"
    metric_crs: str = ""
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    max_output_chars: int = Field(default=12000, ge=6000, le=12000)


class ExecutionConfig(BaseModel):
    """Reproducible Python execution with workspace write guards."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = True
    outputs_dir: str = ".geoharness/runs"
    timeout_seconds: float = Field(default=120, gt=0, le=3600)
    max_output_chars: int = Field(default=12000, ge=100, le=1000000)


class PublicationConfig(BaseModel):
    """Bounded recovery for transient Windows directory publication failures."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    max_attempts: int = Field(default=4, ge=1, le=10)
    retry_delay_seconds: float = Field(default=0.1, ge=0, le=1)


class QgisConfig(BaseModel):
    """An independently installed QGIS runtime; never import it into the agent process."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = True
    install_dir: str = ""
    python_executable: str = ""
    prefix_path: str = ""
    outputs_dir: str = ".geoharness/qgis"
    plans_dir: str = ".geoharness/qgis-plans"
    inspection_max_chars: int = Field(default=2400, ge=1200, le=12000)
    timeout_seconds: float = Field(default=180, gt=0, le=3600)
    max_output_chars: int = Field(default=12000, ge=100, le=1000000)
    max_search_results: int = Field(default=20, ge=1, le=100)


class AgentConfig(BaseModel):
    """Completion budget for reasoning and complete tool arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    max_tokens: int = Field(default=16384, ge=1024, le=65536)
    task_budget_seconds: float | None = Field(default=None, gt=0, le=86400)
    delivery_reserve_seconds: float = Field(default=180, ge=0, le=3600)
    request_metrics_limit: int = Field(default=2048, ge=64, le=10000)
    context_keep_recent_results: int = Field(default=6, ge=1, le=100)
    context_result_max_chars: int = Field(default=4000, ge=1000, le=1000000)
    context_archive_dir: str = ".geoharness/runs/context"
    auto_continue: bool = Field(default=True, description=(
        "Continue pending tool results after a turn batch when new successful progress exists."
    ))
    internal_todo_path: str = ".geoharness/tasks/TODO.md"
    include_stream_usage: bool = True
    request_extra_body: dict[str, Any] = Field(default_factory=dict)
    reasoning_effort: Literal["low", "medium", "high", "max"] | None = Field(
        default=None, description="Explicit thinking policy; recovery keeps the main model/mode.")
    empty_response_retries: int = Field(default=1, ge=0, le=1)
    empty_response_fallback_model: str = ""
    empty_response_fallback_extra_body: dict[str, Any] = Field(default_factory=dict)
    empty_response_max_tokens: int | None = Field(default=None, ge=256, le=65536)
    tool_argument_retries: int = Field(default=1, ge=0, le=1, description=(
        "One targeted recovery for truncated invalid JSON calls; complete sibling calls "
        "and prior tool results are preserved. Uses the configured empty-response fallback."
    ))

    @field_validator("request_extra_body", "empty_response_fallback_extra_body")
    @classmethod
    def validate_provider_extensions(cls, value: dict[str, Any]) -> dict[str, Any]:
        """Extensions must not silently replace the configured model or tool context."""
        reserved = {
            "model", "messages", "tools", "stream", "stream_options",
            "max_tokens", "max_completion_tokens",
        }
        if reserved.intersection(value):
            raise ValueError("Use explicit agent settings for model and token limits")
        return value


class DeliveryConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    outputs_dir: str = "exports"


class MapLayout(BaseModel):
    """Presentation parameters that a map reviewer may safely adjust."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    width: float = Field(default=10, ge=4, le=24)
    height: float = Field(default=8, ge=4, le=24)
    dpi: int = Field(default=150, ge=72, le=300)
    legend_outside: bool = False
    title_font_size: float = Field(default=14, ge=8, le=32)
    legend_font_size: float = Field(default=9, ge=6, le=20)
    point_size: float = Field(default=18, ge=2, le=150)
    line_width: float = Field(default=1.8, ge=0.2, le=8)
    polygon_alpha: float = Field(default=0.75, ge=0.1, le=1)


class MapReviewConfig(BaseModel):
    """Image-based review. Empty model/provider fields reuse the main model."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    enabled: bool = True
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    request_extra_body: dict[str, Any] = Field(
        default_factory=dict,
        description="Optional provider request extensions; empty inherits provider defaults.",
    )
    reasoning_effort: Literal["low", "medium", "high", "max"] | None = None
    response_format: Literal["tool", "json"] = Field(
        default="tool",
        description=("Review response contract: function calling or JSON text; "
                     "no automatic fallback."),
    )
    # Accepted for existing configuration files; generation has no count or
    # cumulative review-time limit. timeout_seconds still bounds each request.
    max_repairs: int = Field(default=4, ge=0, deprecated=True,
                            description="Legacy setting; ignored for map generation.")
    max_reviews_per_request: int = Field(default=5, ge=0, deprecated=True,
                                        description="Legacy setting; review count is unlimited.")
    total_timeout_seconds: float = Field(default=120, gt=0, deprecated=True,
                                        description="Legacy setting; cumulative time is unlimited.")
    timeout_seconds: float = Field(default=180, gt=0, le=300)
    read_timeout_seconds: float = Field(default=90, gt=0, le=300,
        description="Network idle timeout for streamed pixel review; total deadline is separate.")
    service_failure_ttl_seconds: float = Field(
        default=60, gt=0, le=86400, allow_inf_nan=False,
        description="Transient review outage cooldown; server Retry-After may extend it.",
    )
    max_tokens: int = Field(default=8192, ge=256, le=131072)
    max_image_side: int = Field(default=1800, ge=256, le=4096)
    auto_repair: bool = Field(default=False, deprecated=True,
        description="Legacy setting; geo_map_review is always read-only and ignores it.")
    max_rois: int = Field(default=5, ge=0, le=8)
    roi_max_side: int = Field(default=2048, ge=256, le=4096)
    max_border_fraction: float = Field(default=0.08, gt=0, le=0.25)
    diagnostics_max_chars: int = Field(default=2048, ge=128, le=8192)
    max_validation_errors: int = Field(default=12, ge=1, le=50)
    layout: MapLayout = Field(default_factory=MapLayout)

    @field_validator("reasoning_effort", "response_format", mode="before")
    @classmethod
    def normalize_review_options(cls, value: Any) -> Any:
        """Hand-edited option spelling is case insensitive; model identifiers are not."""
        return value.strip().lower() if isinstance(value, str) else value

# Pattern for ${VAR} expansion in config values
_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


@dataclass(frozen=True)
class McpServerConfig:
    """Single MCP server configuration."""

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None


@dataclass(frozen=True)
class BridgeRule:
    """Analysis type → symbolization mapping."""

    render_method: str = "categorized"
    color_scheme: str = "RdBu"
    n_classes: int = 5


@dataclass(frozen=True)
class CartographyConfig:
    """Cartography configuration."""

    default_template: str = ""
    outputs_dir: str = "exports"
    static_renderer: str = "qgis"
    jobs_dir: str = ".geoharness/maps"
    minimum_span_geographic: float = 0.02
    minimum_span_projected: float = 1000
    extent_padding: float = 0.05
    bridge_rules: dict[str, BridgeRule] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.static_renderer not in {"qgis", "matplotlib"}:
            raise ValueError("cartography.static_renderer must be qgis or matplotlib")
        if any(isinstance(value, bool) or not math.isfinite(value) or value <= 0
               for value in (self.minimum_span_geographic, self.minimum_span_projected)):
            raise ValueError("cartography minimum extent spans must be positive")
        if isinstance(self.extent_padding, bool) or not math.isfinite(self.extent_padding) or not (
            0 <= self.extent_padding <= 1
        ):
            raise ValueError("cartography.extent_padding must be between 0 and 1")


@dataclass(frozen=True)
class CascadeConfig:
    """Cognitive cascade configuration."""

    l1_to_l2_tools: list[str] = field(default_factory=lambda: [
        "geo_spatial_relation",
        "geo_all_relations",
    ])
    l2_to_l3_tools: list[str] = field(default_factory=lambda: [
        "geo_moran_local",
        "geo_getis_ord",
    ])
    suggestion_prompt: str = (
        "分析结果已完成，建议加载更高级认知技能以深入理解空间模式。"
    )


@dataclass(frozen=True)
class CognitionConfig:
    """Cognition configuration."""

    cascade: CascadeConfig = field(default_factory=CascadeConfig)


@dataclass(frozen=True)
class DataConfig:
    """Data source configuration."""

    default_crs: str = "EPSG:4326"
    analysis_crs: str = "EPSG:3857"
    postgis_dsn: str = ""
    file_dir: str = ""
    admin_kg_api_url: str = "https://geo.datav.aliyun.com/areas_v3/bound"
    admin_kg_cache_dir: str = ""


@dataclass(frozen=True)
class PermissionsConfig:
    """Permission configuration."""

    allow_write_paths: list[str] = field(
        default_factory=lambda: ["~/.geoharness/workspaces/"]
    )
    deny_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class GeoConfig:
    """Top-level GeoHarness configuration."""

    model: str = "deepseek-chat"
    api_key: str = ""
    base_url: str = ""
    mcp_servers: dict[str, McpServerConfig] = field(default_factory=dict)
    cartography: CartographyConfig = field(default_factory=CartographyConfig)
    cognition: CognitionConfig = field(default_factory=CognitionConfig)
    data: DataConfig = field(default_factory=DataConfig)
    permissions: PermissionsConfig = field(default_factory=PermissionsConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    publication: PublicationConfig = field(default_factory=PublicationConfig)
    qgis: QgisConfig = field(default_factory=QgisConfig)
    map_review: MapReviewConfig = field(default_factory=MapReviewConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)


def _expand_vars(value: Any) -> Any:
    """Expand ${VAR} references in a value using environment variables."""
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            var_name = match.group(1)
            return os.environ.get(var_name, match.group(0))

        return _VAR_PATTERN.sub(_replace, value)
    elif isinstance(value, dict):
        return {k: _expand_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_vars(item) for item in value]
    else:
        return value


def _load_env(config_dir: Path) -> None:
    """Load .env file from config directory."""
    env_path = config_dir / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _parse_mcp_servers(
    raw: dict[str, Any] | None,
) -> dict[str, McpServerConfig]:
    """Parse MCP server configs from raw YAML dict."""
    if not raw:
        return {}
    result: dict[str, McpServerConfig] = {}
    for name, cfg in raw.items():
        cfg_expanded = _expand_vars(cfg)
        result[name] = McpServerConfig(
            command=cfg_expanded.get("command", "python"),
            args=cfg_expanded.get("args", []),
            env=cfg_expanded.get("env", {}),
            cwd=cfg_expanded.get("cwd"),
        )
    return result


def _parse_bridge_rules(
    raw: dict[str, Any] | None,
) -> dict[str, BridgeRule]:
    """Parse bridge rules from raw YAML dict."""
    if not raw:
        return {}
    result: dict[str, BridgeRule] = {}
    for analysis_type, cfg in raw.items():
        result[analysis_type] = BridgeRule(
            render_method=cfg.get("render_method", "categorized"),
            color_scheme=cfg.get("color_scheme", "RdBu"),
            n_classes=cfg.get("n_classes", 5),
        )
    return result


def _parse_cartography(raw: dict[str, Any] | None) -> CartographyConfig:
    """Parse cartography config from raw YAML dict."""
    if not raw:
        return CartographyConfig()
    template = _expand_vars(raw.get("default_template", ""))
    outputs_dir = _expand_vars(raw.get("outputs_dir") or CartographyConfig().outputs_dir)
    rules = _parse_bridge_rules(raw.get("bridge_rules"))
    return CartographyConfig(
        default_template=template,
        outputs_dir=outputs_dir,
        static_renderer=raw.get("static_renderer", "qgis"),
        jobs_dir=_expand_vars(raw.get("jobs_dir") or CartographyConfig().jobs_dir),
        minimum_span_geographic=raw.get("minimum_span_geographic", 0.02),
        minimum_span_projected=raw.get("minimum_span_projected", 1000),
        extent_padding=raw.get("extent_padding", 0.05),
        bridge_rules=rules,
    )


def _parse_cascade(raw: dict[str, Any] | None) -> CascadeConfig:
    """Parse cascade config from raw YAML dict."""
    if not raw:
        return CascadeConfig()
    return CascadeConfig(
        l1_to_l2_tools=raw.get("l1_to_l2_tools", CascadeConfig().l1_to_l2_tools),
        l2_to_l3_tools=raw.get("l2_to_l3_tools", CascadeConfig().l2_to_l3_tools),
        suggestion_prompt=raw.get(
            "suggestion_prompt", CascadeConfig().suggestion_prompt
        ),
    )


def _parse_cognition(raw: dict[str, Any] | None) -> CognitionConfig:
    """Parse cognition config from raw YAML dict."""
    if not raw:
        return CognitionConfig()
    return CognitionConfig(cascade=_parse_cascade(raw.get("cascade")))


def _parse_data(raw: dict[str, Any] | None) -> DataConfig:
    """Parse data config from raw YAML dict.

    Supports both flat keys (postgis_dsn) and nested format:
        postgis:
          dsn: ...
        admin_kg:
          api_url: ...
          cache_dir: ...
    """
    if not raw:
        return DataConfig()

    # Handle nested postgis config (only if "postgis" key exists)
    if "postgis" in raw and isinstance(raw["postgis"], dict):
        postgis_dsn = _expand_vars(raw["postgis"].get("dsn", ""))
    else:
        postgis_dsn = _expand_vars(raw.get("postgis_dsn", ""))

    # Handle nested admin_kg config (only if "admin_kg" key exists)
    if "admin_kg" in raw and isinstance(raw["admin_kg"], dict):
        admin_kg_raw = raw["admin_kg"]
        admin_kg_api_url = admin_kg_raw.get(
            "api_url", "https://geo.datav.aliyun.com/areas_v3/bound"
        )
        admin_kg_cache_dir = _expand_vars(admin_kg_raw.get("cache_dir", ""))
    else:
        admin_kg_api_url = raw.get(
            "admin_kg_api_url", "https://geo.datav.aliyun.com/areas_v3/bound"
        )
        admin_kg_cache_dir = _expand_vars(raw.get("admin_kg_cache_dir", ""))

    return DataConfig(
        default_crs=raw.get("default_crs", "EPSG:4326"),
        analysis_crs=raw.get("analysis_crs", "EPSG:3857"),
        postgis_dsn=postgis_dsn,
        file_dir=_expand_vars(raw.get("file_dir", "")),
        admin_kg_api_url=admin_kg_api_url,
        admin_kg_cache_dir=admin_kg_cache_dir,
    )


def _parse_permissions(raw: dict[str, Any] | None) -> PermissionsConfig:
    """Parse permissions config from raw YAML dict."""
    if not raw:
        return PermissionsConfig()
    return PermissionsConfig(
        allow_write_paths=raw.get(
            "allow_write_paths", ["~/.geoharness/workspaces/"]
        ),
        deny_paths=raw.get("deny_paths", []),
    )


def resolve_config_path(config_path: Path | None = None) -> Path:
    """Resolve the same explicit/environment/default location for CLI and runtime."""
    if config_path is None and os.environ.get("GEOH_CONFIG_PATH"):
        config_path = Path(os.environ["GEOH_CONFIG_PATH"])
    return (config_path or Path.home() / ".geoharness" / "config.yaml").expanduser()


def load_geo_config(config_path: Path | None = None) -> GeoConfig:
    """Load GeoHarness configuration from YAML file.

    Reads from ~/.geoharness/config.yaml by default.
    Expands ${VAR} references using environment variables.
    Loads .env file from config directory first.

    Args:
        config_path: Optional explicit path to config.yaml.

    Returns:
        GeoConfig dataclass with all configuration.
    """
    # Determine config directory and file
    config_path = resolve_config_path(config_path)
    config_dir = config_path.parent

    # Load .env file first (does not override existing env vars)
    _load_env(config_dir)

    # If config file doesn't exist, return defaults
    if not config_path.exists():
        return GeoConfig()

    # Read and parse YAML
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        return GeoConfig()

    # Expand env vars in top-level string fields
    model = _expand_vars(raw.get("model", "deepseek-chat"))
    api_key = _expand_vars(raw.get("api_key", ""))
    base_url = _expand_vars(raw.get("base_url", ""))

    # Parse nested sections
    mcp_servers = _parse_mcp_servers(raw.get("mcp"))
    cartography = _parse_cartography(raw.get("cartography"))
    cognition = _parse_cognition(raw.get("cognition"))
    data = _parse_data(raw.get("data"))
    permissions = _parse_permissions(raw.get("permissions"))

    return GeoConfig(
        model=model,
        api_key=api_key,
        base_url=base_url,
        mcp_servers=mcp_servers,
        cartography=cartography,
        cognition=cognition,
        data=data,
        permissions=permissions,
        analysis=AnalysisConfig.model_validate(_expand_vars(raw.get("analysis") or {})),
        execution=ExecutionConfig.model_validate(_expand_vars(raw.get("execution") or {})),
        publication=PublicationConfig.model_validate(_expand_vars(raw.get("publication") or {})),
        qgis=QgisConfig.model_validate(_expand_vars(raw.get("qgis") or {})),
        map_review=MapReviewConfig.model_validate(_expand_vars(raw.get("map_review") or {})),
        agent=AgentConfig.model_validate(_expand_vars(raw.get("agent") or {})),
        delivery=DeliveryConfig.model_validate(_expand_vars(raw.get("delivery") or {})),
    )


def get_config_dir() -> Path:
    """Return the GeoHarness config directory path."""
    return Path.home() / ".geoharness"
