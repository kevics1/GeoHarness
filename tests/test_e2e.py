"""End-to-end integration tests for GeoHarness.

Phase 8: Verifies that all components work together:
- Runtime assembly with all components (config, API, MCP, tools, hooks, cascade)
- System prompt contains all expected sections
- MCP isolation (no upstream server leakage)
- Cascade integration (L1→L2→L3 suggestions in runtime)
- Tool metadata injection (config, data_catalog, cascade_manager)
- CLI commands (version, init, dry-run, run --help)
- Skills directory auto-inclusion
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from geoharness.config.settings import GeoConfig, McpServerConfig

# ── Full Runtime Assembly ─────────────────────────────────────────


class TestEndToEndRuntime:
    """End-to-end runtime assembly tests."""

    @pytest.fixture
    def full_config(self) -> GeoConfig:
        """Full config with all 3 MCP servers."""
        return GeoConfig(
            model="test-model",
            api_key="test-key",
            base_url="https://test.example.com/v1",
            mcp_servers={
                "geo-mcp-server": McpServerConfig(
                    command="python",
                    args=["-m", "geo_mcp_server"],
                    env={"PYTHONUTF8": "1"},
                ),
                "qgis": McpServerConfig(
                    command="python",
                    args=["-m", "qgis_mcp_server"],
                ),
                "postgres": McpServerConfig(
                    command="python",
                    args=["-m", "postgres_mcp_server"],
                ),
            },
        )

    @pytest.mark.asyncio
    async def test_full_runtime_assembly(
        self,
        full_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Full runtime assembles with all components."""
        with patch("geoharness.runtime.load_geo_config", return_value=full_config):
            from openharness.ui.runtime import RuntimeBundle

            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)

            # Verify RuntimeBundle
            assert isinstance(bundle, RuntimeBundle)

            # Verify API client
            assert bundle.api_client is not None

            # Verify MCP manager
            assert bundle.mcp_manager is not None

            # Verify tool registry
            assert bundle.tool_registry is not None

            # Verify engine
            assert bundle.engine is not None
            assert bundle.engine.system_prompt is not None
            assert len(bundle.engine.system_prompt) > 100

            # Verify tool_metadata contains all domain objects
            assert "geoharness_config" in bundle.engine.tool_metadata
            assert "data_catalog" in bundle.engine.tool_metadata
            assert "cascade_manager" in bundle.engine.tool_metadata

    @pytest.mark.asyncio
    async def test_system_prompt_contains_all_sections(
        self,
        full_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """System prompt contains all expected sections."""
        with patch("geoharness.runtime.load_geo_config", return_value=full_config):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            prompt = bundle.engine.system_prompt

            # Identity
            assert "GeoHarness" in prompt

            # 6 Geography Principles
            assert "Geography Principles" in prompt
            assert "Spatial Thinking Priority" in prompt
            assert "Cognitive Cascade" in prompt
            assert "Scale Awareness" in prompt
            assert "Spatial Heterogeneity" in prompt
            assert "Cartography as Communication" in prompt
            assert "Coordinate Discipline" in prompt

            # Skills Catalog
            assert "geo-perception" in prompt
            assert "geo-comprehension" in prompt
            assert "geo-reasoning" in prompt

            # Tool Catalog
            assert "geo_data" in prompt
            assert "geo_cartography" in prompt
            assert "geo-mcp-server" in prompt

            # Tool Classification
            assert "L1 Perception Tools" in prompt
            assert "L2 Comprehension Tools" in prompt
            assert "L3 Reasoning Tools" in prompt

            # Cascade State
            assert "级联" in prompt or "Cascade" in prompt

            # Bridge Rules
            assert "Bridge Rules" in prompt

            # Environment
            assert "Environment" in prompt
            assert str(tmp_path) in prompt

    @pytest.mark.asyncio
    async def test_cascade_manager_in_tool_metadata(
        self,
        full_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """CascadeManager is injected into tool_metadata."""
        with patch("geoharness.runtime.load_geo_config", return_value=full_config):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            cascade_manager = bundle.engine.tool_metadata["cascade_manager"]

            # Verify it's a CascadeManager
            from geoharness.hooks.cascade import CascadeManager

            assert isinstance(cascade_manager, CascadeManager)

            # Verify it starts empty
            assert cascade_manager.get_current_level() == "L0"
            assert len(cascade_manager.get_pending_suggestions()) == 0

    @pytest.mark.asyncio
    async def test_data_catalog_in_tool_metadata(
        self,
        full_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """DataCatalog is injected into tool_metadata."""
        with patch("geoharness.runtime.load_geo_config", return_value=full_config):
            from geoharness.data.catalog import DataCatalog
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            data_catalog = bundle.engine.tool_metadata["data_catalog"]

            assert isinstance(data_catalog, DataCatalog)

    @pytest.mark.asyncio
    async def test_config_in_tool_metadata(
        self,
        full_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """GeoConfig is injected into tool_metadata."""
        with patch("geoharness.runtime.load_geo_config", return_value=full_config):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            config = bundle.engine.tool_metadata["geoharness_config"]

            assert config is full_config

    @pytest.mark.asyncio
    async def test_hook_executor_has_cascade_hooks(
        self,
        full_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """HookExecutor has cascade hooks registered."""
        with patch("geoharness.runtime.load_geo_config", return_value=full_config):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)
            hook_registry = bundle.hook_executor._registry

            # USER_PROMPT_SUBMIT hooks should be registered
            from openharness.hooks.events import HookEvent

            hooks = hook_registry.get(HookEvent.USER_PROMPT_SUBMIT)
            assert len(hooks) >= 1

    @pytest.mark.asyncio
    async def test_skills_dir_auto_included(
        self,
        full_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Project skills directory is auto-included in extra_skill_dirs."""
        with patch("geoharness.runtime.load_geo_config", return_value=full_config):
            from geoharness.runtime import build_geo_runtime

            bundle = await build_geo_runtime(cwd=tmp_path)

            # extra_skill_dirs should contain the project skills directory
            assert len(bundle.extra_skill_dirs) >= 1
            project_skills = bundle.extra_skill_dirs[0]
            assert "skills" in project_skills

    @pytest.mark.asyncio
    async def test_mcp_isolation_verified(
        self,
        full_config: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """MCP isolation is verified during runtime assembly."""
        with patch("geoharness.runtime.load_geo_config", return_value=full_config):
            from geoharness.runtime import build_geo_runtime

            # Should not raise any exception
            bundle = await build_geo_runtime(cwd=tmp_path)
            assert bundle.mcp_manager is not None


# ── CLI Integration ───────────────────────────────────────────────


class TestCLIIntegration:
    """CLI command integration tests."""

    def test_version_command(self) -> None:
        """geoh version shows version."""
        from typer.testing import CliRunner

        from geoharness import __version__
        from geoharness.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_run_help(self) -> None:
        """geoh run --help shows help."""
        from typer.testing import CliRunner

        from geoharness.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["run", "--help"])
        assert result.exit_code == 0
        assert "TUI" in result.output or "tui" in result.output.lower()

    def test_init_help(self) -> None:
        """geoh init --help shows help."""
        from typer.testing import CliRunner

        from geoharness.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["init", "--help"])
        assert result.exit_code == 0

    def test_dry_run_help(self) -> None:
        """geoh dry-run --help shows help."""
        from typer.testing import CliRunner

        from geoharness.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["dry-run", "--help"])
        assert result.exit_code == 0


# ── Cascade End-to-End ────────────────────────────────────────────


class TestCascadeEndToEnd:
    """End-to-end cascade integration tests."""

    def test_l1_to_l2_to_l3_full_cascade(self) -> None:
        """Full L1→L2→L3 cascade flow."""
        from geoharness.hooks.cascade import CascadeManager

        manager = CascadeManager()

        # L1: use spatial relation tool
        manager.record_tool_use("geo_spatial_relation")
        l1_suggestions = manager.get_pending_suggestions()
        assert len(l1_suggestions) == 1
        assert l1_suggestions[0].target_level == "L2"

        # Consume L1→L2 suggestion
        manager.consume_suggestions()

        # L2: use moran_local (triggered by L1 suggestion)
        manager.record_tool_use("geo_moran_local")
        l2_suggestions = manager.get_pending_suggestions()
        assert len(l2_suggestions) == 1
        assert l2_suggestions[0].target_level == "L3"

        # Consume L2→L3 suggestion
        manager.consume_suggestions()

        # L3: use cluster_detect (triggered by L2 suggestion)
        manager.record_tool_use("geo_cluster_detect")
        # L3 tools don't generate suggestions (highest level)
        l3_suggestions = manager.get_pending_suggestions()
        assert len(l3_suggestions) == 0

        # Verify level progression
        assert manager.get_current_level() == "L3"

    def test_cascade_prompt_section_evolves(self) -> None:
        """Cascade prompt section evolves as tools are used."""
        from geoharness.hooks.cascade import CascadeManager

        manager = CascadeManager()

        # Initial state: L0
        section = manager.get_prompt_section()
        assert "L0" in section

        # After L1 tool: L1
        manager.record_tool_use("geo_data")
        section = manager.get_prompt_section()
        assert "L1" in section

        # After L2 tool: L2
        manager.record_tool_use("geo_moran_local")
        section = manager.get_prompt_section()
        assert "L2" in section

        # After L3 tool: L3
        manager.record_tool_use("geo_cluster_detect")
        section = manager.get_prompt_section()
        assert "L3" in section


# ── System Prompt End-to-End ──────────────────────────────────────


class TestSystemPromptEndToEnd:
    """End-to-end system prompt tests."""

    def test_prompt_with_active_cascade(self) -> None:
        """System prompt includes active cascade suggestions."""
        from geoharness.config.settings import GeoConfig
        from geoharness.hooks.cascade import CascadeManager
        from geoharness.prompts.system_prompt import build_geo_system_prompt

        config = GeoConfig(
            model="test",
            api_key="key",
            base_url="https://test.example.com/v1",
        )
        manager = CascadeManager()
        manager.record_tool_use("geo_spatial_relation")

        prompt = build_geo_system_prompt(Path("/test"), config, manager)

        # Should contain cascade suggestion
        assert "L2" in prompt
        assert "geo-comprehension" in prompt

    def test_prompt_with_custom_bridge_rules(self) -> None:
        """System prompt includes custom bridge rules from config."""
        from geoharness.config.settings import (
            BridgeRule,
            CartographyConfig,
            GeoConfig,
        )
        from geoharness.prompts.system_prompt import build_geo_system_prompt

        config = GeoConfig(
            model="test",
            api_key="key",
            base_url="https://test.example.com/v1",
            cartography=CartographyConfig(
                default_template="/custom/template.qpt",
                bridge_rules={
                    "custom_analysis": BridgeRule(
                        render_method="graduated",
                        color_scheme="Viridis",
                        n_classes=7,
                    ),
                },
            ),
        )

        prompt = build_geo_system_prompt(Path("/test"), config)

        assert "custom_analysis" in prompt
        assert "Viridis" in prompt
        assert "graduated" in prompt
        assert "/custom/template.qpt" in prompt

    def test_prompt_sections_in_order(self) -> None:
        """System prompt sections appear in the correct order."""
        from geoharness.config.settings import GeoConfig
        from geoharness.prompts.system_prompt import build_geo_system_prompt

        config = GeoConfig(
            model="test",
            api_key="key",
            base_url="https://test.example.com/v1",
        )
        prompt = build_geo_system_prompt(Path("/test"), config)

        # Find section positions
        identity_pos = prompt.find("GeoHarness")
        principles_pos = prompt.find("Geography Principles")
        skills_pos = prompt.find("Cognitive Skills")
        tools_pos = prompt.find("Tool Catalog")
        env_pos = prompt.find("Environment")

        # Verify ordering
        assert identity_pos < principles_pos
        assert principles_pos < skills_pos
        assert skills_pos < tools_pos
        assert tools_pos < env_pos
