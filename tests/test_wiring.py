"""Tests for the runtime wiring — MCP activation + TUI backend host.

Covers the Commit-B contracts:
- register_mcp_tools() uses the upstream McpToolAdapter(manager, tool_info)
  API and survives misbehaving managers.
- connect_mcp_servers() is bounded per server: success/timeout/failure are
  recorded, never raised, so one bad server cannot hang startup.
- activate_mcp() honors GEOH_DISABLE_MCP and registers tools into the
  registry shared with the engine.
- The React TUI backend command spawns ``geoharness.tui_backend`` and never
  passes credentials on the command line.
- tui_backend.main() swaps the backend host's runtime factory.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from openharness.mcp.types import McpStdioServerConfig, McpToolInfo
from openharness.tools.base import ToolRegistry

from geoharness.mcp.connect import activate_mcp, connect_mcp_servers
from geoharness.runtime import _path_pattern_variants
from geoharness.tools import register_mcp_tools

# ── Fakes ─────────────────────────────────────────────────────────


class FakeStatus:
    def __init__(self, name: str, state: str = "pending") -> None:
        self.name = name
        self.state = state


class FakeMcpManager:
    """Duck-typed McpClientManager for activation tests."""

    def __init__(
        self,
        behaviors: dict[str, str] | None = None,
        tools: list[McpToolInfo] | None = None,
    ) -> None:
        behaviors = behaviors or {}
        self._behaviors = behaviors
        self._server_configs = {
            name: McpStdioServerConfig(
                type="stdio", command="python", args=["-m", name]
            )
            for name in behaviors
        }
        self._statuses = {name: FakeStatus(name) for name in behaviors}
        self._tools = list(tools or [])
        self.disconnected: list[str] = []

    async def _connect_stdio(self, name: str, config: object) -> None:
        behavior = self._behaviors[name]
        if behavior == "ok":
            self._statuses[name].state = "connected"
        elif behavior == "fail":
            raise RuntimeError("boom")
        elif behavior == "slow":
            await asyncio.sleep(60)  # only a timeout can stop this
        elif behavior == "silent":
            return  # returns but never reaches "connected"

    def list_statuses(self) -> list[FakeStatus]:
        return list(self._statuses.values())

    def list_tools(self) -> list[McpToolInfo]:
        return list(self._tools)

    def disconnect(self, name: str) -> None:
        self.disconnected.append(name)
        self._statuses[name].state = "failed"


def _sample_tool_info() -> McpToolInfo:
    return McpToolInfo(
        server_name="geo",
        name="geo_bbox",
        description="bbox helper",
        input_schema={
            "type": "object",
            "properties": {"wkt": {"type": "string"}},
            "required": ["wkt"],
        },
    )


# ── connect_mcp_servers ───────────────────────────────────────────


class TestConnectMcpServers:
    async def test_records_success_failure_and_timeout(self) -> None:
        manager = FakeMcpManager(
            behaviors={"geo": "ok", "db": "fail", "slow": "slow"}
        )
        results = await connect_mcp_servers(manager, per_server_timeout=0.05)

        assert results["geo"] == "connected"
        assert results["db"].startswith("failed")
        assert results["slow"].startswith("timeout")
        # Half-connected server is cleaned up.
        assert "slow" in manager.disconnected

    async def test_silent_return_without_connected_state(self) -> None:
        """A connect call that returns without reaching 'connected' is a failure."""
        manager = FakeMcpManager(behaviors={"geo": "silent"})
        results = await connect_mcp_servers(manager, per_server_timeout=0.5)
        assert results["geo"].startswith("failed")

    async def test_empty_manager_noop(self) -> None:
        manager = FakeMcpManager()
        assert await connect_mcp_servers(manager) == {}


# ── register_mcp_tools ────────────────────────────────────────────


class TestRegisterMcpTools:
    def test_registers_into_registry(self) -> None:
        manager = FakeMcpManager({"geo": "ok"}, tools=[_sample_tool_info()])
        registry = ToolRegistry()
        assert register_mcp_tools(registry, manager) == 1

        tool = registry.get("mcp__geo__geo_bbox")
        assert tool is not None
        assert tool.name == "mcp__geo__geo_bbox"

    def test_survives_broken_manager(self) -> None:
        class Bad:
            def list_tools(self):
                raise RuntimeError("nope")

        assert register_mcp_tools(ToolRegistry(), Bad()) == 0


# ── activate_mcp ──────────────────────────────────────────────────


class TestActivateMcp:
    async def test_disabled_via_env(self, monkeypatch) -> None:
        monkeypatch.setenv("GEOH_DISABLE_MCP", "1")
        bundle = SimpleNamespace(
            mcp_manager=FakeMcpManager({"geo": "ok"}),
            tool_registry=ToolRegistry(),
        )
        assert await activate_mcp(bundle) == {}

    async def test_connects_and_registers_tools(self) -> None:
        manager = FakeMcpManager({"geo": "ok"}, tools=[_sample_tool_info()])
        registry = ToolRegistry()
        bundle = SimpleNamespace(mcp_manager=manager, tool_registry=registry)

        states = await activate_mcp(bundle, per_server_timeout=0.5)

        assert states == {"geo": "connected"}
        assert registry.get("mcp__geo__geo_bbox") is not None

    async def test_no_manager_noop(self) -> None:
        bundle = SimpleNamespace(mcp_manager=None, tool_registry=ToolRegistry())
        assert await activate_mcp(bundle) == {}


# ── path rule variants ────────────────────────────────────────────


class TestPathPatternVariants:
    def test_includes_base_and_recursive_globs(self, tmp_path) -> None:
        target = str(tmp_path / "workspaces")
        variants = _path_pattern_variants(target)

        assert target in variants
        assert any(v.endswith("*") for v in variants)
        assert any("/" in v for v in variants)


# ── TUI backend ───────────────────────────────────────────────────


class TestTuiBackend:
    def test_backend_command_spawns_geoharness_host(self) -> None:
        from geoharness.launcher import _geo_backend_command

        cmd = _geo_backend_command(
            cwd="X:/work",
            model="test-model",
            api_key="SECRET-KEY",
            api_format="openai",
            system_prompt="SYS",
            max_turns=5,
        )
        assert cmd[1:3] == ["-m", "geoharness.tui_backend"]
        assert "--cwd" in cmd and "X:/work" in cmd
        # Credentials and model must never travel on the command line.
        assert all("SECRET" not in part for part in cmd)
        assert "--api-key" not in cmd
        assert "--model" not in cmd

    def test_main_swaps_runtime_factory(self, monkeypatch) -> None:
        import openharness.ui.backend_host as backend_host

        from geoharness import tui_backend

        original_factory = backend_host.build_runtime
        observed: dict = {}

        async def fake_run(**kwargs):
            observed["factory"] = backend_host.build_runtime
            observed["kwargs"] = kwargs
            return 0

        monkeypatch.setattr(backend_host, "run_backend_host", fake_run)
        try:
            rc = tui_backend.main(["--cwd", "X:/tmp"])
        finally:
            backend_host.build_runtime = original_factory

        assert rc == 0
        assert observed["factory"] is tui_backend.geo_backend_runtime_factory
        assert observed["kwargs"]["cwd"] == "X:/tmp"


# ── upstream contract guard ───────────────────────────────────────


class TestUpstreamContracts:
    """Pin the private upstream surface the wiring depends on.

    If a future openharness-ai upgrade drops one of these, these tests tell
    us before runtime breaks.
    """

    def test_manager_private_methods_exist(self) -> None:
        from openharness.mcp.client import McpClientManager

        assert hasattr(McpClientManager, "_connect_stdio")
        assert hasattr(McpClientManager, "_connect_http")
        assert hasattr(McpClientManager, "list_statuses")
        assert hasattr(McpClientManager, "list_tools")

    def test_mcp_adapter_importable(self) -> None:
        from openharness.tools.mcp_tool import McpToolAdapter

        assert callable(McpToolAdapter)

    def test_backend_host_run_exists(self) -> None:
        from openharness.ui.backend_host import ReactBackendHost

        assert hasattr(ReactBackendHost, "run")

    def test_react_launcher_uses_module_global(self) -> None:
        """react_launcher must call build_backend_command via module global."""
        import inspect

        from openharness.ui import react_launcher

        source = inspect.getsource(react_launcher.launch_react_tui)
        assert "build_backend_command" in source
        assert "backend_command" in source
