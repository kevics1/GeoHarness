"""Tests for geoharness.config.settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from geoharness.config.settings import (
    GeoConfig,
    load_geo_config,
)


class TestGeoConfigDefaults:
    """Test default values of GeoConfig."""

    def test_default_config(self):
        config = GeoConfig()
        assert config.model == "deepseek-chat"
        assert config.api_key == ""
        assert config.base_url == ""
        assert config.mcp_servers == {}
        assert config.cartography.default_template == ""
        assert config.data.default_crs == "EPSG:4326"
        assert config.data.analysis_crs == "EPSG:3857"

    def test_frozen(self):
        config = GeoConfig()
        with pytest.raises(AttributeError):
            config.model = "changed"


class TestLoadGeoConfig:
    """Test load_geo_config function."""

    def test_load_from_file(self, config_file: Path):
        config = load_geo_config(config_file)
        assert config.model == "test-model"
        assert config.api_key == "test-key-12345"
        assert config.base_url == "https://test.api.example.com/v1"

    def test_load_missing_file_returns_defaults(
        self, temp_config_dir: Path
    ):
        missing_path = temp_config_dir / "nonexistent.yaml"
        config = load_geo_config(missing_path)
        assert config.model == "deepseek-chat"
        assert config.api_key == ""

    def test_load_empty_file_returns_defaults(
        self, temp_config_dir: Path
    ):
        empty_path = temp_config_dir / "empty.yaml"
        empty_path.write_text("", encoding="utf-8")
        config = load_geo_config(empty_path)
        assert config.model == "deepseek-chat"

    def test_mcp_servers_parsed(self, config_file: Path):
        config = load_geo_config(config_file)
        assert "geo-mcp-server" in config.mcp_servers
        assert "qgis" in config.mcp_servers

        geo_mcp = config.mcp_servers["geo-mcp-server"]
        assert geo_mcp.command == "python"
        assert geo_mcp.args == ["-m", "geo_mcp_server"]
        assert geo_mcp.env == {"PYTHONUTF8": "1"}

        qgis_mcp = config.mcp_servers["qgis"]
        assert qgis_mcp.command == "python"
        assert qgis_mcp.args == ["-m", "qgis_mcp_server"]

    def test_cartography_parsed(self, config_file: Path):
        config = load_geo_config(config_file)
        assert config.cartography.default_template == "F:/test/template.qpt"

        rules = config.cartography.bridge_rules
        assert "moran_local" in rules
        assert rules["moran_local"].render_method == "categorized"
        assert rules["moran_local"].color_scheme == "RdBu"
        assert rules["moran_local"].n_classes == 5

        assert "getis_ord" in rules
        assert rules["getis_ord"].render_method == "graduated"
        assert rules["getis_ord"].color_scheme == "YlOrRd"
        assert rules["getis_ord"].n_classes == 6

    def test_cognition_parsed(self, config_file: Path):
        config = load_geo_config(config_file)
        cascade = config.cognition.cascade
        assert cascade.l1_to_l2_tools == ["geo_spatial_relation"]
        assert cascade.l2_to_l3_tools == ["geo_moran_local"]
        assert cascade.suggestion_prompt == "test suggestion"

    def test_data_parsed(self, config_file: Path):
        config = load_geo_config(config_file)
        assert config.data.default_crs == "EPSG:4326"
        assert config.data.analysis_crs == "EPSG:3857"
        assert config.data.postgis_dsn == (
            "postgresql://user:pass@localhost:5432/testdb"
        )

    def test_permissions_parsed(self, config_file: Path):
        config = load_geo_config(config_file)
        assert "~/.geoharness/workspaces/" in config.permissions.allow_write_paths
        assert "~/.ssh/" in config.permissions.deny_paths


class TestEnvVarExpansion:
    """Test ${VAR} environment variable expansion."""

    def test_env_var_expansion(
        self, temp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("GEOH_TEST_MODEL", "expanded-model")
        monkeypatch.setenv("GEOH_TEST_KEY", "expanded-key")

        config_path = temp_config_dir / "config.yaml"
        config_path.write_text(
            "model: ${GEOH_TEST_MODEL}\n"
            "api_key: ${GEOH_TEST_KEY}\n",
            encoding="utf-8",
        )
        config = load_geo_config(config_path)
        assert config.model == "expanded-model"
        assert config.api_key == "expanded-key"

    def test_unset_var_keeps_placeholder(
        self, temp_config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.delenv("GEOH_UNSET_VAR", raising=False)

        config_path = temp_config_dir / "config.yaml"
        config_path.write_text(
            "model: ${GEOH_UNSET_VAR}\n",
            encoding="utf-8",
        )
        config = load_geo_config(config_path)
        assert config.model == "${GEOH_UNSET_VAR}"

    def test_env_file_loaded(
        self, config_file: Path, env_file: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Clear any existing env vars
        monkeypatch.delenv("GEOH_MODEL", raising=False)
        monkeypatch.delenv("GEOH_API_KEY", raising=False)
        monkeypatch.delenv("GEOH_BASE_URL", raising=False)

        # Write config that uses ${} references
        config_file.write_text(
            "model: ${GEOH_MODEL}\n"
            "api_key: ${GEOH_API_KEY}\n"
            "base_url: ${GEOH_BASE_URL}\n",
            encoding="utf-8",
        )

        config = load_geo_config(config_file)
        assert config.model == "env-model"
        assert config.api_key == "env-key"
        assert config.base_url == "https://env.api.example.com"
