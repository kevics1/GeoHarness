"""GeoHarness configuration — YAML + .env driven, fully isolated from OpenHarness."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

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
    outputs_dir: str = ""
    bridge_rules: dict[str, BridgeRule] = field(default_factory=dict)


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
    outputs_dir = _expand_vars(raw.get("outputs_dir", ""))
    rules = _parse_bridge_rules(raw.get("bridge_rules"))
    return CartographyConfig(
        default_template=template,
        outputs_dir=outputs_dir,
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
    if config_path is None:
        config_dir = Path.home() / ".geoharness"
        config_path = config_dir / "config.yaml"
    else:
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
    )


def get_config_dir() -> Path:
    """Return the GeoHarness config directory path."""
    return Path.home() / ".geoharness"
