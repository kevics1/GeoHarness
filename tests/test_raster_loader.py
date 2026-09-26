"""Tests for RasterLoader — discovery, description, band reading/stats.

Raster support is what the fire-incident workspace actually needed: the data
folder holds ``西昌市高程.tif`` and ``西昌市可燃物分布.tif`` alongside the
shapefiles. These tests use real (tiny) GeoTIFFs written with rasterio so the
description/stats paths are exercised end to end, not mocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from geoharness.data.raster_loader import RasterLoader

rasterio = pytest.importorskip("rasterio")
np = pytest.importorskip("numpy")


def _write_raster(path: Path, data, *, nodata=None, crs="EPSG:32647") -> None:
    from rasterio.transform import from_origin

    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[0],
        width=data.shape[1],
        count=1,
        dtype=data.dtype,
        crs=crs,
        transform=from_origin(500000, 3000000, 30, 30),
        nodata=nodata,
    ) as ds:
        ds.write(data, 1)


class TestRasterDiscovery:
    def test_lists_only_rasters(self, tmp_path: Path) -> None:
        _write_raster(tmp_path / "dem.tif", np.ones((4, 4), dtype="int16"))
        # A vector file and a stray text file must be ignored.
        (tmp_path / "roads.shp").write_text("x", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

        loader = RasterLoader(file_dir=str(tmp_path))
        names = {s.name for s in loader.list_sources()}
        assert names == {"dem"}
        assert all(s.source_type == "raster" for s in loader.list_sources())

    def test_finds_nested_raster(self, tmp_path: Path) -> None:
        _write_raster(tmp_path / "数据" / "elevation.tif",
                      np.ones((3, 3), dtype="uint8"))
        loader = RasterLoader(file_dir=str(tmp_path))
        assert loader.resolve_path("elevation") is not None

    def test_prunes_noise_directories(self, tmp_path: Path) -> None:
        _write_raster(tmp_path / ".venv" / "junk.tif",
                      np.ones((2, 2), dtype="uint8"))
        _write_raster(tmp_path / "real.tif", np.ones((2, 2), dtype="uint8"))
        loader = RasterLoader(file_dir=str(tmp_path))
        names = {s.name for s in loader.list_sources()}
        assert names == {"real"}, "pruned dirs must not contribute rasters"


class TestRasterDescribe:
    def test_inspect_reports_geometry(self, tmp_path: Path) -> None:
        _write_raster(
            tmp_path / "dem.tif",
            np.arange(12, dtype="int16").reshape(3, 4),
            nodata=-9999.0,
        )
        loader = RasterLoader(file_dir=str(tmp_path))
        detail = loader.get_source_detail("dem")
        assert detail["width"] == 4
        assert detail["height"] == 3
        assert detail["band_count"] == 1
        assert detail["nodata"] == -9999.0
        assert detail["crs"].startswith("EPSG:32647")
        assert len(detail["bbox"]) == 4

    def test_missing_raster_returns_empty(self, tmp_path: Path) -> None:
        loader = RasterLoader(file_dir=str(tmp_path))
        assert loader.get_source_detail("nope") == {}


class TestRasterStats:
    def test_band_stats_match_data(self, tmp_path: Path) -> None:
        data = np.arange(100, dtype="float32").reshape(10, 10)
        _write_raster(tmp_path / "f.tif", data)
        loader = RasterLoader(file_dir=str(tmp_path))
        stats = loader.band_stats("f", band=1)
        assert stats["min"] == 0.0
        assert stats["max"] == 99.0
        assert stats["count"] == 100

    def test_nodata_is_excluded(self, tmp_path: Path) -> None:
        data = np.array([[1, 2], [3, -9999]], dtype="int16")
        _write_raster(tmp_path / "f.tif", data, nodata=-9999.0)
        loader = RasterLoader(file_dir=str(tmp_path))
        stats = loader.band_stats("f", band=1)
        assert stats["min"] == 1.0
        assert stats["max"] == 3.0
        assert stats["count"] == 3, "nodata pixel must not be counted"

    def test_out_of_range_band_raises(self, tmp_path: Path) -> None:
        _write_raster(tmp_path / "f.tif", np.ones((2, 2), dtype="uint8"))
        loader = RasterLoader(file_dir=str(tmp_path))
        with pytest.raises(ValueError, match="out of range"):
            loader.read_band("f", band=5)

    def test_missing_raster_raises(self, tmp_path: Path) -> None:
        loader = RasterLoader(file_dir=str(tmp_path))
        with pytest.raises(ValueError, match="not found"):
            loader.band_stats("nope")
