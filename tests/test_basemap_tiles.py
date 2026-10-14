"""Small offline tile fixtures check both Tianditu grids and publication safety."""

import asyncio
import io
import json
from threading import Event
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
import rasterio
from openharness.tools.base import ToolExecutionContext
from PIL import Image

from geoharness.data import basemaps
from geoharness.data.basemaps import BasemapConfig, BasemapSource, fetch_basemap
from geoharness.tools.geo_basemap import GeoBasemapInput, GeoBasemapTool


def configuration(grid="geodetic", **kwargs):
    return BasemapConfig(enabled=True, providers={"test": BasemapSource(
        label="Test source", url_template="https://example.invalid/{z}/{x}/{y}?key={token}",
        token_env="GEOH_TEST_BASEMAP_TOKEN", grid=grid, min_zoom=1, max_zoom=4,
        attribution="Offline fixture")}, **kwargs)


@pytest.fixture
def fake_tiles(monkeypatch):
    monkeypatch.setenv("GEOH_TEST_BASEMAP_TOKEN", "offline-test-credential")
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), (12, 34, 56)).save(buffer, format="PNG")
    calls = []

    def download(url, **kwargs):
        calls.append((url, kwargs))
        return buffer.getvalue()

    monkeypatch.setattr(basemaps, "_download_tile", download)
    return calls


@pytest.mark.parametrize("grid,crs", [("geodetic", "EPSG:4490"),
                                     ("web_mercator", "EPSG:3857")])
def test_both_grids_produce_correct_local_raster(tmp_path, fake_tiles, grid, crs):
    result = fetch_basemap(configuration(grid), tmp_path, "test", [100, 20, 110, 30], zoom=3)
    assert result["crs"] == crs and result["tile_count"] == 1
    with rasterio.open(result["file_path"]) as dataset:
        assert dataset.count == 3 and dataset.width == 256 and dataset.height == 256
        assert dataset.read()[:, 0, 0].tolist() == [12, 34, 56]
        if grid == "geodetic":
            assert list(dataset.bounds) == [90, 0, 135, 45]
    cached = fetch_basemap(configuration(grid), tmp_path, "test", [100, 20, 110, 30], zoom=3)
    assert cached["cached"] and len(fake_tiles) == 1
    assert "offline-test-credential" not in json.dumps(result)
    assert "example.invalid" not in json.dumps(result)


def test_limits_bad_tiles_credentials_and_workspace(tmp_path, fake_tiles, monkeypatch):
    with pytest.raises(ValueError, match="exceeds"):
        fetch_basemap(configuration(max_tiles=1), tmp_path, "test", [-170, -70, 170, 70], zoom=4)
    assert not fake_tiles
    result = fetch_basemap(configuration(max_tiles=2), tmp_path, "test", [100, 20, 110, 30])
    assert result["tile_count"] <= 2
    fake_tiles.clear()
    with pytest.raises(ValueError, match="date.line"):
        fetch_basemap(configuration(), tmp_path, "test", [170, -10, -170, 10])
    with pytest.raises(ValueError, match="subdirectory"):
        fetch_basemap(configuration(cache_dir="../outside"), tmp_path, "test", [100, 20, 110, 30])
    monkeypatch.setattr(basemaps, "_download_tile", lambda *args, **kwargs: b"<xml>denied</xml>")
    with pytest.raises(ValueError, match="PNG/JPEG"):
        fetch_basemap(configuration(), tmp_path, "test", [100, 20, 110, 30], zoom=2)
    assert not list((tmp_path / ".geoharness/basemaps").glob(".staging-*"))
    monkeypatch.delenv("GEOH_TEST_BASEMAP_TOKEN")
    with pytest.raises(ValueError, match="unset"):
        fetch_basemap(configuration(), tmp_path, "test", [100, 20, 110, 30])


