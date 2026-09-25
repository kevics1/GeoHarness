"""GeoHarness backend — bridges the runtime to the TUI frontend.

Provides functions to build the GeoHarness runtime and start a session.
The launcher uses these to connect the TUI to the domain-specific runtime.
"""

from __future__ import annotations

import logging
from pathlib import Path

from openharness.ui.runtime import RuntimeBundle, close_runtime, start_runtime

from geoharness.mcp.connect import activate_mcp
from geoharness.runtime import build_geo_runtime

logger = logging.getLogger(__name__)


async def start_geo_backend(
    cwd: Path,
    config_path: Path | None = None,
    extra_skill_dirs: tuple[str, ...] = (),
) -> RuntimeBundle:
    """Build and start the GeoHarness runtime.

    This is the main entry point for the TUI backend. It:
    1. Builds the GeoHarness runtime via build_geo_runtime()
    2. Starts the session (fires SESSION_START hooks)
    3. Returns the RuntimeBundle for the TUI to use

    Args:
        cwd: Working directory for the agent.
        config_path: Optional explicit path to config.yaml.
        extra_skill_dirs: Additional skill directories.

    Returns:
        Started RuntimeBundle ready for TUI interaction.
    """
    logger.info("Starting GeoHarness backend in %s", cwd)

    bundle = await build_geo_runtime(
        cwd=cwd,
        config_path=config_path,
        extra_skill_dirs=extra_skill_dirs,
    )

    # Activate MCP (bounded per-server timeouts; degrade gracefully when a
    # server is slow or misconfigured — the session must still start).
    try:
        await activate_mcp(bundle)
    except Exception as e:  # noqa: BLE001
        logger.warning("MCP activation failed: %s", e)

    await start_runtime(bundle)
    logger.info("GeoHarness backend started successfully")

    return bundle


async def stop_geo_backend(bundle: RuntimeBundle) -> None:
    """Stop the GeoHarness runtime and clean up resources.

    Args:
        bundle: RuntimeBundle to stop.
    """
    logger.info("Stopping GeoHarness backend")
    await close_runtime(bundle)
    logger.info("GeoHarness backend stopped")
