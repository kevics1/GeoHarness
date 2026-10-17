"""GeoHarness runtime — directly constructs RuntimeBundle, bypassing build_runtime().

This is the core isolation mechanism: by NOT calling build_runtime(), we avoid
load_settings() which would read ~/.openharness/settings.json and leak upstream
MCP servers, model settings, and permissions into GeoHarness.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from openharness.api.openai_client import OpenAICompatibleClient
from openharness.commands.registry import create_default_command_registry
from openharness.config.settings import PathRuleConfig, PermissionSettings
from openharness.hooks.executor import HookExecutionContext, HookExecutor
from openharness.hooks.loader import HookRegistry
from openharness.mcp.client import McpClientManager
from openharness.permissions.checker import PermissionChecker
from openharness.state.app_state import AppState
from openharness.state.store import AppStateStore
from openharness.tools.base import ToolRegistry
from openharness.ui.runtime import RuntimeBundle

from geoharness.api_client import GeoOpenAICompatibleClient
from geoharness.config.settings import GeoConfig, load_geo_config
from geoharness.engine import GeoQueryEngine
from geoharness.hooks.cascade import (
    CascadeManager,
    build_cascade_manager,
    register_cascade_hooks,
)
from geoharness.hooks.executor import GeoHookExecutor
from geoharness.mcp.config import build_mcp_configs, verify_mcp_isolation
from geoharness.session_state import GeoSessionBackend
from geoharness.workspace_isolation import install_workspace_isolation

logger = logging.getLogger(__name__)

# Agent turn limit. The upstream default (8) aborts multi-layer cartography
# ("Stopped after 8 turns") well before the workflow finishes. Forty bounds one
# batch; pending tool results can be resumed without starting a new map budget.
_DEFAULT_MAX_TURNS = 40


async def build_geo_runtime(
    cwd: Path,
    config_path: Path | None = None,
    system_prompt: str | None = None,
    extra_skill_dirs: tuple[str, ...] = (),
    permission_prompt: Any | None = None,
    ask_user_prompt: Any | None = None,
    config_override: GeoConfig | None = None,
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
    # Reused upstream prompt/plugin consumers also need project path isolation;
    # OPENHARNESS_CONFIG_DIR alone does not affect their cwd/.openharness paths.
    install_workspace_isolation()

    # 1. Load GeoHarness config (from ~/.geoharness/config.yaml)
    config = config_override or load_geo_config(config_path)
    logger.info("GeoHarness config loaded: model=%s", config.model)
    from geoharness.cartography.review_config import review_configuration

    reviewer = review_configuration(config)
    logger.info("Image reviewer: model=%s source=%s reasoning=%s format=%s",
                reviewer["model"], reviewer["model_source"], reviewer["reasoning_effort"],
                reviewer["response_format"])

    # 2. Build API client directly (bypasses resolve_auth profile override)
    api_client = _build_api_client(config)
    logger.info("API client: %s", type(api_client).__name__)

    # 3. Build MCP manager with domain servers only
    mcp_manager = _build_mcp_manager(config)
    logger.info("MCP servers: %s", list(config.mcp_servers.keys()))

    # 4. Build tool registry with whitelist
    tool_registry = _build_tool_registry(config)
    logger.info("Tools registered: %d", len(tool_registry.list_tools()))

    # 5. Build permission checker
    permission_checker = _build_permission_checker(config)

    # 6. Build hook executor (registers cascade hooks)
    hook_executor = _build_hook_executor(config, api_client, cwd)

    # 7. Build DataCatalog and CascadeManager (needed for system prompt)
    from geoharness.data.catalog import build_data_catalog

    data_catalog = build_data_catalog(config, workspace_dir=str(cwd))
    cascade_manager = build_cascade_manager(config)

    # 8. Build system prompt (uses cascade_manager for cascade state section)
    if system_prompt is None:
        system_prompt = _build_system_prompt(config, cwd, cascade_manager)

    # Skill directories: bundled skills/ plus any caller-provided dirs. Needed
    # both for the RuntimeBundle (below) and the engine's tool_metadata so the
    # `skill` tool can find bundled skills at runtime.
    all_skill_dirs = (str(_bundled_skills_dir()), *extra_skill_dirs)

    engine = GeoQueryEngine(
        api_client=api_client,
        tool_registry=tool_registry,
        permission_checker=permission_checker,
        cwd=str(cwd),
        model=config.model,
        system_prompt=system_prompt,
        max_tokens=config.agent.max_tokens,
        # Multi-layer cartography routinely needs 20+ tool calls (inspect every
        # layer, load, symbolize, export). The upstream default of 8 turns
        # aborts mid-workflow ("Stopped after 8 turns"); default to 40, still
        # overridable via GEOH_MAX_TURNS.
        max_turns=_resolve_max_turns(config),
        permission_prompt=permission_prompt,
        ask_user_prompt=ask_user_prompt,
        hook_executor=hook_executor,
        tool_metadata={
            "geoharness_config": config,
            "data_catalog": data_catalog,
            "cascade_manager": cascade_manager,
            "map_workflow_state": {},
            "map_job_state": {},
            # Upstream execution shallow-copies metadata for each tool. Share
            # this bucket before the first inspect so measured profiles survive.
            "geo_data_context": {},
            # The `skill` tool reads this key to find skill directories.
            # Without it, project skills (cartography, spatial-analysis, …)
            # are invisible at runtime even though they are bundled — which
            # made the model burn turns guessing skill names in the TUI.
            "extra_skill_dirs": all_skill_dirs,
            # Visual review always owns its transport, even when it inherits the
            # main provider/model: extra_body, forced review output and disabled
            # SDK retries must not be lost through the agent's streaming client.
            # Tests may still explicitly inject a review client at the tool seam.
        },
    )

    # Refresh between model turns, including continue_pending; do not duplicate
    # data profiles in the persisted conversation or expose configuration secrets.
    from geoharness.data.task_context import model_data_context
    from geoharness.task_budget import budget_context

    api_client.data_context_provider = lambda: budget_context(
        engine.tool_metadata, config) + model_data_context(
        engine.tool_metadata, cwd=cwd, config=config,
    )

    # 8b. Warm up in the background: geopandas/rasterio imports plus the first
    # directory scans take a second or two (longer on cold AV caches). Doing
    # it here — once — means the model's first batch of parallel inspects hits
    # warm imports/caches instead of stampeding cold ones.
    _warm_data_layer(data_catalog)

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
        enforce_max_turns=False,
        settings_overrides={
            "model": config.model,
            "api_key": config.api_key,
            "api_format": "openai",
            "base_url": config.base_url,
            "system_prompt": system_prompt,
        },
        extra_skill_dirs=all_skill_dirs,
        extra_plugin_roots=(),
        session_backend=GeoSessionBackend(),
    )

    # 12. Verify MCP isolation
    _verify_mcp_isolation(bundle, config)

    logger.info("GeoHarness runtime assembled successfully")
    return bundle


def _bundled_skills_dir() -> Path:
    """Find installed package resources, or source skills in an editable checkout."""
    package_dir = Path(__file__).resolve().parent
    installed_dir = package_dir / "skills"
    if installed_dir.is_dir():
        return installed_dir
    return package_dir.parent.parent / "skills"


def _warm_data_layer(data_catalog: Any) -> None:
    """Pre-import the geo stack and warm the scans in a daemon thread.

    Everything that the model's first tool batch would otherwise do
    concurrently (heavy C-extension imports, directory walks) runs once here,
    while the session is still starting up. Failures are logged and ignored —
    warm-up is best-effort; real tool calls still work without it.
    """

    def _warm() -> None:
        try:
            import geopandas  # noqa: F401
            import matplotlib  # noqa: F401
            import rasterio  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            logger.debug("Geo library warm-up failed: %s", exc)
        try:
            for name in ("file", "raster"):
                connector = data_catalog.get_connector(name)
                if connector is not None:
                    connector.list_sources()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Scan warm-up failed: %s", exc)

    threading.Thread(target=_warm, name="geoh-warmup", daemon=True).start()


def _resolve_max_turns(config: GeoConfig) -> int:
    """Resolve the agent turn limit: env override, else a generous default.

    The upstream ``QueryEngine`` default is 8, which aborts multi-layer
    cartography workflows long before the model can inspect, load, symbolize
    and export every layer. GeoHarness defaults to 40 per batch; set
    ``GEOH_MAX_TURNS`` to a positive integer to override. Pending tool results
    survive the batch limit and can be resumed.

    Returns:
        The turn limit to pass to ``QueryEngine``.
    """
    raw = os.environ.get("GEOH_MAX_TURNS", "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            logger.warning("GEOH_MAX_TURNS=%r is not an integer; ignoring", raw)
        else:
            if value >= 1:
                return value
            logger.warning("GEOH_MAX_TURNS=%d is < 1; ignoring", value)
    return _DEFAULT_MAX_TURNS


def _build_api_client(config: GeoConfig) -> OpenAICompatibleClient:
    """Build OpenAI-compatible API client directly.

    This bypasses resolve_auth() which would override api_key with
    the active profile from ~/.openharness/credentials.json.
    """
    if not config.api_key:
        logger.warning("No API key configured — agent will fail on LLM calls")

    return GeoOpenAICompatibleClient(
        api_key=config.api_key,
        base_url=config.base_url if config.base_url else None,
        settings=config.agent,
    )


def _build_mcp_manager(config: GeoConfig) -> McpClientManager:
    """Build MCP client manager with domain servers only.

    Delegates to geoharness.mcp.config.build_mcp_configs() which converts
    GeoConfig.mcp_servers to McpStdioServerConfig objects.
    """
    server_configs = build_mcp_configs(config)
    return McpClientManager(server_configs=server_configs)


def _build_tool_registry(config: GeoConfig | None = None) -> ToolRegistry:
    """Build the GeoHarness tool registry with configured QGIS tools.

    MCP tools are registered later by geoharness.mcp.connect.activate_mcp()
    once servers have connected — registration mutates this same registry
    instance, which the QueryEngine also holds.
    """
    from geoharness.tools import build_geo_tool_registry

    return build_geo_tool_registry(config=config)


def _build_permission_checker(config: GeoConfig) -> PermissionChecker:
    """Build permission checker with rules derived from GeoConfig.

    deny_paths -> deny rules; allow_write_paths -> allow rules (the
    checker enforces deny rules and the built-in sensitive-path
    protection; allow rules are recorded for policy completeness).
    """
    path_rules: list[PathRuleConfig] = []
    for pattern in config.permissions.deny_paths:
        for variant in _path_pattern_variants(pattern):
            path_rules.append(PathRuleConfig(pattern=variant, allow=False))
    for pattern in config.permissions.allow_write_paths:
        for variant in _path_pattern_variants(pattern):
            path_rules.append(PathRuleConfig(pattern=variant, allow=True))

    perm_settings = PermissionSettings(path_rules=path_rules)
    # Native tools enforce their artifact/workspace destinations. Sensitive-path
    # protection and explicit denied_tools still outrank this allow-list.
    perm_settings.allowed_tools.extend([
        "geo_cartography", "geo_analysis", "geo_map_review", "geo_deliver",
        "todo_write",
    ])
    if config.execution.enabled:
        perm_settings.allowed_tools.append("geo_python")
    if config.qgis.enabled:
        perm_settings.allowed_tools.extend(["geo_qgis", "geo_qgis_project"])
    return PermissionChecker(settings=perm_settings)


def _path_pattern_variants(pattern: str) -> tuple[str, ...]:
    """Expand ``~`` and produce separator variants for fnmatch matching.

    The checker matches fully-resolved absolute paths with fnmatch. On
    Windows both ``\\`` and ``/`` can appear in candidate paths depending
    on the tool, and directory patterns should also match their contents.
    """
    expanded = str(Path(pattern).expanduser())
    base = expanded.rstrip("/\\")
    slash = base.replace("\\", "/")
    variants = {
        base,
        slash,
        f"{base}/*",
        f"{base}\\*",
        f"{slash}/*",
    }
    return tuple(sorted(variants))


def _build_hook_executor(
    config: GeoConfig, api_client: OpenAICompatibleClient, cwd: Path
) -> HookExecutor:
    """Build hook executor with cascade hooks registered.

    Phase 6: Registers PromptHookDefinition cascade hooks for
    USER_PROMPT_SUBMIT event via register_cascade_hooks().
    """
    registry = HookRegistry()
    register_cascade_hooks(registry, config)

    context = HookExecutionContext(
        cwd=cwd,
        api_client=api_client,
        default_model=config.model,
    )
    return GeoHookExecutor(registry=registry, context=context)


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
