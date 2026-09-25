"""MCP activation — bounded connect + tool registration.

`build_geo_runtime()` constructs the McpClientManager but never connects
(connecting spawns subprocesses, which must not happen during unit tests).
Interactive entry points (print mode, TUI backend, dry-run) call
`activate_mcp()` once, which:

1. Connects each configured server with a per-server timeout so one slow
   or misconfigured server cannot hang startup.
2. Verifies the manager's post-connect status (catches transports that
   fail silently).
3. Registers every connected MCP tool into the SAME ToolRegistry instance
   the QueryEngine already holds (registration mutates in place).

Set GEOH_DISABLE_MCP=1 to skip activation entirely (diagnostics, CI).
Override the per-server handshake budget with GEOH_MCP_TIMEOUT (seconds).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from openharness.mcp.types import McpHttpServerConfig, McpStdioServerConfig

logger = logging.getLogger(__name__)

DEFAULT_PER_SERVER_TIMEOUT = 20.0


async def connect_mcp_servers(
    manager: Any,
    per_server_timeout: float = DEFAULT_PER_SERVER_TIMEOUT,
) -> dict[str, str]:
    """Connect all configured servers with a per-server timeout.

    Returns:
        Mapping of server name -> outcome ("connected", "timeout after Ns",
        "failed: <reason>", "unsupported transport").
    """
    results: dict[str, str] = {}

    server_configs = getattr(manager, "_server_configs", None)
    if not isinstance(server_configs, dict) or not server_configs:
        return results

    for name, cfg in list(server_configs.items()):
        transport = getattr(cfg, "type", None)
        if isinstance(cfg, McpStdioServerConfig):
            connect = getattr(manager, "_connect_stdio", None)
        elif isinstance(cfg, McpHttpServerConfig) or transport == "http":
            connect = getattr(manager, "_connect_http", None)
        elif transport == "stdio":
            # Duck-typed stdio config (e.g. lightweight test doubles).
            connect = getattr(manager, "_connect_stdio", None)
        else:
            connect = None

        if connect is None:
            results[name] = "unsupported transport"
            continue

        try:
            await asyncio.wait_for(connect(name, cfg), timeout=per_server_timeout)
        except asyncio.TimeoutError:
            if _status_state(manager, name) == "connected":
                results[name] = "connected"
                logger.info("MCP server '%s' connected (late)", name)
                continue
            results[name] = f"timeout after {per_server_timeout:g}s"
            logger.warning(
                "MCP server '%s' timed out after %.0fs — continuing without it",
                name,
                per_server_timeout,
            )
            _safe_disconnect(manager, name)
            continue
        except Exception as e:  # noqa: BLE001 — report and continue
            results[name] = f"failed: {e}"
            logger.warning("MCP server '%s' failed to connect: %s", name, e)
            _safe_disconnect(manager, name)
            continue

        state = _status_state(manager, name)
        if state in ("failed", "pending", "disabled"):
            # The handshake returned but never reached "connected":
            # pending = registration incomplete, failed/disabled = error.
            # Do not advertise tools from a server in this state.
            results[name] = f"failed (post-connect state: {state})"
            logger.warning(
                "MCP server '%s' did not reach connected state: %s", name, state
            )
            _safe_disconnect(manager, name)
        else:
            # "connected", or unknown (manager exposes no status API).
            results[name] = "connected"
            logger.info("MCP server '%s' connected", name)

    return results


def _status_state(manager: Any, name: str) -> str | None:
    """Read a server's post-connect state from the manager, if exposed."""
    try:
        for status in manager.list_statuses():
            if getattr(status, "name", None) == name:
                state = str(getattr(status, "state", "") or "")
                return state or None
    except Exception:
        return None
    return None


def _safe_disconnect(manager: Any, name: str) -> None:
    """Best-effort cleanup of a half-connected server."""
    disconnect = getattr(manager, "disconnect", None)
    if not callable(disconnect):
        return
    try:
        disconnect(name)
    except Exception as e:  # noqa: BLE001
        logger.debug("Disconnect cleanup for %s failed: %s", name, e)


async def activate_mcp(
    bundle: Any,
    per_server_timeout: float | None = None,
) -> dict[str, str]:
    """Connect configured MCP servers and register their tools.

    Args:
        bundle: RuntimeBundle whose mcp_manager and tool_registry should
            be activated (same registry object shared with the engine).
        per_server_timeout: Seconds allowed for each server handshake;
            defaults to GEOH_MCP_TIMEOUT or 20s.

    Returns:
        Mapping of server name -> connection outcome ({} when disabled).
    """
    if os.environ.get("GEOH_DISABLE_MCP") == "1":
        logger.info("MCP activation skipped (GEOH_DISABLE_MCP=1)")
        return {}

    if per_server_timeout is None:
        try:
            per_server_timeout = float(
                os.environ.get("GEOH_MCP_TIMEOUT", DEFAULT_PER_SERVER_TIMEOUT)
            )
        except ValueError:
            per_server_timeout = DEFAULT_PER_SERVER_TIMEOUT

    manager = getattr(bundle, "mcp_manager", None)
    if manager is None:
        return {}

    states = await connect_mcp_servers(manager, per_server_timeout)

    registry = getattr(bundle, "tool_registry", None)
    if registry is not None:
        from geoharness.tools import register_mcp_tools

        count = register_mcp_tools(registry, manager)
        logger.info(
            "MCP activation complete: %d/%d servers up, %d tools registered",
            sum(1 for state in states.values() if state == "connected"),
            len(states),
            count,
        )

    return states
