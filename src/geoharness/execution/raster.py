"""Small Rasterio operations with an explicit band-first, georeferenced data contract.

No reimplementation of raster algorithms: Rasterio reads/resamples/masks/writes.
These helpers retain CRS/transform/nodata and reject accidental axis loss before I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from affine import Affine
from rasterio.enums import Resampling

_UNSET = object()


@dataclass(frozen=True)
class RasterData:
    array: Any
    crs: Any
    transform: Affine
    nodata: Any = None


def _shape(value) -> tuple[int, int]:
    if (not isinstance(value, (list, tuple)) or len(value) != 2
            or any(type(n) is not int or n <= 0 for n in value)):
        raise ValueError(f"RasterShapeError: expected out_shape=(rows, columns)>0; actual={value}")
    return tuple(value)


def read_raster(path, *, bands=None, out_shape=None, resampling="nearest",
                geometries=None, all_touched=False) -> RasterData:
    """Read/clip/resample to a masked (bands, rows, columns) array with metadata.

    A single band is still a list selection: this interface never squeezes axes.
    Resampling changes resolution, not CRS. Use verified GeoSeries/DataFrame for clips.
    """
    from rasterio.features import geometry_mask
    from rasterio.mask import raster_geometry_mask

    try:
        method = Resampling[resampling]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Unknown resampling method {resampling!r}; use Resampling names") from exc
    with rasterio.open(path) as source:
        if source.crs is None:
            raise ValueError("RasterCRSError: source has no verified CRS")
        indices = list(source.indexes) if bands is None else (
            [bands] if type(bands) is int else list(bands))
        if not indices or any(type(i) is not int or i not in source.indexes for i in indices):
            raise ValueError(f"RasterBandError: actual bands={indices}; available={source.indexes}")
        window, transform, shapes = None, source.transform, None
        base_shape = (source.height, source.width)
        if geometries is not None:
            import geopandas as gpd

            if not isinstance(geometries, (gpd.GeoSeries, gpd.GeoDataFrame)) \
                    or geometries.crs is None:
                raise ValueError("RasterCRSError: clipping requires CRS-bearing "
                                 "GeoSeries/DataFrame")
            shapes = geometries.geometry.to_crs(source.crs)
            if (shapes.empty or shapes.isna().any() or shapes.is_empty.any()
                    or not shapes.is_valid.all() or not np.isfinite(shapes.total_bounds).all()):
                raise ValueError("RasterGeometryError: clip geometries must be "
                                 "valid/finite/nonempty")
            mask, transform, window = raster_geometry_mask(
                source, shapes, crop=True, all_touched=all_touched)
            base_shape = mask.shape
        height, width = _shape(out_shape) if out_shape is not None else base_shape
        transform = transform * Affine.scale(base_shape[1] / width, base_shape[0] / height)
        array = source.read(indexes=indices, window=window, masked=True,
                            out_shape=(len(indices), height, width), resampling=method)
        if shapes is not None:
            excluded = geometry_mask(shapes, (height, width), transform, all_touched=all_touched)
            array.mask = np.ma.getmaskarray(array) | excluded[np.newaxis, :, :]
        return RasterData(array=array, crs=source.crs, transform=transform, nodata=source.nodata)


def write_raster(path, data, *, crs=None, transform=None, nodata=_UNSET) -> Path:
    """Write a GeoTIFF; raw arrays require explicit CRS and Affine transform.

    Always require three dimensions, including a length-one band axis for single bands.
    Masked pixels require a representable nodata value, never a guessed fill value.
    """
    if isinstance(data, RasterData):
        if crs is not None and rasterio.crs.CRS.from_user_input(crs) != data.crs:
            raise ValueError("RasterCRSError: cannot relabel RasterData; reproject explicitly")
        if transform is not None and transform != data.transform:
            raise ValueError("RasterTransformError: cannot override RasterData georeferencing")
        crs, transform = data.crs, data.transform
        nodata = data.nodata if nodata is _UNSET else nodata
        array = np.ma.asarray(data.array)
    else:
        array = np.ma.asarray(data)
        nodata = None if nodata is _UNSET else nodata
    if array.ndim != 3 or any(size <= 0 for size in array.shape):
        raise ValueError("RasterShapeError: expected (bands, rows, columns); "
                         f"actual shape={array.shape}, ndim={array.ndim}")
    if crs is None:
        raise ValueError("RasterCRSError: write requires verified CRS")
    crs = rasterio.crs.CRS.from_user_input(crs)
    if not isinstance(transform, Affine) or not np.isfinite(tuple(transform)).all() \
            or abs(transform.determinant) == 0:
        raise ValueError("RasterTransformError: expected finite nondegenerate Affine transform")
    if array.dtype.kind not in "uif" or not rasterio.dtypes.check_dtype(array.dtype.name):
        raise ValueError(f"RasterDtypeError: unsupported actual dtype={array.dtype}")
    if not np.isfinite(array.compressed()).all():
        raise ValueError("RasterValueError: valid pixels contain NaN/infinity; "
                         "mask them explicitly")
    masked = np.ma.getmaskarray(array).any()
    if masked and nodata is None:
        raise ValueError("RasterNodataError: masked pixels require explicit nodata")
    if nodata is not None:
        if array.dtype.kind in "ui":
            limits = np.iinfo(array.dtype)
            if not np.isfinite(nodata) or not limits.min <= nodata <= limits.max \
                    or nodata != int(nodata):
                raise ValueError(f"RasterNodataError: {nodata} not representable in {array.dtype}")
        elif np.isinf(nodata) or (np.isfinite(nodata)
                                 and abs(nodata) > np.finfo(array.dtype).max):
            raise ValueError(f"RasterNodataError: {nodata} not representable in {array.dtype}")
        if (np.ma.filled(array == nodata, False)).any():
            raise ValueError("RasterNodataError: nodata collides with valid pixel values")
    path = Path(path)
    with rasterio.open(path, "w", driver="GTiff", count=array.shape[0], height=array.shape[1],
                       width=array.shape[2], dtype=array.dtype.name, crs=crs,
                       transform=transform, nodata=nodata) as destination:
        destination.write(array.filled(nodata) if masked else array.data)
    return path


def raster_backdrop(source, output, *, band=1, out_shape=None, geometries=None,
                    percentiles=(2, 98), rgb=False, resampling="nearest") -> Path:
    """Explicit display stretch; keep source unchanged and reserve uint8 zero for nodata.

    This creates a presentation background, not an analysis/reclassification product.
    """
    if len(percentiles) != 2 or not 0 <= percentiles[0] < percentiles[1] <= 100:
        raise ValueError("percentiles requires 0 <= low < high <= 100")
    data = read_raster(source, bands=band, out_shape=out_shape, geometries=geometries,
                       resampling=resampling)
    values = data.array[0]
    valid = values.compressed()
    if not valid.size or not np.isfinite(valid).all():
        raise ValueError("RasterValueError: backdrop needs finite nonempty valid pixels")
    low, high = np.percentile(valid, percentiles)
    base = np.ma.getdata(values).astype("float64")
    base = np.where(np.ma.getmaskarray(values), low, base)
    gray = (np.full(base.shape, 128, dtype="uint8") if high == low else
            np.rint(np.clip((base - low) / (high - low), 0, 1) * 254 + 1).astype("uint8"))
    gray = np.ma.array(gray, mask=np.ma.getmaskarray(values))
    array = np.ma.stack([gray] * (3 if rgb else 1))
    return write_raster(output, RasterData(array, data.crs, data.transform, 0))
