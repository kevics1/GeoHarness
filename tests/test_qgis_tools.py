"""QGIS tool/config contracts without requiring native QGIS in pytest's Python."""

from unittest.mock import AsyncMock

import pytest
from openharness.tools.base import ToolExecutionContext
from pydantic import ValidationError

from geoharness.config.settings import DataConfig, GeoConfig, QgisConfig, load_geo_config
from geoharness.tools.geo_qgis import GeoQgisInput, GeoQgisTool, qgis_read_roots


@pytest.mark.parametrize("name", ["geo_qgis", "geo_qgis_project"])
def test_qgis_workflow_runs_without_per_call_approval_and_respects_denials(name, tmp_path):
    from geoharness.runtime import _build_permission_checker

    checker = _build_permission_checker(GeoConfig())
    decision = checker.evaluate(name, is_read_only=False)
    assert decision.allowed and not decision.requires_confirmation
    credential = checker.evaluate(
        name, is_read_only=False, file_path=str(tmp_path / '.ssh/id_rsa'),
    )
    assert not credential.allowed
    checker._settings.denied_tools.append(name)
    assert not checker.evaluate(name, is_read_only=False).allowed


def test_qgis_config_reads_paths_from_yaml_and_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GEOH_TEST_QGIS_PATH", str(tmp_path / "qgis-install"))
    path = tmp_path / "config.yaml"
    path.write_text(
        "qgis:\n  install_dir: ${GEOH_TEST_QGIS_PATH}\n  timeout_seconds: 240\n"
        "  max_search_results: 12\n", encoding="utf-8",
    )
    config = load_geo_config(path)
    assert config.qgis.install_dir == str(tmp_path / "qgis-install")
    assert config.qgis.timeout_seconds == 240
    assert config.qgis.max_search_results == 12
    assert config.qgis.enabled


@pytest.mark.parametrize("value", [{"timeout_seconds": 0}, {"max_search_results": 0},
                                  {"unknown_setting": True}])
def test_qgis_config_rejects_invalid_settings(value):
    with pytest.raises(ValidationError):
        QgisConfig.model_validate(value)


@pytest.mark.parametrize("value", [
    {"action": "run"}, {"action": "help"}, {"action": "model"},
    {"action": "search", "parameters": {"INPUT": "data.shp"}},
])
def test_qgis_action_needs_complete_parameters(value):
    with pytest.raises(ValidationError):
        GeoQgisInput.model_validate(value)


def test_read_roots_include_configured_data_and_qgis_artifacts(tmp_path):
    data = tmp_path.parent / "external-data"
    roots = qgis_read_roots(GeoConfig(data=DataConfig(file_dir=str(data))), tmp_path)
    assert data.resolve() in roots
    assert (tmp_path / ".geoharness" / "qgis").resolve() in roots
    assert len(roots) == len(set(roots))


@pytest.mark.asyncio
async def test_qgis_tool_uses_runtime_config_and_exact_query(tmp_path, monkeypatch):
    from geoharness.qgis import runner

    execute = AsyncMock(return_value={"status": "succeeded", "result": {"total": 2}})
    monkeypatch.setattr(runner, "execute_qgis", execute)
    settings = QgisConfig(max_search_results=7)
    config = GeoConfig(qgis=settings)
    result = await GeoQgisTool().execute(
        GeoQgisInput(action="search", query="buffer"),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config}),
    )
    assert not result.is_error and result.metadata["total"] == 2
    args, kwargs = execute.call_args
    assert args[0] == "search" and args[1]["query"] == "buffer" and args[1]["limit"] == 7
    assert kwargs["settings"] is settings and kwargs["cwd"] == tmp_path


@pytest.mark.asyncio
async def test_worker_failure_is_not_reported_as_success(tmp_path, monkeypatch):
    from geoharness.qgis import runner

    execute = AsyncMock(return_value={
        "status": "failed", "error_summary": "Unknown algorithm", "result": {},
    })
    monkeypatch.setattr(runner, "execute_qgis", execute)
    result = await GeoQgisTool().execute(
        GeoQgisInput(action="run", algorithm_id="fake:missing"),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()}),
    )
    assert result.is_error and "Unknown algorithm" in result.output


@pytest.mark.asyncio
async def test_disabled_qgis_gives_explicit_error(tmp_path):
    result = await GeoQgisTool().execute(
        GeoQgisInput(action="doctor"), ToolExecutionContext(
            cwd=tmp_path,
            metadata={"geoharness_config": GeoConfig(qgis=QgisConfig(enabled=False))},
        ),
    )
    assert result.is_error and "disabled" in result.output


def test_query_is_read_only_but_processing_is_not():
    tool = GeoQgisTool()
    assert tool.is_read_only(GeoQgisInput(action="help", algorithm_id="native:buffer"))
    assert not tool.is_read_only(GeoQgisInput(action="run", algorithm_id="native:buffer"))
