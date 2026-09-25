"""GeoHarness launcher — starts the interactive session with the GeoHarness runtime.

Provides launch_geo_tui() which:
1. Defaults to React TUI — reuses OpenHarness's frontend with config isolation
2. Falls back to print mode if React frontend unavailable (--print to force)
3. Exits with error if no API key configured

Config isolation strategy for React TUI:
  The backend subprocess (spawned by launch_react_tui) inherits env vars from
  the parent process.  We set OPENHARNESS_CONFIG_DIR to ~/.geoharness/ and
  generate a clean settings.json there — a copy of ~/.openharness/settings.json
  with ALL credential_slot fields set to null and GeoHarness API settings
  (api_format, api_key, base_url, model) overridden.

  This is necessary because resolve_auth() checks profile.credential_slot
  BEFORE self.api_key (settings.py line 820-847).  When the upstream
  minimax-anthropic profile has credential_slot="minimax-anthropic",
  resolve_auth() finds the MiniMax key in credential storage and returns it,
  completely ignoring the --api-key CLI argument that build_runtime() passes
  via settings_overrides.  Setting OPENHARNESS_PROFILE=geoharness does NOT
  fix this because sync_active_profile_from_flat_fields() (called by
  merge_cli_overrides) trusts the profile's stale api_format when
  OPENHARNESS_PROFILE is set, reverting "openai" back to "anthropic" and
  causing AnthropicApiClient to be used instead of OpenAICompatibleClient.

  Using OPENHARNESS_CONFIG_DIR sidesteps both issues: load_settings() reads
  our clean settings.json (no credential_slot anywhere), so resolve_auth()
  falls through to self.api_key (set by --api-key CLI arg), and
  sync_active_profile_from_flat_fields() uses flat field values (correct)
  because OPENHARNESS_PROFILE is not set.
"""

from __future__ import annotations