async def test_tool_registers_profile_without_exposing_template(tmp_path, fake_tiles):
    context = ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": SimpleNamespace(basemaps=configuration())})
    tool = GeoBasemapTool()
    listing = await tool.execute(GeoBasemapInput(), context)
    assert "example.invalid" not in listing.output
    result = await tool.execute(GeoBasemapInput(action="fetch", provider="test",
                               bounds=[100, 20, 110, 30], zoom=3), context)
    assert not result.is_error, result.output
    assert context.metadata["geo_data_context"]["profiles"]
    assert result.metadata["layer_spec"]["raster_style"]["renderer"] == "default"


def test_network_errors_are_redacted_and_redirects_rejected(monkeypatch):
    url = "https://example.invalid/?key=offline-test-credential"

    def fail(*args, **kwargs):
        raise HTTPError(url, 403, "failure contains " + url, {}, None)

    monkeypatch.setattr(basemaps, "build_opener", lambda *args: SimpleNamespace(open=fail))
    with pytest.raises(ValueError) as caught:
        basemaps._download_tile(url, timeout=1, user_agent="offline test")
    assert str(caught.value) == "Tile service HTTP status 403."
    assert basemaps._NoRedirect().redirect_request(None, None, 302, "", {}, url) is None


async def test_cancel_stops_download_before_publication(tmp_path, fake_tiles, monkeypatch):
    entered, release, finished = Event(), Event(), Event()
    download = basemaps._download_tile
    fetch = fetch_basemap

    def blocked_download(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return download(*args, **kwargs)

    def tracked_fetch(*args, **kwargs):
        try:
            return fetch(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(basemaps, "_download_tile", blocked_download)
    monkeypatch.setattr("geoharness.tools.geo_basemap.fetch_basemap", tracked_fetch)
    context = ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": SimpleNamespace(basemaps=configuration())})
    task = asyncio.create_task(GeoBasemapTool().execute(GeoBasemapInput(
        action="fetch", provider="test", bounds=[100, 20, 110, 30], zoom=3), context))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    assert not list((tmp_path / ".geoharness/basemaps").iterdir())
    assert not context.metadata.get("geo_data_context")


def test_optional_tool_registration_and_config_loading(tmp_path):
    from geoharness.config.settings import GeoConfig, load_geo_config
    from geoharness.tools import build_geo_tool_registry

    config_path = tmp_path / "config.yaml"
    config_path.write_text('basemaps:\n  enabled: true\n  providers:\n    test:\n'
        '      label: Sample\n      grid: geodetic\n'
        '      url_template: "https://example.invalid/{z}/{x}/{y}?key={token}"\n',
        encoding="utf-8")
    config = load_geo_config(config_path)
    assert config.basemaps.providers["test"].url_template.endswith("{token}")
    def names(cfg):
        return {tool.name for tool in build_geo_tool_registry(config=cfg).list_tools()}

    assert "geo_basemap" not in names(GeoConfig())
    assert "geo_basemap" in names(config)


def test_modified_cache_is_rejected(tmp_path, fake_tiles):
    from pathlib import Path

    result = fetch_basemap(configuration(), tmp_path, "test", [100, 20, 110, 30], zoom=3)
    path = Path(result["file_path"])
    with rasterio.open(path, "r+") as dataset:
        pixels = dataset.read()
        pixels[0, 0, 0] = 255
        dataset.write(pixels)
    with pytest.raises(ValueError, match="integrity"):
        fetch_basemap(configuration(), tmp_path, "test", [100, 20, 110, 30], zoom=3)


def test_qgis_reads_basemap_cache_without_exposing_configuration(tmp_path, fake_tiles):
    from pathlib import Path

    from geoharness.config.settings import GeoConfig
    from geoharness.qgis.worker_context import WorkerContext
    from geoharness.tools.geo_qgis import qgis_read_roots

    config = GeoConfig(basemaps=configuration())
    result = fetch_basemap(config.basemaps, tmp_path, "test", [100, 20, 110, 30], zoom=3)
    worker = WorkerContext(tmp_path, tmp_path / "qgis-run", qgis_read_roots(config, tmp_path))
    assert worker.read_path(result["file_path"]) == Path(result["file_path"])
    secret = tmp_path / ".geoharness/.env"
    secret.write_text("do not read", encoding="utf-8")
    with pytest.raises(PermissionError, match="Credential"):
        worker.read_path(str(secret))
