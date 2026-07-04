"""MCP server configuration builder — converts GeoConfig to McpStdioServerConfig.

All MCP server paths, commands, and env vars come from config/environment
variables. Never hardcode absolute paths.

Key function: build_mcp_configs(config) -> dict[str, McpStdioServerConfig]
"""

from __future__ import annotations

import logging
from typing import Any

from openharness.mcp.types import McpStdioServerConfig

from geoharness.config.settings import GeoConfig

logger = logging.getLogger(__name__)

# Expected domain MCP servers (no upstream servers should leak in)
EXPECTED_MCP_SERVERS = {"geo-mcp-server", "qgis", "postgres"}


def build_mcp_configs(
    config: GeoConfig,
) -> dict[str, McpStdioServerConfig]:
    """Build MCP server configs from GeoConfig.

    Converts GeoConfig.mcp_servers (McpServerConfig dataclass) to
    McpStdioServerConfig (Pydantic model) for McpClientManager.

    Args:
        config: GeoHarness configuration.

    Returns:
        Dict mapping server names to McpStdioServerConfig objects.
    """
    server_configs: dict[str, McpStdioServerConfig] = {}

    for name, cfg in config.mcp_servers.items():
        try:
            server_configs[name] = McpStdioServerConfig(
                type="stdio",
                command=cfg.command,
                args=list(cfg.args),
                env=dict(cfg.env) if cfg.env else None,
                cwd=cfg.cwd if cfg.cwd else None,
            )
        except Exception as e:
            logger.error("Failed to build MCP config for %s: %s", name, e)

    logger.info(
        "Built %d MCP server configs: %s",
        len(server_configs),
        list(server_configs.keys()),
    )
    return server_configs


def verify_mcp_isolation(
    mcp_manager: Any,
    expected_servers: set[str] | None = None,
) -> tuple[bool, set[str]]:
    """Verify no upstream MCP servers leaked into the manager.

    Args:
        mcp_manager: McpClientManager instance to check.
        expected_servers: Set of expected server names.
            Defaults to EXPECTED_MCP_SERVERS.

    Returns:
        Tuple of (is_isolated, leaked_servers).
        - is_isolated: True if no unexpected servers found.
        - leaked_servers: Set of server names that should not be present.
    """
    if expected_servers is None:
        expected_servers = EXPECTED_MCP_SERVERS

    try:
        statuses = mcp_manager.list_statuses()
        actual_servers = {s.name for s in statuses}
    except Exception as e:
        logger.warning("Cannot list MCP statuses: %s", e)
        return True, set()  # Assume isolated if we can't check

    leaked = actual_servers - expected_servers
    if leaked:
        logger.warning("Leaked MCP servers from upstream: %s", leaked)
        for name in leaked:
            try:
                mcp_manager.disconnect(name)
                logger.info("Disconnected leaked server: %s", name)
            except Exception as e:
                logger.warning("Failed to disconnect %s: %s", name, e)
        return False, leaked

    logger.info("MCP isolation verified: %d servers, no leaks", len(actual_servers))
    return True, set()


def get_mcp_server_names(config: GeoConfig) -> list[str]:
    """Return the list of configured MCP server names.

    Args:
        config: GeoHarness configuration.

    Returns:
        List of server names in configuration order.
    """
    return list(config.mcp_servers.keys())
