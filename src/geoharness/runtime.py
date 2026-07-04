"""GeoHarness runtime — directly constructs RuntimeBundle, bypassing build_runtime().

This is the core isolation mechanism: by NOT calling build_runtime(), we avoid
load_settings() which would read ~/.openharness/settings.json and leak upstream
MCP servers, model settings, and permissions into GeoHarness.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from openharness.api.openai_client import OpenAICompatibleClient
from openharness.commands.registry import create_default_command_registry
from openharness.config.settings import PermissionSettings
from openharness.engine.query_engine import QueryEngine
from openharness.hooks.executor import HookExecutionContext, HookExecutor
from openharness.hooks.loader import HookRegistry
from openharness.mcp.client import McpClientManager
from openharness.permissions.checker import PermissionChecker
from openharness.state.app_state import AppState
from openharness.state.store import AppStateStore
from openharness.tools.base import ToolRegistry
from openharness.ui.runtime import RuntimeBundle

from geoharness.config.settings import GeoConfig, load_geo_config
from geoharness.hooks.cascade import (
    CascadeManager,
    build_cascade_manager,
    register_cascade_hooks,
)
from geoharness.mcp.config import build_mcp_configs, verify_mcp_isolation

logger = logging.getLogger(__name__)


async def build_geo_runtime(
    cwd: Path,
    config_path: Path | None = None,
    system_prompt: str | None = None,
    extra_skill_dirs: tuple[str, ...] = (),
) -> RuntimeBundle:
    """Build GeoHarness runtime by directly constructing RuntimeBundle.

    This function does NOT call openharness.ui.runtime.build_runtime().
    Instead, it constructs each component independently and assembles
    the RuntimeBundle dataclass directly, ensuring complete isolation
    from upstream OpenHarness configuration.

    Args:
        cwd: Working directory for the agent.
        config_path: Optional explicit path to config.yaml.
        system_prompt: Optional pre-built system prompt.
        extra_skill_dirs: Additional skill directories to search.

    Returns:
        Fully assembled RuntimeBundle with domain-specific components.
    """
    # 1. Load GeoHarness config (from ~/.geoharness/config.yaml)
    config = load_geo_config(config_path)
    logger.info("GeoHarness config loaded: model=%s", config.model)

    # 2. Build API client directly (bypasses resolve_auth profile override)
    api_client = _build_api_client(config)
    logger.info("API client: %s", type(api_client).__name__)

    # 3. Build MCP manager with domain servers only
    mcp_manager = _build_mcp_manager(config)
    logger.info("MCP servers: %s", list(config.mcp_servers.keys()))

    # 4. Build tool registry with whitelist
    tool_registry = _build_tool_registry()
    logger.info("Tools registered: %d", len(tool_registry.list_tools()))

    # 5. Build permission checker
    permission_checker = _build_permission_checker(config)

    # 6. Build hook executor (registers cascade hooks)
    hook_executor = _build_hook_executor(config, api_client)

    # 7. Build DataCatalog and CascadeManager (needed for system prompt)
    from geoharness.data.catalog import build_data_catalog

    data_catalog = build_data_catalog(config)
    cascade_manager = build_cascade_manager(config)

    # 8. Build system prompt (uses cascade_manager for cascade state section)
    if system_prompt is None:
        system_prompt = _build_system_prompt(config, cwd, cascade_manager)

    engine = QueryEngine(
        api_client=api_client,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        cwd=str(cwd),
        model=config.model,
        system_prompt=system_prompt,
        max_tokens=4096,
        hook_executor=hook_executor,
        tool_metadata={
            "geoharness_config": config,
            "data_catalog": data_catalog,
            "cascade_manager": cascade_manager,
        },
    )

    # 9. Build AppState
    app_state = AppState(
        model=config.model,
        permission_mode="default",
        theme="dark",
        cwd=str(cwd),
        base_url=config.base_url,
    )
    app_state_store = AppStateStore(initial_state=app_state)

    # 10. Build commands registry
    commands = _build_commands()

    # 11. Construct RuntimeBundle directly
    # Auto-include project skills directory (skills/ at project root)
    project_skills_dir = str(Path(__file__).parent.parent.parent / "skills")
    all_skill_dirs = (project_skills_dir, *extra_skill_dirs)

    bundle = RuntimeBundle(
        api_client=api_client,
        cwd=str(cwd),
        mcp_manager=mcp_manager,
        tool_registry=tool_registry,
        app_state=app_state_store,
        hook_executor=hook_executor,
        engine=engine,
        commands=commands,
        external_api_client=True,
        enforce_max_turns=True,
        extra_skill_dirs=all_skill_dirs,
        extra_plugin_roots=(),
    )

    # 12. Verify MCP isolation
    _verify_mcp_isolation(bundle, config)

    logger.info("GeoHarness runtime assembled successfully")
    return bundle


def _build_api_client(config: GeoConfig) -> OpenAICompatibleClient:
    """Build OpenAI-compatible API client directly.

    This bypasses resolve_auth() which would override api_key with
    the active profile from ~/.openharness/credentials.json.
    """
    if not config.api_key:
        logger.warning("No API key configured — agent will fail on LLM calls")

    return OpenAICompatibleClient(
        api_key=config.api_key,
        base_url=config.base_url if config.base_url else None,
    )


def _build_mcp_manager(config: GeoConfig) -> McpClientManager:
    """Build MCP client manager with domain servers only.

    Delegates to geoharness.mcp.config.build_mcp_configs() which converts
    GeoConfig.mcp_servers to McpStdioServerConfig objects.
    """
    server_configs = build_mcp_configs(config)
    return McpClientManager(server_configs=server_configs)


def _build_tool_registry() -> ToolRegistry:
    """Build tool registry with GeoHarness whitelist.

    Phase 1: Empty registry — tools will be added in Phase 3.
    """
    return ToolRegistry()


def _build_permission_checker(config: GeoConfig) -> PermissionChecker:
    """Build permission checker with domain rules."""
    perm_settings = PermissionSettings()
    return PermissionChecker(settings=perm_settings)


def _build_hook_executor(
    config: GeoConfig, api_client: OpenAICompatibleClient
) -> HookExecutor:
    """Build hook executor with cascade hooks registered.

    Phase 6: Registers PromptHookDefinition cascade hooks for
    USER_PROMPT_SUBMIT event via register_cascade_hooks().
    """
    registry = HookRegistry()
    register_cascade_hooks(registry, config)

    context = HookExecutionContext(
        cwd=Path.cwd(),
        api_client=api_client,
        default_model=config.model,
    )
    return HookExecutor(registry=registry, context=context)


def _build_system_prompt(
    config: GeoConfig,
    cwd: Path,
    cascade_manager: CascadeManager | None = None,
) -> str:
    """Build GeoHarness system prompt.

    Phase 7: Full system prompt with 6 geography principles, skills catalog,
    tool catalog, cascade state, and bridge rules.
    """
    from geoharness.prompts.system_prompt import build_geo_system_prompt

    return build_geo_system_prompt(cwd, config, cascade_manager)


def _build_commands() -> Any:
    """Build command registry using OpenHarness default command registry."""
    return create_default_command_registry()


def _verify_mcp_isolation(bundle: RuntimeBundle, config: GeoConfig) -> None:
    """Verify no upstream MCP servers leaked into the bundle.

    Delegates to geoharness.mcp.config.verify_mcp_isolation() which checks
    that only domain-configured MCP servers are present.
    """
    expected = set(config.mcp_servers.keys())
    is_isolated, leaked = verify_mcp_isolation(bundle.mcp_manager, expected)
    if not is_isolated:
        logger.warning(
            "MCP isolation check failed: %d leaked servers: %s",
            len(leaked),
            leaked,
        )
