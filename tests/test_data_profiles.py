"""Discovery remembers measured metadata before spatial scripts are generated."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.data.file_loader import FileLoader
from geoharness.data.profiles import (
    MAX_AUTO_INSPECT,
    MAX_PROFILES,
    MAX_SCHEMA_FIELDS,
    current_profiles,
    file_signature,
    make_profile,
    record_profiles,
)
from geoharness.data.raster_loader import RasterLoader
from geoharness.tools.geo_raster_data import GeoRasterDataInput, GeoRasterDataTool
from geoharness.tools.geo_vector_data import GeoVectorDataInput, GeoVectorDataTool


def _context(tmp_path: Path, loader, *, kind: str) -> ToolExecutionContext:
    catalog = SimpleNamespace(
        get_connector=lambda name: loader if kind == "vector" else None,
        get_raster_connector=lambda: loader if kind == "raster" else None,
    )
    return ToolExecutionContext(cwd=tmp_path, metadata={"data_catalog": catalog})


def _raster(path: Path, *, crs="EPSG:32647", width=4) -> None:
    rasterio = pytest.importorskip("rasterio")
    np = pytest.importorskip("numpy")
    from rasterio.transform import from_origin

    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=3, count=1,
        dtype="int16", crs=crs, nodata=-9999,
        transform=from_origin(500000, 3000000, 30, 30),
    ) as dataset:
        dataset.write(np.ones((3, width), dtype="int16"), 1)


async def test_raster_list_inspects_headers_without_reading_pixels(tmp_path, monkeypatch):
    path = tmp_path / "dem.tif"
    _raster(path)
    loader = RasterLoader(str(tmp_path))
    monkeypatch.setattr(loader, "read_band", lambda *args, **kw: pytest.fail("pixel scan"))
    ctx = _context(tmp_path, loader, kind="raster")

    result = await GeoRasterDataTool().execute(GeoRasterDataInput(), ctx)

    assert not result.is_error
    assert "EPSG:32647" in result.output
    assert "overlay/mask" in result.output
    profile = next(iter(ctx.metadata["geo_data_context"]["profiles"].values()))
    assert Path(profile["path"]) == path.resolve()
    assert profile["crs"] == "EPSG:32647"
    assert profile["size"] == [4, 3]
    assert profile["band_count"] == 1
    assert profile["resolution"] == [30.0, 30.0]
    assert profile["nodata"] == -9999.0
    assert profile["freshness"] == "current"
    json.dumps(ctx.metadata["geo_data_context"], allow_nan=False)


async def test_vector_discovery_and_load_record_actual_schema_and_crs(tmp_path):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    path = tmp_path / "points.geojson"
    gpd.GeoDataFrame({"value": [2, 3]}, geometry=[Point(0, 0), Point(1, 1)],
                     crs="EPSG:4326").to_file(path, driver="GeoJSON")
    ctx = _context(tmp_path, FileLoader(str(tmp_path)), kind="vector")
    tool = GeoVectorDataTool()

    discovered = await tool.execute(GeoVectorDataInput(), ctx)
    assert not discovered.is_error
    assert "EPSG:4326" in discovered.output
    profile = next(iter(current_profiles(ctx.metadata).values()))
    assert profile["geometry_type"] == "Point"
    assert profile["feature_count"] == 2
    assert profile["bbox"] == [0.0, 0.0, 1.0, 1.0]

    loaded = await tool.execute(GeoVectorDataInput(action="load", file_path=str(path)), ctx)
    assert not loaded.is_error
    profile = next(iter(current_profiles(ctx.metadata).values()))
    assert "value" in profile["schema"]
    assert profile["feature_count"] == 2
    assert "gdf" not in json.dumps(profile)


async def test_rewritten_raster_invalidates_profile_and_header_cache(tmp_path):
    path = tmp_path / "dem.tif"
    _raster(path)
    loader = RasterLoader(str(tmp_path))
    ctx = _context(tmp_path, loader, kind="raster")
    tool = GeoRasterDataTool()
    await tool.execute(GeoRasterDataInput(), ctx)

    _raster(path, crs="EPSG:32648", width=7)
    assert current_profiles(ctx.metadata) == {}
    refreshed = await tool.execute(GeoRasterDataInput(action="inspect", name="dem"), ctx)
    assert not refreshed.is_error
    assert "EPSG:32648" in refreshed.output
    assert next(iter(current_profiles(ctx.metadata).values()))["width"] == 7


async def test_discovery_has_inspection_limit_and_survives_bad_file(tmp_path):
    paths = [tmp_path / f"item_{i:02}.tif" for i in range(MAX_AUTO_INSPECT + 2)]
    for path in paths:
        path.write_bytes(b"header")
    calls = []

    class Loader:
        def list_sources(self):
            return [SimpleNamespace(name=path.stem, metadata={"path": str(path)})
                    for path in paths]

        def get_source_detail(self, target):
            calls.append(target)
            if target == str(paths[0]):
                raise ValueError("bad header")
            return {"path": target, "crs": "EPSG:32647", "width": 4, "height": 3}

    ctx = _context(tmp_path, Loader(), kind="raster")
    result = await GeoRasterDataTool().execute(GeoRasterDataInput(), ctx)
    assert not result.is_error
    assert len(calls) == MAX_AUTO_INSPECT
    assert "not inspected" in result.output
    assert paths[-1].stem in result.output
    assert len(current_profiles(ctx.metadata)) == MAX_AUTO_INSPECT - 1


def test_profiles_are_bounded_json_and_explicit_about_unknowns(tmp_path):
    detail = {
        "path": str(tmp_path / "float.tif"), "name": "float", "crs": None,
        "nodata": float("nan"), "bbox": [float("nan")] * 4,
        "schema": {f"column_{i}": "str" for i in range(MAX_SCHEMA_FIELDS + 5)},
    }
    profile = make_profile(detail, kind="raster")
    assert profile["crs"] is None
    assert profile["nodata"] == "NaN"
    assert "bbox" not in profile
    assert "schema" not in profile
    assert profile["freshness"] == "unverified"
    json.dumps(profile, allow_nan=False)
    metadata = {}
    for i in range(MAX_PROFILES + 4):
        path = tmp_path / f"points_{i}.geojson"
        path.write_bytes(b"{}")
        measured = {**detail, "path": str(path), "_measured_file_stat": file_signature(path)}
        record_profiles(metadata, [make_profile(measured, kind="vector")])
    profiles = current_profiles(metadata)
    assert len(profiles) == MAX_PROFILES
    assert all(len(p["schema"]) == MAX_SCHEMA_FIELDS for p in profiles.values())
    assert all(p["schema_omitted"] == 5 for p in profiles.values())


async def test_stats_remembers_header_and_failed_inspect_does_not_add_profile(tmp_path):
    path = tmp_path / "dem.tif"
    _raster(path)
    ctx = _context(tmp_path, RasterLoader(str(tmp_path)), kind="raster")
    tool = GeoRasterDataTool()
    stats = await tool.execute(GeoRasterDataInput(action="stats", name="dem"), ctx)
    assert not stats.is_error
    assert next(iter(current_profiles(ctx.metadata).values()))["crs"] == "EPSG:32647"
    before = dict(current_profiles(ctx.metadata))
    failed = await tool.execute(GeoRasterDataInput(action="inspect", name="missing"), ctx)
    assert failed.is_error
    assert current_profiles(ctx.metadata) == before


def test_vector_detail_cache_changes_with_file_and_shapefile_crs_sidecar(tmp_path):
    gpd = pytest.importorskip("geopandas")
    from pyproj import CRS
    from shapely.geometry import Point

    path = tmp_path / "points.geojson"
    frame = gpd.GeoDataFrame({"value": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326")
    frame.to_file(path, driver="GeoJSON")
    loader = FileLoader(str(tmp_path))
    assert loader.get_source_detail(str(path))["row_count"] == 1
    changed = gpd.GeoDataFrame({"value": [2, 3]}, geometry=[Point(0, 0), Point(1, 1)],
                              crs="EPSG:32648")
    changed.to_file(path, driver="GeoJSON")
    detail = loader.get_source_detail(str(path))
    assert detail["row_count"] == 2
    assert detail["crs"] == "EPSG:32648"

    shape = tmp_path / "points.shp"
    frame.to_file(shape)
    old = loader.get_source_detail(str(shape))
    assert old["crs"] == "EPSG:4326"
    metadata = {}
    record_profiles(metadata, [make_profile(old, kind="vector")])
    shape.with_suffix(".prj").write_text(CRS.from_epsg(32647).to_wkt("WKT1_ESRI"))
    assert current_profiles(metadata) == {}
    assert loader.get_source_detail(str(shape))["crs"] == "EPSG:32647"


def test_profile_snapshot_is_pure_and_deleted_file_is_excluded(tmp_path):
    path = tmp_path / "dem.tif"
    path.write_bytes(b"header")
    metadata = {}
    record_profiles(metadata, [make_profile(
        {"path": str(path), "_measured_file_stat": file_signature(path)}, kind="raster"
    )])
    before = json.dumps(metadata, sort_keys=True)
    assert current_profiles(metadata)
    assert json.dumps(metadata, sort_keys=True) == before
    path.unlink()
    assert current_profiles(metadata) == {}
    assert json.dumps(metadata, sort_keys=True) == before


async def test_timed_out_discovery_cannot_commit_late_thread_profile(tmp_path, monkeypatch):
    import asyncio

    import geoharness.tools.geo_raster_data as raster_tool

    path = tmp_path / "slow.tif"
    path.write_bytes(b"header")
    release = threading.Event()
    finished = threading.Event()

    class Loader:
        def list_sources(self):
            return [SimpleNamespace(name="slow", metadata={"path": str(path)})]

        def get_source_detail(self, name):
            assert release.wait(timeout=2)
            finished.set()
            return {"path": str(path), "crs": "EPSG:32647"}

    monkeypatch.setattr(raster_tool, "_RASTER_TIMEOUT_SECONDS", 0.02)
    ctx = _context(tmp_path, Loader(), kind="raster")
    result = await GeoRasterDataTool().execute(GeoRasterDataInput(), ctx)
    assert result.is_error
    assert "timed out" in result.output
    release.set()
    assert await asyncio.to_thread(finished.wait, 1)
    assert "geo_data_context" not in ctx.metadata


@pytest.mark.parametrize("kind", ["vector", "raster"])
@pytest.mark.parametrize("change", ["overwrite", "delete"])
def test_measured_detail_cannot_bind_old_metadata_to_changed_file(tmp_path, kind, change):
    if kind == "raster":
        path = tmp_path / "dem.tif"
        _raster(path)
        loader = RasterLoader(str(tmp_path))
    else:
        gpd = pytest.importorskip("geopandas")
        from shapely.geometry import Point

        path = tmp_path / "points.geojson"
        gpd.GeoDataFrame(geometry=[Point(0, 0)], crs="EPSG:4326").to_file(path)
        loader = FileLoader(str(tmp_path))
    first = loader.get_source_detail(str(path))
    assert first["_measured_file_stat"] == file_signature(path)
    cached = loader.get_source_detail(str(path))
    assert cached["_measured_file_stat"] == first["_measured_file_stat"]
    if change == "delete":
        path.unlink()
    else:
        path.write_bytes(b"different file after metadata measurement")
    assert make_profile(first, kind=kind) is None
    assert make_profile(cached, kind=kind) is None


@pytest.mark.parametrize("change", ["overwrite", "delete"])
async def test_discovery_rejects_change_after_loader_returns_metadata(
    tmp_path, monkeypatch, change
):
    path = tmp_path / "dem.tif"
    _raster(path)
    loader = RasterLoader(str(tmp_path))
    original = loader.get_source_detail

    def changed_detail(name):
        detail = original(name)
        if change == "delete":
            path.unlink()
        else:
            _raster(path, crs="EPSG:32648", width=7)
        return detail

    monkeypatch.setattr(loader, "get_source_detail", changed_detail)
    ctx = _context(tmp_path, loader, kind="raster")
    result = await GeoRasterDataTool().execute(GeoRasterDataInput(), ctx)
    assert not result.is_error
    assert result.metadata["data_profiles"] == []
    assert "EPSG:32647" not in result.output
    assert ctx.metadata["geo_data_context"]["profiles"] == {}


@pytest.mark.parametrize("change", ["overwrite", "delete"])
def test_commit_rejects_file_change_after_profile_construction(tmp_path, change):
    path = tmp_path / "dem.tif"
    path.write_bytes(b"original")
    profile = make_profile(
        {"path": str(path), "crs": "EPSG:32647", "_measured_file_stat": file_signature(path)},
        kind="raster",
    )
    assert profile["freshness"] == "current"
    if change == "delete":
        path.unlink()
    else:
        path.write_bytes(b"replaced file with a different CRS")
    metadata = {}
    record_profiles(metadata, [profile])
    assert metadata["geo_data_context"]["profiles"] == {}


async def test_stats_cannot_mix_measured_pixels_with_replacement_header(tmp_path, monkeypatch):
    path = tmp_path / "dem.tif"
    _raster(path)
    loader = RasterLoader(str(tmp_path))
    original = loader.band_stats

    def changed_stats(name, band=1):
        stats = original(name, band=band)
        _raster(path, crs="EPSG:32648", width=7)
        return stats

    monkeypatch.setattr(loader, "band_stats", changed_stats)
    ctx = _context(tmp_path, loader, kind="raster")
    result = await GeoRasterDataTool().execute(GeoRasterDataInput(action="stats", name="dem"), ctx)
    assert not result.is_error
    assert result.metadata["data_profiles"] == []
    assert ctx.metadata["geo_data_context"]["profiles"] == {}


async def test_vector_adapter_resolves_measured_path_before_loading(tmp_path):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    path = tmp_path / "points.geojson"
    gpd.GeoDataFrame(geometry=[Point(0, 0)], crs="EPSG:4326").to_file(path)
    loader = FileLoader(str(tmp_path))

    class Adapter:
        # An adapter without optional resolve_path must not discover its path
        # only after load, when it might describe a replacement data version.
        def get_source_detail(self, name):
            return loader.get_source_detail(name)

        def load_geodataframe(self, name):
            frame = loader.load_geodataframe(name)
            path.write_bytes(b"replacement after frame load")
            return frame

    ctx = _context(tmp_path, Adapter(), kind="vector")
    result = await GeoVectorDataTool().execute(
        GeoVectorDataInput(action="load", name="points"), ctx
    )
    assert not result.is_error
    assert result.metadata["data_profiles"] == []
    assert ctx.metadata["geo_data_context"]["profiles"] == {}


def test_unsigned_metadata_cannot_claim_current_even_when_path_exists(tmp_path):
    path = tmp_path / "dem.tif"
    path.write_bytes(b"header")
    profile = make_profile({"path": str(path), "crs": "EPSG:32647"}, kind="raster")
    assert profile["freshness"] == "unverified"
    assert profile["file_stat"] is None
