"""Tests for Phase 7: Frontend integration — system prompt and launcher.

Verifies that:
- build_geo_system_prompt() produces a complete prompt with all sections
- System prompt contains 6 geography principles
- System prompt contains cognitive skills catalog
- System prompt contains tool catalog with L1/L2/L3 classification
- System prompt contains cascade state from CascadeManager
- System prompt contains bridge rules from config
- System prompt contains environment info
- Launcher returns error code when no API key configured
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from geoharness.config.settings import (
    BridgeRule,
    CartographyConfig,
    GeoConfig,
)
from geoharness.hooks.cascade import CascadeManager
from geoharness.prompts.system_prompt import (
    _build_bridge_rules_section,
    _build_cascade_section,
    _build_environment_section,
    _build_tool_classification_section,
    build_geo_system_prompt,
)

# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def default_config() -> GeoConfig:
    """Default GeoConfig for testing."""
    return GeoConfig(
        model="test-model",
        api_key="test-key",
        base_url="https://test.example.com/v1",
    )


@pytest.fixture
def config_with_bridge_rules() -> GeoConfig:
    """GeoConfig with custom bridge rules."""
    return GeoConfig(
        model="test-model",
        api_key="test-key",
        base_url="https://test.example.com/v1",
        cartography=CartographyConfig(
            default_template="F:/test/template.qpt",
            bridge_rules={
                "moran_local": BridgeRule(
                    render_method="categorized",
                    color_scheme="RdBu",
                    n_classes=5,
                ),
                "getis_ord": BridgeRule(
                    render_method="graduated",
                    color_scheme="YlOrRd",
                    n_classes=6,
                ),
            },
        ),
    )


@pytest.fixture
def cascade_manager_with_history() -> CascadeManager:
    """CascadeManager with some tool usage history."""
    manager = CascadeManager()
    manager.record_tool_use("geo_spatial_relation")
    return manager


# ── build_geo_system_prompt ───────────────────────────────────────


class TestBuildGeoSystemPrompt:
    """Test build_geo_system_prompt() function."""

    def test_returns_non_empty_string(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """System prompt is a non-empty string."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert isinstance(prompt, str)
        assert len(prompt) > 100

    def test_contains_identity(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt contains GeoHarness identity."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "GeoHarness" in prompt
        assert "spatial analysis" in prompt.lower()

    def test_contains_geography_principles_header(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt contains geography principles section."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "Geography Principles" in prompt

    def test_contains_all_six_principles(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt contains all 6 geography principles."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "Spatial Thinking Priority" in prompt
        assert "Cognitive Cascade" in prompt
        assert "Scale Awareness" in prompt or "MAUP" in prompt
        assert "Spatial Heterogeneity" in prompt
        assert "Cartography as Communication" in prompt
        assert "Coordinate Discipline" in prompt

    def test_contains_skills_catalog(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt contains cognitive skills catalog."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "Cognitive Skills" in prompt
        assert "geo-perception" in prompt
        assert "geo-comprehension" in prompt
        assert "geo-reasoning" in prompt

    def test_contains_tool_catalog(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt contains tool catalog."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "Tool Catalog" in prompt
        assert "geo_data" in prompt
        assert "geo_cartography" in prompt
        assert "geo-mcp-server" in prompt
        assert "postgres" in prompt

    def test_contains_tool_classification(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt contains L1/L2/L3 tool classification."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "Tool Classification" in prompt
        assert "L1 Perception Tools" in prompt
        assert "L2 Comprehension Tools" in prompt
        assert "L3 Reasoning Tools" in prompt

    def test_contains_cascade_state_with_manager(
        self,
        default_config: GeoConfig,
        cascade_manager_with_history: CascadeManager,
        tmp_path: Path,
    ) -> None:
        """Prompt contains cascade state when CascadeManager is provided."""
        prompt = build_geo_system_prompt(
            tmp_path, default_config, cascade_manager_with_history
        )
        assert "认知级联" in prompt or "Cascade" in prompt
        assert "L1" in prompt

    def test_contains_cascade_state_without_manager(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt contains cascade section even without CascadeManager."""
        prompt = build_geo_system_prompt(tmp_path, default_config, None)
        assert "Cascade" in prompt or "级联" in prompt

    def test_contains_bridge_rules(
        self,
        config_with_bridge_rules: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Prompt contains bridge rules from config."""
        prompt = build_geo_system_prompt(tmp_path, config_with_bridge_rules)
        assert "Bridge Rules" in prompt
        assert "moran_local" in prompt
        assert "RdBu" in prompt
        assert "getis_ord" in prompt
        assert "YlOrRd" in prompt

    def test_contains_default_template(
        self,
        config_with_bridge_rules: GeoConfig,
        tmp_path: Path,
    ) -> None:
        """Prompt contains default template path."""
        prompt = build_geo_system_prompt(tmp_path, config_with_bridge_rules)
        assert "F:/test/template.qpt" in prompt

    def test_contains_environment_info(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt contains environment info."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "Environment" in prompt
        assert str(tmp_path) in prompt

    def test_contains_coordinate_discipline(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt mentions GCJ-02 and WGS84 coordinate systems."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "GCJ-02" in prompt
        assert "WGS84" in prompt

    def test_contains_maup(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt mentions MAUP."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "MAUP" in prompt or "Modifiable Areal Unit" in prompt

    def test_contains_skip_authority(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt mentions model's skip authority for cascade."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "skip" in prompt.lower() or "跳过" in prompt

    def test_contains_l2_tools_priority(
        self, default_config: GeoConfig, tmp_path: Path
    ) -> None:
        """Prompt mentions L2 tools geometries parameter priority."""
        prompt = build_geo_system_prompt(tmp_path, default_config)
        assert "geometries" in prompt


# ── Section Builder Functions ─────────────────────────────────────


class TestSectionBuilders:
    """Test individual section builder functions."""

    def test_environment_section_contains_os(self, tmp_path: Path) -> None:
        """_build_environment_section contains OS info."""
        section = _build_environment_section(tmp_path)
        assert "OS:" in section
        assert str(tmp_path) in section

    def test_environment_section_contains_python(self, tmp_path: Path) -> None:
        """_build_environment_section contains Python version."""
        section = _build_environment_section(tmp_path)
        assert "Python:" in section

    def test_cascade_section_with_manager(self) -> None:
        """_build_cascade_section with CascadeManager returns prompt section."""
        manager = CascadeManager()
        manager.record_tool_use("geo_data")
        section = _build_cascade_section(manager)
        assert "认知级联" in section or "Cascade" in section
        assert "L1" in section

    def test_cascade_section_without_manager(self) -> None:
        """_build_cascade_section without CascadeManager returns fallback."""
        section = _build_cascade_section(None)
        assert "No cascade state" in section

    def test_bridge_rules_section_with_rules(
        self, config_with_bridge_rules: GeoConfig
    ) -> None:
        """_build_bridge_rules_section lists configured rules."""
        section = _build_bridge_rules_section(config_with_bridge_rules)
        assert "moran_local" in section
        assert "categorized" in section
        assert "RdBu" in section

    def test_bridge_rules_section_empty(self, default_config: GeoConfig) -> None:
        """_build_bridge_rules_section shows default when no rules."""
        section = _build_bridge_rules_section(default_config)
        assert "default" in section.lower()

    def test_tool_classification_section(self) -> None:
        """_build_tool_classification_section lists all L1/L2/L3 tools."""
        section = _build_tool_classification_section()
        assert "L1 Perception Tools" in section
        assert "L2 Comprehension Tools" in section
        assert "L3 Reasoning Tools" in section
        assert "geo_data" in section
        assert "geo_moran_local" in section
        assert "geo_cluster_detect" in section


# ── Launcher ──────────────────────────────────────────────────────


class TestLauncher:
    """Test launcher functions."""

    def test_launch_returns_error_without_api_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """launch_geo_tui returns 1 when no API key configured."""
        # Patch Path.home to use tmp_path
        config_dir = tmp_path / ".geoharness"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            "model: test\napi_key: ''\nbase_url: ''\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        import asyncio

        from geoharness.launcher import launch_geo_tui

        exit_code = asyncio.run(launch_geo_tui(cwd=tmp_path))
        assert exit_code == 1

    @pytest.mark.asyncio
    async def test_launch_print_mode_with_valid_config(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """launch_geo_tui in print mode builds runtime."""
        config_dir = tmp_path / ".geoharness"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            "model: test\napi_key: fake-key\nbase_url: https://test.example.com/v1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # Mock bundle; MCP activation is stubbed at the launcher level.
        mock_bundle = MagicMock()
        mock_bundle.engine = MagicMock()

        with patch(
            "geoharness.runtime.build_geo_runtime",
            new_callable=AsyncMock,
            return_value=mock_bundle,
        ) as mock_build:
            with patch(
                "geoharness.launcher.activate_mcp",
                new_callable=AsyncMock,
            ) as mock_activate:
                with patch(
                    "openharness.ui.runtime.start_runtime",
                    new_callable=AsyncMock,
                ):
                    with patch(
                        "openharness.ui.runtime.close_runtime",
                        new_callable=AsyncMock,
                    ):
                        from geoharness.launcher import launch_geo_tui

                        # Mock input() to return 'exit' immediately
                        monkeypatch.setattr("builtins.input", lambda _: "exit")

                        exit_code = await launch_geo_tui(
                            cwd=tmp_path, print_mode=True
                        )
                        assert exit_code == 0
                        mock_build.assert_called_once()
                        mock_activate.assert_awaited_once_with(mock_bundle)

    @pytest.mark.asyncio
    async def test_launch_tui_mode_default(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """launch_geo_tui defaults to React TUI and writes the isolated settings.json."""
        config_dir = tmp_path / ".geoharness"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            "model: test-model\napi_key: fake-key\nbase_url: https://test.example.com/v1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        # The launcher writes os.environ directly — pre-register keys so
        # monkeypatch cleans them up and nothing leaks into other tests.
        monkeypatch.delenv("OPENHARNESS_CONFIG_DIR", raising=False)
        monkeypatch.delenv("OPENHARNESS_PROFILE", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        # Mock _run_react_tui to avoid actually launching the TUI
        with patch(
            "geoharness.launcher._run_react_tui",
            new_callable=AsyncMock,
            return_value=0,
        ) as mock_tui:
            from geoharness.launcher import launch_geo_tui

            exit_code = await launch_geo_tui(cwd=tmp_path)
            assert exit_code == 0
            mock_tui.assert_called_once()

            # New isolation strategy: point upstream OpenHarness at
            # ~/.geoharness/ (OPENHARNESS_CONFIG_DIR) instead of relying on
            # the old OPENHARNESS_PROFILE switch.
            import json
            import os

            assert os.environ.get("OPENHARNESS_CONFIG_DIR") == str(config_dir)
            assert os.environ.get("OPENHARNESS_PROFILE") is None
            assert os.environ.get("OPENAI_API_KEY") == "fake-key"

            # The generated settings.json IS the isolation artifact: OpenAI
            # format, GeoHarness credentials, and no credential_slot overrides
            # (which would hijack resolve_auth() in the TUI backend).
            settings = json.loads(
                (config_dir / "settings.json").read_text(encoding="utf-8")
            )
            assert settings["api_format"] == "openai"
            assert settings["provider"] == "openai"
            assert settings["api_key"] == "fake-key"
            assert settings["base_url"] == "https://test.example.com/v1"
            assert settings["model"] == "test-model"
            assert all(
                profile.get("credential_slot") is None
                for profile in settings["profiles"].values()
            )


# ── Backend ───────────────────────────────────────────────────────


class TestBackend:
    """Test backend functions."""

    @pytest.mark.asyncio
    async def test_start_geo_backend_returns_bundle(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """start_geo_backend returns a RuntimeBundle."""
        config_dir = tmp_path / ".geoharness"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            "model: test\napi_key: fake-key\nbase_url: https://test.example.com/v1\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with patch(
            "geoharness.backend.build_geo_runtime",
            new_callable=AsyncMock,
        ) as mock_build:
            with patch(
                "geoharness.backend.activate_mcp",
                new_callable=AsyncMock,
            ) as mock_activate:
                with patch(
                    "openharness.ui.runtime.start_runtime",
                    new_callable=AsyncMock,
                ):
                    from geoharness.backend import start_geo_backend

                    mock_bundle = AsyncMock()
                    mock_build.return_value = mock_bundle

                    bundle = await start_geo_backend(cwd=tmp_path)
                    assert bundle is mock_bundle
                    mock_build.assert_called_once()
                    mock_activate.assert_awaited_once_with(mock_bundle)

    @pytest.mark.asyncio
    async def test_stop_geo_backend_calls_close(
        self,
    ) -> None:
        """stop_geo_backend calls close_runtime."""
        # Patch at backend module level since close_runtime is imported at top
        with patch(
            "geoharness.backend.close_runtime",
            new_callable=AsyncMock,
        ) as mock_close:
            from geoharness.backend import stop_geo_backend

            mock_bundle = AsyncMock()
            await stop_geo_backend(mock_bundle)
            mock_close.assert_called_once_with(mock_bundle)


# ── CLI run command ───────────────────────────────────────────────


class TestCLIRun:
    """Test CLI run command integration."""

    def test_run_command_exists(self) -> None:
        """CLI has a 'run' command."""
        from typer.testing import CliRunner

        from geoharness.cli import app

        runner = CliRunner()
        result = runner.invoke(app, ["run", "--help"])
        # Should not error — command exists and shows help
        assert result.exit_code == 0
        assert "TUI" in result.output or "tui" in result.output.lower()
