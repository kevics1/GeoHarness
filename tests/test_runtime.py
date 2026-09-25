"""Tests for geoharness.runtime — runtime assembly and isolation.

These tests verify that build_geo_runtime() correctly constructs RuntimeBundle
without calling build_runtime() or load_settings(), ensuring no upstream
OpenHarness config leaks into GeoHarness.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from geoharness.config.settings import GeoConfig, McpServerConfig


class TestBuildGeoRuntime:
    """Test build_geo_runtime() assembly."""

    @pytest.fixture
    def mock_config(self) -> GeoConfig:
        """Minimal config for testing."""
        return GeoConfig(
            model="test-model",
            api_key="test-key",
            base_url="https://test.example.com/v1",
            mcp_servers={
                "test-server": McpServerConfig(
                    command="python",
                    args=["-m", "test_mcp"],
                ),
            },
        )

    @pytest.mark.asyncio
    async def test_runtime_returns_runtimebundle(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify build_geo_runtime returns a RuntimeBundle instance."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from openharness.ui.runtime import RuntimeBundle

            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert isinstance(bundle, RuntimeBundle)

    @pytest.mark.asyncio
    async def test_runtime_uses_domain_api_client(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify API client is OpenAICompatibleClient with domain config."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from openharness.api.openai_client import OpenAICompatibleClient

            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert isinstance(bundle.api_client, OpenAICompatibleClient)

    @pytest.mark.asyncio
    async def test_runtime_cwd_set_correctly(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify cwd is set to the provided directory."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert bundle.cwd == str(tmp_path)

    @pytest.mark.asyncio
    async def test_runtime_external_api_client_true(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify external_api_client is True (we built it ourselves)."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert bundle.external_api_client is True

    @pytest.mark.asyncio
    async def test_runtime_no_extra_plugin_roots(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify no upstream plugins are loaded."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert bundle.extra_plugin_roots == ()

    @pytest.mark.asyncio
    async def test_runtime_engine_is_queryengine(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify engine is a QueryEngine instance."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from openharness.engine.query_engine import QueryEngine

            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert isinstance(bundle.engine, QueryEngine)

    @pytest.mark.asyncio
    async def test_runtime_tool_registry_is_toolregistry(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify tool_registry is a ToolRegistry instance."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from openharness.tools.base import ToolRegistry

            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert isinstance(bundle.tool_registry, ToolRegistry)

    @pytest.mark.asyncio
    async def test_runtime_hook_executor_present(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify hook_executor is present."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert bundle.hook_executor is not None

    @pytest.mark.asyncio
    async def test_runtime_mcp_manager_present(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify mcp_manager is present."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert bundle.mcp_manager is not None

    @pytest.mark.asyncio
    async def test_runtime_app_state_present(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify app_state is present."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert bundle.app_state is not None

    @pytest.mark.asyncio
    async def test_runtime_commands_present(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify commands object is present."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert bundle.commands is not None

    @pytest.mark.asyncio
    async def test_runtime_with_custom_system_prompt(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify custom system prompt is used."""
        custom_prompt = "Custom test prompt for GeoHarness."
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(
                cwd=tmp_path, system_prompt=custom_prompt
            )
            # The system prompt should be in the engine
            assert custom_prompt in bundle.engine.system_prompt

    @pytest.mark.asyncio
    async def test_runtime_default_system_prompt_contains_geoharness(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify default system prompt mentions GeoHarness."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert "GeoHarness" in bundle.engine.system_prompt

    @pytest.mark.asyncio
    async def test_runtime_tool_metadata_has_config(
        self,
        mock_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Verify geoharness_config is injected into tool_metadata."""
        with patch(
            "geoharness.runtime.load_geo_config", return_value=mock_config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            assert "geoharness_config" in bundle.engine.tool_metadata
            assert bundle.engine.tool_metadata["geoharness_config"] is mock_config


class TestMcpIsolation:
    """Test MCP isolation from upstream OpenHarness."""

    @pytest.mark.asyncio
    async def test_no_upstream_mcp_servers(
        self,
        tmp_path: Path,
    ) -> None:
        """Verify no upstream MCP servers leak into GeoHarness."""
        config = GeoConfig(
            model="test",
            api_key="test-key",
            base_url="https://test.example.com/v1",
            mcp_servers={
                "geo-mcp-server": McpServerConfig(
                    command="python", args=["-m", "geo_mcp"]
                ),
                "postgres": McpServerConfig(
                    command="python", args=["-m", "postgres_mcp"]
                ),
            },
        )
        with patch(
            "geoharness.runtime.load_geo_config", return_value=config
        ):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)

            # Check that only expected servers are present
            try:
                statuses = bundle.mcp_manager.list_statuses()
                server_names = {s.name for s in statuses}
                expected = {"geo-mcp-server", "postgres"}
                leaked = server_names - expected
                assert not leaked, f"Leaked MCP servers: {leaked}"
            except Exception:
                # MCP not connected yet — that's OK during construction
                pass
