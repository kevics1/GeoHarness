"""GeoHarness tool registry — whitelist + native + MCP.

Builds a ToolRegistry with only domain-relevant tools:
- 2 native tools (geo_data, geo_cartography) — orchestration
- 5 whitelisted OpenHarness tools (ask_user_question, skill, todo_write,
  tool_search, brief) — user interaction helpers
- MCP tools (registered dynamically when MCP manager is connected)

Total target: 2 + 5 + 66 MCP = 73 tools (when all MCP servers connected).
"""

from __future__ import annotations

import logging
from typing import Any

from openharness.tools.base import ToolRegistry

from geoharness.tools.geo_cartography import GeoCartographyTool
from geoharness.tools.geo_data import GeoDataTool

logger = logging.getLogger(__name__)

# OpenHarness tools to keep (whitelist)
# These are user-interaction and utility tools that don't conflict with
# domain-specific operations. All other 33 built-in tools are excluded.
_WHITELIST_TOOL_NAMES = {
    "ask_user_question",
    "skill",
    "todo_write",
    "tool_search",
    "brief",
}


def build_geo_tool_registry(
    mcp_manager: Any | None = None,
    config: Any | None = None,
) -> ToolRegistry:
    """Build GeoHarness tool registry with whitelist + native + MCP.

    Does NOT call create_default_tool_registry() — starts from empty
    registry to avoid leaking 33+ OpenHarness built-in tools.

    Args:
        mcp_manager: Optional McpClientManager with connected servers.
            If provided, all MCP tools are registered.
        config: Optional GeoConfig (currently unused but reserved for
            future config-driven tool filtering).

    Returns:
        ToolRegistry with domain-specific tools.
    """
    registry = ToolRegistry()

    # 1. Register native tools
    registry.register(GeoDataTool())
    registry.register(GeoCartographyTool())
    logger.info("Registered 2 native tools")

    # 2. Register whitelisted OpenHarness tools
    _register_whitelist_tools(registry)

    # 3. Register MCP tools if manager is provided
    if mcp_manager is not None:
        _register_mcp_tools(registry, mcp_manager)

    total = len(registry.list_tools())
    logger.info("Tool registry built: %d tools", total)
    return registry


def _register_whitelist_tools(registry: ToolRegistry) -> None:
    """Register whitelisted OpenHarness helper tools.

    Imports from create_default_tool_registry and picks only the tools
    in _WHITELIST_TOOL_NAMES.
    """
    try:
        from openharness.tools import create_default_tool_registry

        default_registry = create_default_tool_registry()
    except ImportError as e:
        logger.warning("Failed to import default tool registry: %s", e)
        return

    registered = 0
    for tool in default_registry.list_tools():
        if tool.name in _WHITELIST_TOOL_NAMES:
            registry.register(tool)
            registered += 1

    logger.info("Registered %d whitelisted OpenHarness tools", registered)


def _register_mcp_tools(
    registry: ToolRegistry, mcp_manager: Any
) -> None:
    """Register all MCP tools from connected servers.

    Uses McpToolAdapter to wrap each MCP tool as a BaseTool.
    """
    try:
        from openharness.mcp.adapter import McpToolAdapter
    except ImportError:
        try:
            from openharness.mcp.client import McpToolAdapter
        except ImportError as e:
            logger.warning("McpToolAdapter not available: %s", e)
            return

    registered = 0
    try:
        # Try different methods to get MCP tools
        if hasattr(mcp_manager, "list_tools"):
            tools_info = mcp_manager.list_tools()
            for server_name, tool_info in tools_info:
                tool = McpToolAdapter(
                    server_name=server_name,
                    tool_info=tool_info,
                    mcp_manager=mcp_manager,
                )
                registry.register(tool)
                registered += 1
        elif hasattr(mcp_manager, "get_all_tools"):
            tools = mcp_manager.get_all_tools()
            for tool in tools:
                registry.register(tool)
                registered += 1
    except Exception as e:
        logger.warning("Failed to register MCP tools: %s", e)

    logger.info("Registered %d MCP tools", registered)
