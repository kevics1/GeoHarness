"""GeoHarness TUI backend host — the React frontend's backend process.

The React terminal frontend spawns this module
(``python -m geoharness.tui_backend``) instead of upstream
``python -m openharness --backend-only``. Upstream's ``ReactBackendHost``
loop is reused, but its runtime factory
(``openharness.ui.backend_host.build_runtime``) is swapped for the
GeoHarness runtime so the TUI gets the Geo system prompt, native tools
and domain MCP servers — instead of upstream ``build_runtime()``.

The backend also suppresses the duplicate system transcript emitted by
upstream immediately after an error event.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _geo_backend_host_class():
    from openharness.ui.backend_host import ReactBackendHost

    class GeoReactBackendHost(ReactBackendHost):
        """Emit each error once while preserving unrelated status messages."""

        _last_error_message: str | None = None

        async def _emit(self, event):
            previous = self._last_error_message
            self._last_error_message = None
            if (
                previous is not None and event.type == "transcript_item"
                and event.item is not None and event.item.role == "system"
                and event.item.text == previous
            ):
                return
            if event.type == "error":
                self._last_error_message = event.message
            await super()._emit(event)

    return GeoReactBackendHost


async def geo_backend_runtime_factory(**kwargs: Any) -> Any:
    """Signature-compatible replacement for upstream ``build_runtime``.

    Receives the kwargs ``ReactBackendHost.run()`` passes (model, api_key,
    permission_prompt, ask_user_prompt, ...). Credentials/model are
    intentionally ignored: the GeoHarness runtime is config-driven from
    ``~/.geoharness/`` so the TUI backend can never be hijacked by
    inherited upstream auth.
    """
    from geoharness.mcp.connect import activate_mcp
    from geoharness.runtime import build_geo_runtime

    cwd = Path(kwargs.get("cwd") or Path.cwd())
    system_prompt = kwargs.get("system_prompt")
    extra_skill_dirs = tuple(kwargs.get("extra_skill_dirs") or ())

    bundle = await build_geo_runtime(
        cwd=cwd,
        system_prompt=system_prompt,
        extra_skill_dirs=extra_skill_dirs,
        permission_prompt=kwargs.get("permission_prompt"),
        ask_user_prompt=kwargs.get("ask_user_prompt"),
    )

    restored_messages = kwargs.get("restore_messages")
    if restored_messages:
        from openharness.engine.messages import (
            ConversationMessage,
            sanitize_conversation_messages,
        )

        bundle.engine.load_messages(sanitize_conversation_messages([
            ConversationMessage.model_validate(message) for message in restored_messages
        ]))
    restored_metadata = kwargs.get("restore_tool_metadata")
    if isinstance(restored_metadata, dict):
        # Restore conversation focus, never serialized config or live connectors.
        focus = restored_metadata.get("task_focus_state")
        if isinstance(focus, dict):
            bundle.engine.tool_metadata["task_focus_state"] = focus

    max_turns = kwargs.get("max_turns")
    if max_turns is not None:
        try:
            bundle.engine.set_max_turns(max_turns)
        except Exception as e:  # noqa: BLE001
            logger.debug("set_max_turns failed: %s", e)

    # MCP activation is bounded; failures degrade to the native toolset.
    try:
        await activate_mcp(bundle)
    except Exception as e:  # noqa: BLE001 — TUI must start even if MCP is down
        logger.warning("MCP activation failed: %s", e)

    return bundle


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="geoharness.tui_backend")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--system-prompt", dest="system_prompt", default=None)
    parser.add_argument("--permission-mode", dest="permission_mode", default=None)
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    import openharness.ui.backend_host as backend_host

    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    backend_host.build_runtime = geo_backend_runtime_factory
    backend_host.ReactBackendHost = _geo_backend_host_class()

    host_kwargs: dict[str, Any] = {}
    if args.cwd:
        host_kwargs["cwd"] = args.cwd
    if args.system_prompt:
        host_kwargs["system_prompt"] = args.system_prompt
    if args.permission_mode:
        host_kwargs["permission_mode"] = args.permission_mode

    return asyncio.run(backend_host.run_backend_host(**host_kwargs))


if __name__ == "__main__":
    raise SystemExit(main())
