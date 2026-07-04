"""Tests for Phase 5: MCP integration.

Verifies that:
- build_mcp_configs() converts GeoConfig to McpStdioServerConfig correctly
- verify_mcp_isolation() detects leaked upstream servers
- EXPECTED_MCP_SERVERS contains exactly the 3 domain servers
- get_mcp_server_names() returns config server names in order
- build_mcp_configs() gracefully handles invalid server configs
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest
from openharness.mcp.types import McpStdioServerConfig

from geoharness.config.settings import GeoConfig, McpServerConfig
from geoharness.mcp.config import (
    EXPECTED_MCP_SERVERS,
    build_mcp_configs,
    get_mcp_server_names,
    verify_mcp_isolation,
)

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def three_server_config() -> GeoConfig:
    """Config with all 3 domain MCP servers."""
    return GeoConfig(
        model="test-model",
        api_key="test-key",
        base_url="https://test.example.com/v1",
        mcp_servers={
            "geo-mcp-server": McpServerConfig(
                command="python",
                args=["-m", "geo_mcp_server"],
                env={"PYTHONUTF8": "1", "AMAP_API_KEY": "test-key"},
            ),
            "qgis": McpServerConfig(
                command="python",
                args=["-m", "qgis_mcp_server"],
                env={"PROJ_LIB": "/path/to/proj"},
            ),
            "postgres": McpServerConfig(
                command="python",
                args=["-m", "postgres_mcp_server"],
                env={"DSN": "postgresql://localhost/testdb"},
            ),
        },
    )


@pytest.fixture
def single_server_config() -> GeoConfig:
    """Config with only one MCP server."""
    return GeoConfig(
        model="test-model",
        api_key="test-key",
        base_url="https://test.example.com/v1",
        mcp_servers={
            "geo-mcp-server": McpServerConfig(
                command="python",
                args=["-m", "geo_mcp_server"],
            ),
        },
    )


@dataclass
class MockServerStatus:
    """Mock of MCP server status for testing."""

    name: str
    connected: bool = True


class MockMcpManager:
    """Mock MCP manager for isolation testing."""

    def __init__(self, server_names: list[str]) -> None:
        self._statuses = [
            MockServerStatus(name=name, connected=True) for name in server_names
        ]
        self.disconnected: list[str] = []

    def list_statuses(self) -> list[MockServerStatus]:
        return list(self._statuses)

    def disconnect(self, name: str) -> None:
        self.disconnected.append(name)
        self._statuses = [s for s in self._statuses if s.name != name]


# ── EXPECTED_MCP_SERVERS ──────────────────────────────────────────


class TestExpectedServers:
    """Test EXPECTED_MCP_SERVERS constant."""

    def test_contains_three_domain_servers(self) -> None:
        """EXPECTED_MCP_SERVERS must contain exactly the 3 domain servers."""
        assert EXPECTED_MCP_SERVERS == {"geo-mcp-server", "qgis", "postgres"}

    def test_count_is_three(self) -> None:
        """Exactly 3 servers expected."""
        assert len(EXPECTED_MCP_SERVERS) == 3

    def test_is_a_set(self) -> None:
        """Must be a set for efficient membership testing."""
        assert isinstance(EXPECTED_MCP_SERVERS, set)


# ── build_mcp_configs ─────────────────────────────────────────────


class TestBuildMcpConfigs:
    """Test build_mcp_configs() function."""

    def test_returns_dict_of_mcp_stdio_server_config(
        self,
        three_server_config: GeoConfig,
    ) -> None:
        """build_mcp_configs returns dict[str, McpStdioServerConfig]."""
        configs = build_mcp_configs(three_server_config)
        assert isinstance(configs, dict)
        for name, cfg in configs.items():
            assert isinstance(name, str)
            assert isinstance(cfg, McpStdioServerConfig)

    def test_three_servers_built(self, three_server_config: GeoConfig) -> None:
        """All 3 configured servers are built."""
        configs = build_mcp_configs(three_server_config)
        assert len(configs) == 3
        assert set(configs.keys()) == {"geo-mcp-server", "qgis", "postgres"}

    def test_command_correctly_transferred(
        self,
        three_server_config: GeoConfig,
    ) -> None:
        """Command field is correctly transferred from config."""
        configs = build_mcp_configs(three_server_config)
        assert configs["geo-mcp-server"].command == "python"
        assert configs["qgis"].command == "python"
        assert configs["postgres"].command == "python"

    def test_args_correctly_transferred(
        self,
        three_server_config: GeoConfig,
    ) -> None:
        """Args list is correctly transferred from config."""
        configs = build_mcp_configs(three_server_config)
        assert configs["geo-mcp-server"].args == ["-m", "geo_mcp_server"]
        assert configs["qgis"].args == ["-m", "qgis_mcp_server"]
        assert configs["postgres"].args == ["-m", "postgres_mcp_server"]

    def test_env_correctly_transferred(
        self,
        three_server_config: GeoConfig,
    ) -> None:
        """Env dict is correctly transferred from config."""
        configs = build_mcp_configs(three_server_config)
        assert configs["geo-mcp-server"].env["PYTHONUTF8"] == "1"
        assert configs["geo-mcp-server"].env["AMAP_API_KEY"] == "test-key"
        assert configs["qgis"].env["PROJ_LIB"] == "/path/to/proj"

    def test_type_is_stdio(self, three_server_config: GeoConfig) -> None:
        """All server configs have type='stdio'."""
        configs = build_mcp_configs(three_server_config)
        for cfg in configs.values():
            assert cfg.type == "stdio"

    def test_single_server(self, single_server_config: GeoConfig) -> None:
        """Single server config produces single entry."""
        configs = build_mcp_configs(single_server_config)
        assert len(configs) == 1
        assert "geo-mcp-server" in configs

    def test_empty_mcp_servers(self) -> None:
        """Empty mcp_servers dict produces empty configs dict."""
        config = GeoConfig(
            model="test",
            api_key="key",
            base_url="https://test.example.com/v1",
            mcp_servers={},
        )
        configs = build_mcp_configs(config)
        assert len(configs) == 0

    def test_env_none_when_not_set(self) -> None:
        """Server config without env gets env=None."""
        config = GeoConfig(
            model="test",
            api_key="key",
            base_url="https://test.example.com/v1",
            mcp_servers={
                "test-server": McpServerConfig(
                    command="python",
                    args=["-m", "test"],
                ),
            },
        )
        configs = build_mcp_configs(config)
        # env should be None (not empty dict) per McpStdioServerConfig default
        assert configs["test-server"].env is None

    def test_cwd_transferred(self) -> None:
        """cwd field is transferred when set."""
        config = GeoConfig(
            model="test",
            api_key="key",
            base_url="https://test.example.com/v1",
            mcp_servers={
                "test-server": McpServerConfig(
                    command="python",
                    args=["-m", "test"],
                    cwd="/custom/cwd",
                ),
            },
        )
        configs = build_mcp_configs(config)
        assert configs["test-server"].cwd == "/custom/cwd"

    def test_invalid_server_skipped(self) -> None:
        """Invalid server config (missing command) is skipped, not crash."""
        config = GeoConfig(
            model="test",
            api_key="key",
            base_url="https://test.example.com/v1",
            mcp_servers={
                "good-server": McpServerConfig(
                    command="python",
                    args=["-m", "good"],
                ),
            },
        )
        # Manually add a broken entry by patching mcp_servers
        # McpServerConfig is frozen, so we use a mock that raises on command
        broken = MagicMock()
        broken.command = None  # Will cause McpStdioServerConfig validation error
        broken.args = []
        broken.env = {}
        broken.cwd = None
        config_with_bad = GeoConfig(
            model="test",
            api_key="key",
            base_url="https://test.example.com/v1",
            mcp_servers={
                "good-server": config.mcp_servers["good-server"],
                "bad-server": broken,
            },
        )
        configs = build_mcp_configs(config_with_bad)
        # Good server should still be built
        assert "good-server" in configs
        # Bad server should be skipped (not present)
        assert "bad-server" not in configs


# ── verify_mcp_isolation ──────────────────────────────────────────


class TestVerifyMcpIsolation:
    """Test verify_mcp_isolation() function."""

    def test_no_leak_returns_true(self) -> None:
        """Manager with only expected servers returns (True, empty set)."""
        manager = MockMcpManager(["geo-mcp-server", "qgis", "postgres"])
        is_isolated, leaked = verify_mcp_isolation(manager)
        assert is_isolated is True
        assert leaked == set()

    def test_leaked_server_detected(self) -> None:
        """Unexpected server is detected as leak."""
        manager = MockMcpManager([
            "geo-mcp-server",
            "qgis",
            "postgres",
            "upstream-server",  # This should be detected as leaked
        ])
        is_isolated, leaked = verify_mcp_isolation(manager)
        assert is_isolated is False
        assert "upstream-server" in leaked

    def test_leaked_server_disconnected(self) -> None:
        """Leaked servers are disconnected from the manager."""
        manager = MockMcpManager([
            "geo-mcp-server",
            "upstream-leak",
        ])
        verify_mcp_isolation(manager)
        assert "upstream-leak" in manager.disconnected

    def test_expected_servers_not_disconnected(self) -> None:
        """Expected servers are NOT disconnected."""
        manager = MockMcpManager(["geo-mcp-server", "qgis", "postgres"])
        verify_mcp_isolation(manager)
        assert len(manager.disconnected) == 0

    def test_custom_expected_servers(self) -> None:
        """Custom expected_servers set works correctly."""
        manager = MockMcpManager(["server-a", "server-b", "server-c"])
        is_isolated, leaked = verify_mcp_isolation(
            manager, expected_servers={"server-a", "server-b"}
        )
        assert is_isolated is False
        assert "server-c" in leaked

    def test_empty_manager_isolated(self) -> None:
        """Empty manager (no servers) is considered isolated."""
        manager = MockMcpManager([])
        is_isolated, leaked = verify_mcp_isolation(manager)
        assert is_isolated is True
        assert leaked == set()

    def test_all_leaked_when_expected_empty(self) -> None:
        """All servers are leaks when expected_servers is empty."""
        manager = MockMcpManager(["geo-mcp-server", "qgis"])
        is_isolated, leaked = verify_mcp_isolation(
            manager, expected_servers=set()
        )
        assert is_isolated is False
        assert leaked == {"geo-mcp-server", "qgis"}

    def test_list_statuses_error_returns_isolated(self) -> None:
        """If list_statuses() raises, return (True, set()) gracefully."""
        manager = MagicMock()
        manager.list_statuses.side_effect = RuntimeError("not connected")
        is_isolated, leaked = verify_mcp_isolation(manager)
        assert is_isolated is True
        assert leaked == set()

    def test_disconnect_error_handled(self) -> None:
        """If disconnect() raises, it's caught, not propagated."""
        manager = MockMcpManager(["geo-mcp-server", "leaked"])
        # Override disconnect to raise
        manager.disconnect = MagicMock(side_effect=RuntimeError("fail"))
        is_isolated, leaked = verify_mcp_isolation(manager)
        assert is_isolated is False
        assert "leaked" in leaked

    def test_returns_tuple(self) -> None:
        """Return type is tuple[bool, set]."""
        manager = MockMcpManager(["geo-mcp-server"])
        result = verify_mcp_isolation(manager)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], set)


