"""Pytest fixtures for GeoHarness tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def temp_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a temporary ~/.geoharness/ config directory."""
    config_dir = tmp_path / ".geoharness"
    config_dir.mkdir(parents=True)

    # Patch Path.home() to return tmp_path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    return config_dir


@pytest.fixture
def sample_config_yaml() -> str:
    """Return a sample config.yaml content for testing."""
    return """\
model: test-model
api_key: test-key-12345
base_url: https://test.api.example.com/v1

mcp:
  geo-mcp-server:
    command: python
    args: ["-m", "geo_mcp_server"]
    env:
      PYTHONUTF8: "1"
  qgis:
    command: python
    args: ["-m", "qgis_mcp_server"]

cartography:
  default_template: "F:/test/template.qpt"
  bridge_rules:
    moran_local:
      render_method: categorized
      color_scheme: RdBu
      n_classes: 5
    getis_ord:
      render_method: graduated
      color_scheme: YlOrRd
      n_classes: 6

cognition:
  cascade:
    l1_to_l2_tools: ["geo_spatial_relation"]
    l2_to_l3_tools: ["geo_moran_local"]
    suggestion_prompt: "test suggestion"

data:
  default_crs: EPSG:4326
  analysis_crs: EPSG:3857
  postgis_dsn: "postgresql://user:pass@localhost:5432/testdb"

permissions:
  allow_write_paths:
    - ~/.geoharness/workspaces/
  deny_paths:
    - ~/.ssh/
"""


@pytest.fixture
def config_file(
    temp_config_dir: Path, sample_config_yaml: str
) -> Path:
    """Write sample config.yaml to temp config dir."""
    config_path = temp_config_dir / "config.yaml"
    config_path.write_text(sample_config_yaml, encoding="utf-8")
    return config_path


@pytest.fixture
def env_file(temp_config_dir: Path) -> Path:
    """Write sample .env to temp config dir."""
    env_path = temp_config_dir / ".env"
    env_path.write_text(
        "GEOH_MODEL=env-model\n"
        "GEOH_API_KEY=env-key\n"
        "GEOH_BASE_URL=https://env.api.example.com\n",
        encoding="utf-8",
    )
    return env_path
