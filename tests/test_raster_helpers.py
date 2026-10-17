"""Read back real tiny rasters; exercise helpers inside the guarded child as well."""

import hashlib

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from openharness.tools.base import ToolExecutionContext
from rasterio.transform import from_origin
from shapely.geometry import box

from geoharness._proj_fix import ensure_proj_data
from geoharness.config.settings import GeoConfig
from geoharness.execution.raster import RasterData, raster_backdrop, read_raster, write_raster
from geoharness.execution.stages import classify_failure
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool


@pytest.fixture
def source(tmp_path):
    ensure_proj_data()
    path = tmp_path / "source.tif"
    pixels = np.arange(16, dtype="int16").reshape(4, 4)
    pixels[0, 0] = -9999
    with rasterio.open(path, "w", driver="GTiff", height=4, width=4, count=1, dtype="int16",
                       crs="EPSG:3857", transform=from_origin(100, 240, 10, 10),
                       nodata=-9999) as output:
        output.write(pixels, 1)
    return path


def test_read_resample_preserves_band_axis_bounds_and_nodata(source):
    data = read_raster(source, bands=1, out_shape=(2, 2))
    assert data.array.shape == (1, 2, 2)
    assert data.transform == from_origin(100, 240, 20, 20)
    assert data.crs == rasterio.crs.CRS.from_epsg(3857) and data.nodata == -9999
    with rasterio.open(source) as original:
        expected = original.read([1], out_shape=(1, 2, 2), masked=True)
    np.testing.assert_array_equal(data.array, expected)


def test_clip_resample_transforms_geometry_and_keeps_exact_georeferencing(source, tmp_path):
    # Stay inside cell boundaries so CRS round-trip floating noise cannot add a border cell.
    shapes = gpd.GeoSeries([box(111, 211, 129, 229)], crs=3857).to_crs(4326)
    data = read_raster(source, geometries=shapes, out_shape=(4, 4))
    target = write_raster(tmp_path / "clipped.tif", data)
    with rasterio.open(target) as result:
        assert result.count == 1 and result.shape == (4, 4)
        assert result.crs.to_epsg() == 3857
        assert tuple(result.bounds) == pytest.approx((110, 210, 130, 230), abs=1e-6)
        np.testing.assert_array_equal(result.read(masked=True), data.array)


@pytest.mark.parametrize("rgb", [False, True])
def test_backdrop_retains_mask_and_source_and_has_requested_bands(source, tmp_path, rgb):
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    target = raster_backdrop(source, tmp_path / "backdrop.tif", rgb=rgb, percentiles=(0, 100))
    with rasterio.open(target) as result:
        assert result.count == (3 if rgb else 1)
        assert result.shape == (4, 4) and result.nodata == 0
        array = result.read(masked=True)
        assert array.mask[0, 0, 0]
        assert array[0, 0, 1] == 1 and array[0, 3, 3] == 255
        if rgb:
            np.testing.assert_array_equal(array[0], array[2])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_shape_and_nodata_fail_before_creating_output(source, tmp_path):
    data = read_raster(source)
    target = tmp_path / "bad.tif"
    with pytest.raises(ValueError, match="actual shape=.*ndim=1"):
        write_raster(target, data.array[0][0], crs=data.crs, transform=data.transform)
    assert not target.exists()
    with pytest.raises(ValueError, match="collides"):
        write_raster(target, np.array([[[0, 1]]], dtype="uint8"), crs=data.crs,
                     transform=data.transform, nodata=0)
    with pytest.raises(ValueError, match="explicit nodata"):
        write_raster(target, RasterData(data.array, data.crs, data.transform))
    with pytest.raises(ValueError, match="cannot relabel"):
        write_raster(target, data, crs=4326)
    with pytest.raises(ValueError, match="ndim=2"):
        write_raster(target, data.array[0], crs=data.crs, transform=data.transform)


async def test_helpers_are_injected_and_respect_source_write_guards(source, tmp_path):
    code = f"""
import numpy as np
data = read_raster({str(source)!r}, bands=1, out_shape=(2,2))
assert data.array.shape == (1,2,2)
write_raster('resampled.tif', data)
raster_backdrop({str(source)!r}, 'rgb.tif', rgb=True)
try: write_raster({str(source)!r}, data)
except PermissionError: pass
else: raise AssertionError('source write unexpectedly allowed')
print('read/write/resample/backdrop and guards passed')
"""
    result = await GeoPythonTool().execute(GeoPythonInput(code=code,
        expected_outputs=["resampled.tif", "rgb.tif"]), ToolExecutionContext(
            cwd=tmp_path, metadata={"geoharness_config": GeoConfig()}))
    assert not result.is_error, result.output
    files = {item["path"].split("/")[-1].split("\\")[-1]: item
             for item in result.metadata["files"]}
    assert files["rgb.tif"]["shape"] == [3, 4, 4]
    assert files["resampled.tif"]["validation_state"] == "passed"


@pytest.mark.parametrize(("message", "category"), [
    ("TypeError: mask() got an unexpected keyword argument 'fill_value'", "api_contract"),
    ("ValueError: RasterShapeError actual shape=(4,)", "array_shape"),
    ("ValueError: Source shape (3,734) is inconsistent with given indexes 3", "array_shape"),
    ("PermissionError: Read path is outside allowed data/runtime directories", "permission"),
])
def test_deterministic_error_categories(message, category):
    failure = classify_failure(message)
    assert failure["category"] == category and failure["retry_without_change"] is False