import logging
import os
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
    """Launch the GeoHarness interactive session.

    Defaults to React TUI (reusing OpenHarness's frontend), with env-var
    config isolation to prevent ~/.openharness/settings.json from overriding
    GeoHarness settings in the backend subprocess.

    Args:
        cwd: Working directory.
        config_path: Optional config.yaml path.
        prompt: Optional initial prompt.
        print_mode: If True, use print mode instead of React TUI.

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

    # Generate a clean isolated settings.json and point OPENHARNESS_CONFIG_DIR
    # at it — inherited by the backend subprocess spawned by launch_react_tui.
    _setup_config_isolation(config)

    # Try React TUI (default)
    try:
        return await _run_react_tui(cwd, config, system_prompt, prompt)
    except Exception as e:
        logger.warning("React TUI failed: %s, falling back to print mode", e)
        print(f"React TUI 不可用: {e}。回退到打印模式。")
        return await _run_print_mode(cwd, config, system_prompt, prompt)


def _setup_config_isolation(config: GeoConfig) -> None:
    """Generate a clean settings.json in ~/.geoharness/ and point OPENHARNESS_CONFIG_DIR at it.

    This is the ROOT-CAUSE fix for the 401 "Invalid API key" error that recurred
    in React TUI mode while print mode worked fine.

    Why the 401 happened:
      ~/.openharness/settings.json has the "minimax-anthropic" profile with
      "credential_slot": "minimax-anthropic". In resolve_auth() (settings.py
      ~line 820), when credential_slot is set, the credential store is checked
      FIRST and self.api_key (set from the --api-key CLI arg by the React
      backend) is ignored via `explicit_key = "" if profile.credential_slot
      else self.api_key` (line 847). The stale MiniMax key then goes to an
      OpenAI-compatible endpoint -> 401.

    Why the previous OPENHARNESS_PROFILE=geoharness fix was insufficient:
      Setting OPENHARNESS_PROFILE triggers sync_active_profile_from_flat_fields()
      (settings.py ~line 673). With profile_from_env=True it sets
      flat_profile_fields_match_profile=True, which trusts the Profile's STALE
      api_format ("anthropic", captured before env overrides) over the current
      flat field value ("openai"). An AnthropicApiClient gets built against an
      OpenAI endpoint -> failure.

    This fix sidesteps both bugs at the source:
      1. Copy ~/.openharness/settings.json -> ~/.geoharness/settings.json
      2. Set ALL profiles' credential_slot to null (kills the priority chain)
      3. Override flat API fields (api_format=openai, api_key, base_url, model)
      4. Add a clean "geoharness" profile (no credential_slot) and make it active
      5. Set OPENHARNESS_CONFIG_DIR=~/.geoharness/ so load_settings() reads OUR file
      6. Do NOT set OPENHARNESS_PROFILE (avoids the stale-value bug entirely)

    The backend subprocess (spawned by launch_react_tui via useBackendSession.ts)
    inherits env vars from the parent, so OPENHARNESS_CONFIG_DIR reaches it.
    """
    import json

    home = Path.home()
    source_path = home / ".openharness" / "settings.json"
    geo_config_dir = home / ".geoharness"
    geo_config_dir.mkdir(parents=True, exist_ok=True)
    target_path = geo_config_dir / "settings.json"

    # Start from a copy of the original OpenHarness settings so that permission
    # rules, mcp_servers, plugin config, theme, etc. are preserved.
    if source_path.exists():
        settings: dict = json.loads(source_path.read_text(encoding="utf-8"))
    else:
        settings = {}

    # Override flat API fields for GeoHarness (OpenAI-compatible format).
    settings["api_key"] = config.api_key
    settings["api_format"] = "openai"
    settings["provider"] = "openai"
    settings["base_url"] = config.base_url
    settings["model"] = config.model
    settings["active_profile"] = "geoharness"
    # Ensure our --system-prompt CLI arg wins; don't pin one in the file.
    settings["system_prompt"] = None
    # Drop inherited MCP servers — GeoHarness builds its own MCP set from
    # ~/.geoharness/config.yaml in build_geo_runtime(). A copied entry (e.g.
    # a stale upstream MCP server from ~/.openharness/settings.json) must not
    # leak into the TUI backend process.
    settings["mcp_servers"] = {}

    # Clear ALL credential_slot fields and add a clean "geoharness" profile.
    # Nulling credential_slot is the heart of the root-cause fix: it makes
    # resolve_auth() fall through to self.api_key (set from --api-key CLI arg).
    profiles = settings.get("profiles")
    if not isinstance(profiles, dict):
        profiles = {}
    for profile in profiles.values():
        if isinstance(profile, dict):
            profile["credential_slot"] = None
    profiles["geoharness"] = {
        "label": "GeoHarness",
        "provider": "openai",
        "api_format": "openai",
        "auth_source": "openai_api_key",
        "default_model": config.model,
        "base_url": config.base_url,
        "last_model": config.model,
        "credential_slot": None,
        "allowed_models": [],
        "context_window_tokens": None,
        "auto_compact_threshold_tokens": None,
    }
    settings["profiles"] = profiles

    # Write the clean settings.json to ~/.geoharness/
    target_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Point OpenHarness at our isolated config directory. get_config_dir() in
    # paths.py checks this env var FIRST, so all of config/data/logs/sessions
    # resolve under ~/.geoharness/.
    os.environ["OPENHARNESS_CONFIG_DIR"] = str(geo_config_dir)

    # Do NOT set OPENHARNESS_PROFILE — it triggers the stale-value bug in
    # sync_active_profile_from_flat_fields(). active_profile in the file is enough.
    os.environ.pop("OPENHARNESS_PROFILE", None)

    # Keep OPENAI_API_KEY as a backup auth source: resolve_auth_env_value() finds
    # it when auth_source="openai_api_key" (our geoharness profile).
    os.environ["OPENAI_API_KEY"] = config.api_key

    logger.debug(
        "GeoHarness config isolation: wrote %s, OPENHARNESS_CONFIG_DIR=%s",
        target_path,
        geo_config_dir,
    )


async def _run_react_tui(
    cwd: Path,
    config: GeoConfig,
    system_prompt: str,
    prompt: str | None,
) -> int:
    """Run the React TUI via OpenHarness's run_repl.

    Env vars for config isolation are set by launch_geo_tui() before calling
    this function. The backend subprocess (spawned by launch_react_tui)
    inherits them via os.environ.copy().
    """
    from openharness.ui.app import run_repl

    await run_repl(
        prompt=prompt,
        cwd=str(cwd),
        model=config.model,
        base_url=config.base_url or None,
        system_prompt=system_prompt,
        api_key=config.api_key,
        api_format="openai",
    )
    return 0


async def _run_print_mode(
    cwd: Path,
    config: GeoConfig,
    system_prompt: str,
    prompt: str | None,
) -> int:
    """Run in print mode (stdin/stdout interactive, fully isolated).

    Bypasses handle_line() to avoid current_settings() reading
    ~/.openharness/settings.json and overwriting the GeoHarness system prompt.
    Directly calls engine.submit_message() and renders events to stdout.
    """
    from openharness.ui.runtime import close_runtime, start_runtime

    from geoharness.runtime import build_geo_runtime

    bundle = await build_geo_runtime(
        cwd=cwd,
        config_path=None,
        system_prompt=system_prompt,
    )

    # Connect MCP servers (build_geo_runtime constructs but doesn't connect)
    logging.getLogger("openharness").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)
    logging.getLogger().setLevel(logging.WARNING)
    try:
        await bundle.mcp_manager.connect_all()
    except Exception as e:
        logger.warning("MCP connection failed: %s", e)

    await start_runtime(bundle)

    print(f"GeoHarness v0.1.0 | model={config.model} | cwd={cwd}")
    print("Type 'exit' to quit.")
    sys.stdout.flush()

    if prompt:
        await _process_line(bundle, prompt)

    while True:
        try:
            line = input("\n> ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        line = line.strip()
        if not line or line.lower() in ("exit", "quit"):
            break
        await _process_line(bundle, line)
        sys.stdout.flush()

    await close_runtime(bundle)
    return 0


async def _process_line(bundle: object, line: str) -> None:
    """Process a single line of input — bypasses handle_line.

    Directly calls engine.submit_message() to avoid handle_line() calling
    bundle.current_settings() which reads ~/.openharness/settings.json and
    rebuilds the system prompt, overwriting the GeoHarness identity.
    """
    from openharness.engine.query import MaxTurnsExceeded

    engine = bundle.engine  # type: ignore[attr-defined]

    try:
        async for event in engine.submit_message(line):
            await _render_event(event)
    except MaxTurnsExceeded as exc:
        print(f"\n[已达到最大轮次限制 ({exc.max_turns})]")
    except Exception as e:
        print(f"\n[错误] {e}")
    print()  # newline after response


async def _render_event(event: object) -> None:
    """Render a StreamEvent to stdout."""
    from openharness.engine.stream_events import (
        AssistantTextDelta,
        AssistantTurnComplete,
        CompactProgressEvent,
        ErrorEvent,
        StatusEvent,
        ToolExecutionCompleted,
        ToolExecutionStarted,
    )

    if isinstance(event, AssistantTextDelta):
        print(event.text, end="", flush=True)
    elif isinstance(event, AssistantTurnComplete):
        pass  # text already printed via deltas
    elif isinstance(event, ToolExecutionStarted):
        print(f"\n[工具调用: {event.tool_name}]")
        if event.tool_input:
            import json

            preview = json.dumps(event.tool_input, ensure_ascii=False)
            if len(preview) > 200:
                preview = preview[:200] + "..."
            print(f"  参数: {preview}")
    elif isinstance(event, ToolExecutionCompleted):
        if event.is_error:
            print(f"  [错误] {event.output[:500]}")
        else:
            output = event.output
            if len(output) > 500:
                output = output[:500] + "..."
            print(f"  [结果] {output}")
    elif isinstance(event, ErrorEvent):
        print(f"\n[错误] {event.message}")
    elif isinstance(event, StatusEvent):
        print(f"\n[状态] {event.message}")
    elif isinstance(event, CompactProgressEvent):
        if event.message:
            print(f"\n[压缩进度] {event.message}")
