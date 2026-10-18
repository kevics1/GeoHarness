"""GeoHarness tool registry — whitelist + native + MCP.

Builds a ToolRegistry with only domain-relevant tools:
- 10 native tools plus 2 optional QGIS tools: Processing and editable projects
- 5 whitelisted OpenHarness tools (ask_user_question, skill, todo_write,
  tool_search, brief) — user interaction helpers
- MCP tools (registered dynamically when MCP manager is connected)

17 tools are available with QGIS enabled; configured MCP tools register dynamically.
"""

from __future__ import annotations

import logging
from typing import Any

from openharness.tools.base import ToolRegistry

from geoharness.tools.geo_accessibility import GeoAccessibilityTool
from geoharness.tools.geo_analysis import GeoAnalysisTool
from geoharness.tools.geo_cartography import GeoCartographyTool
from geoharness.tools.geo_db_data import GeoDBDataTool
from geoharness.tools.geo_deliver import GeoDeliverTool
from geoharness.tools.geo_map_review import GeoMapReviewTool
from geoharness.tools.geo_python import GeoPythonTool
from geoharness.tools.geo_raster_data import GeoRasterDataTool
from geoharness.tools.geo_task import GeoTaskTool
from geoharness.tools.geo_vector_data import GeoVectorDataTool

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
        config: Optional GeoConfig controlling optional QGIS tool availability.

    Returns:
        ToolRegistry with domain-specific tools.
    """
    registry = ToolRegistry()

    # 1. Register native tools — one data loader per source type, so the
    #    model picks the data type up front instead of guessing a mixed source.
    registry.register(GeoDBDataTool())
    registry.register(GeoVectorDataTool())
    registry.register(GeoRasterDataTool())
    registry.register(GeoCartographyTool())
    registry.register(GeoAnalysisTool())
    registry.register(GeoAccessibilityTool())
    registry.register(GeoPythonTool(
        timeout_limit=getattr(getattr(config, "execution", None), "timeout_seconds", None)
    ))
    registry.register(GeoMapReviewTool())
    registry.register(GeoDeliverTool())
    registry.register(GeoTaskTool())
    logger.info("Registered 10 native tools")

    if getattr(getattr(config, "qgis", None), "enabled", True):
        from geoharness.tools.geo_qgis import GeoQgisTool
        from geoharness.tools.geo_qgis_project import GeoQgisProjectTool

        registry.register(GeoQgisTool())
        registry.register(GeoQgisProjectTool())

    # 2. Register whitelisted OpenHarness tools
    _register_whitelist_tools(registry)

    # 3. Register MCP tools if manager is provided
    if mcp_manager is not None:
        register_mcp_tools(registry, mcp_manager)

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
            if tool.name == "skill":
                from geoharness.tools.geo_skill import GeoSkillTool

                tool = GeoSkillTool()
            elif tool.name == "todo_write":
                from geoharness.tools.geo_todo import GeoTodoWriteTool

                tool = GeoTodoWriteTool()
            elif tool.name == "ask_user_question":
                from geoharness.tools.geo_question import GeoAskUserQuestionTool

                tool = GeoAskUserQuestionTool()
            registry.register(tool)
            registered += 1

    logger.info("Registered %d whitelisted OpenHarness tools", registered)


def register_mcp_tools(registry: ToolRegistry, mcp_manager: Any) -> int:
    """Register all connected MCP tools from a manager into a registry.

    Uses the upstream McpToolAdapter(manager, tool_info) contract. The
    manager exposes a flat list[McpToolInfo] via list_tools().

    Returns:
        Number of tools registered.
    """
    try:
        from openharness.tools.mcp_tool import McpToolAdapter
    except ImportError as e:
        logger.warning("McpToolAdapter not available: %s", e)
        return 0

    try:
        tools_info = list(mcp_manager.list_tools())
    except Exception as e:
        logger.warning("Failed to list MCP tools: %s", e)
        return 0

    registered = 0
    for tool_info in tools_info:
        try:
            registry.register(McpToolAdapter(mcp_manager, tool_info))
            registered += 1
        except Exception as e:
            logger.warning(
                "Failed to register MCP tool %s: %s",
                getattr(tool_info, "name", "?"),
                e,
            )

    logger.info("Registered %d MCP tools", registered)
    return registered
