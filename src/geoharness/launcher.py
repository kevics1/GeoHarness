"""GeoHarness launcher — starts the TUI with the GeoHarness runtime.

Provides launch_geo_tui() which:
1. Tries React TUI via OpenHarness's run_repl()
2. Falls back to print mode if React frontend unavailable
3. Falls back to dry-run if no API key configured
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from geoharness.config.settings import GeoConfig, load_geo_config
from geoharness.hooks.cascade import build_cascade_manager
from geoharness.prompts.system_prompt import build_geo_system_prompt

logger = logging.getLogger(__name__)


async def launch_geo_tui(
    cwd: Path,
    config_path: Path | None = None,
    prompt: str | None = None,
    print_mode: bool = False,
) -> int:
    """Launch the GeoHarness TUI.

    Tries React TUI first, falls back to print mode.

    Args:
        cwd: Working directory.
        config_path: Optional config.yaml path.
        prompt: Optional initial prompt.
        print_mode: If True, use print mode instead of TUI.

    Returns:
        Exit code (0 = success).
    """
    config = load_geo_config(config_path)

    if not config.api_key:
        logger.error("No API key configured. Run 'geoh init' first.")
        print("Error: No API key configured. Run 'geoh init' first.")
        return 1

    cascade_manager = build_cascade_manager(config)
    system_prompt = build_geo_system_prompt(cwd, config, cascade_manager)

    if print_mode:
        return await _run_print_mode(cwd, config, system_prompt, prompt)

    # Try React TUI
    try:
        return await _run_react_tui(cwd, config, system_prompt, prompt)
    except Exception as e:
        logger.warning("React TUI failed: %s, falling back to print mode", e)
        return await _run_print_mode(cwd, config, system_prompt, prompt)


async def _run_react_tui(
    cwd: Path,
    config: GeoConfig,
    system_prompt: str,
    prompt: str | None,
) -> int:
    """Run the React TUI via OpenHarness's run_repl."""
    from openharness.ui.app import run_repl

    await run_repl(
        prompt=prompt,
        cwd=str(cwd),
        model=config.model,
        base_url=config.base_url or None,
        system_prompt=system_prompt,
        api_key=config.api_key,
    )
    return 0


async def _run_print_mode(
    cwd: Path,
    config: GeoConfig,
    system_prompt: str,
    prompt: str | None,
) -> int:
    """Run in print mode (no TUI, just stdin/stdout)."""
    from openharness.ui.runtime import start_runtime

    from geoharness.runtime import build_geo_runtime

    bundle = await build_geo_runtime(
        cwd=cwd,
        config_path=None,
        system_prompt=system_prompt,
    )
    await start_runtime(bundle)

    print(f"GeoHarness v0.1.0 | model={config.model} | cwd={cwd}")
    print("Type 'exit' to quit.\n")

    if prompt:
        await _process_line(bundle, prompt)

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line or line.lower() in ("exit", "quit"):
                break
            await _process_line(bundle, line)
    except (KeyboardInterrupt, EOFError):
        pass

    from openharness.ui.runtime import close_runtime

    await close_runtime(bundle)
    return 0


async def _process_line(bundle: object, line: str) -> None:
    """Process a single line of input."""
    from openharness.ui.runtime import handle_line

    await handle_line(
        bundle,  # type: ignore[arg-type]
        line,
        print_system=lambda *a, **kw: None,
        render_event=lambda *a, **kw: None,
        clear_output=lambda *a, **kw: None,
    )