# ── get_mcp_server_names ──────────────────────────────────────────


class TestGetMcpServerNames:
    """Test get_mcp_server_names() function."""

    def test_returns_list(self, three_server_config: GeoConfig) -> None:
        """Returns a list of strings."""
        names = get_mcp_server_names(three_server_config)
        assert isinstance(names, list)
        for name in names:
            assert isinstance(name, str)

    def test_three_servers(self, three_server_config: GeoConfig) -> None:
        """All 3 server names returned."""
        names = get_mcp_server_names(three_server_config)
        assert len(names) == 3
        assert set(names) == {"geo-mcp-server", "qgis", "postgres"}

    def test_single_server(self, single_server_config: GeoConfig) -> None:
        """Single server name returned."""
        names = get_mcp_server_names(single_server_config)
        assert names == ["geo-mcp-server"]

    def test_empty_config(self) -> None:
        """Empty mcp_servers returns empty list."""
        config = GeoConfig(
            model="test",
            api_key="key",
            base_url="https://test.example.com/v1",
            mcp_servers={},
        )
        names = get_mcp_server_names(config)
        assert names == []

    def test_returns_new_list_not_reference(
        self,
        three_server_config: GeoConfig,
    ) -> None:
        """Returned list is a new list, not a reference to internal dict."""
        names = get_mcp_server_names(three_server_config)
        names.append("extra")
        # Original config should not be affected
        assert "extra" not in three_server_config.mcp_servers
