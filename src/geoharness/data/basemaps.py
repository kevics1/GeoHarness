"""Configured tile services materialized as bounded, local RGB GeoTIFFs."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import shutil
import tempfile
from pathlib import Path
from string import Formatter
from threading import Event
from typing import Literal
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, model_validator

from geoharness.data.profiles import file_signature, make_profile


class BasemapSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, max_length=256)
    url_template: str = Field(min_length=1, max_length=4096, repr=False)
    token_env: str = Field(default="GEOH_TIANDITU_TOKEN", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    grid: Literal["geodetic", "web_mercator"]
    min_zoom: int = Field(default=1, ge=0, le=22)
    max_zoom: int = Field(default=18, ge=0, le=22)
    tile_size: int = Field(default=256, ge=16, le=1024)
    attribution: str = Field(default="", max_length=1024)

    @model_validator(mode="after")
    def check_template(self):
        try:
            pieces = list(Formatter().parse(self.url_template))
            fields = {field for _, field, _, _ in pieces if field is not None}
            parsed = urlsplit(self.url_template)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and not parsed.username and not parsed.password and not parsed.fragment
                     and fields == {"x", "y", "z", "token"}
                     and all(not spec and not conversion for _, _, spec, conversion in pieces))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Tile template requires http(s), no URL credentials, and x/y/z/token.")
        if self.min_zoom > self.max_zoom:
            raise ValueError("min_zoom must not exceed max_zoom.")
        if self.grid == "geodetic" and self.min_zoom < 1:
            raise ValueError("Geodetic c grid starts at zoom 1 (two columns, one row).")
        return self


class BasemapConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    cache_dir: str = ".geoharness/basemaps"
    providers: dict[str, BasemapSource] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=20, gt=0, le=120)
    max_tiles: int = Field(default=64, ge=1, le=256)
    max_pixels: int = Field(default=16777216, ge=256, le=67108864)
    user_agent: str = Field(default="GeoHarness", min_length=1, max_length=256,
                            pattern=r"^[^\r\n]+$")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _download_tile(url: str, *, timeout: float, user_agent: str) -> bytes:
    """No redirects, and never expose service URLs through exception text."""
    try:
        request = Request(url, headers={"User-Agent": user_agent})
        with build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            content = response.read(4 * 1024 * 1024 + 1)
        if len(content) > 4 * 1024 * 1024:
            raise ValueError("Tile response exceeds 4 MiB.")
        return content
    except HTTPError as error:
        raise ValueError(f"Tile service HTTP status {error.code}.") from None
    except Exception as error:
        raise ValueError(f"Tile request failed ({type(error).__name__}).") from None


def _cache_root(workspace: Path, configured: str) -> Path:
    workspace = workspace.resolve()
    raw = Path(configured).expanduser()
    path = raw if raw.is_absolute() else workspace / raw
    if not path.is_relative_to(workspace) or path == workspace or ".." in path.parts:
        raise ValueError("Basemap cache_dir must be a workspace subdirectory.")
    for parent in (path, *path.parents):
        if parent == workspace:
            break
        if _is_link(parent):
            raise ValueError("Basemap cache must not contain symbolic links.")
    resolved = path.resolve()
    if not resolved.is_relative_to(workspace) or resolved == workspace:
        raise ValueError("Basemap cache escaped the workspace.")
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _grid_bounds(bounds: list[float], crs: str, grid: str) -> tuple[list[float], str]:
    from pyproj import CRS, Transformer

    if len(bounds) != 4 or not all(math.isfinite(v) for v in bounds):
        raise ValueError("bounds must contain four finite coordinates.")
    if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
        raise ValueError("bounds must be ordered and must not cross the date line.")
    try:
        source = CRS.from_user_input(crs)
        target = CRS.from_epsg(4490 if grid == "geodetic" else 3857)
        geographic = Transformer.from_crs(source, 4326, always_xy=True).transform_bounds(
            *bounds, densify_pts=21, errcheck=True)
        if (not all(math.isfinite(v) for v in geographic)
                or geographic[0] < -180 or geographic[2] > 180
                or geographic[0] >= geographic[2]
                or geographic[1] < -90 or geographic[3] > 90):
            raise ValueError
        if grid == "web_mercator" and (
                geographic[1] < -85.0511287798066 or geographic[3] > 85.0511287798066):
            raise ValueError
        transformed = Transformer.from_crs(source, target, always_xy=True).transform_bounds(
            *bounds, densify_pts=21, errcheck=True)
    except Exception:
        raise ValueError(
            "Invalid CRS/bounds, unsupported latitude, or date-line crossing.") from None
    return list(transformed), target.to_string()


def _tile_window(bounds: list[float], zoom: int, grid: str):
    count_x = 2**zoom
    if grid == "geodetic":
        # Tianditu's c grid has 2 x 1 tiles at level 1, not XYZ/Web Mercator.
        count_y = max(1, count_x // 2)
        left, top, span = -180.0, 90.0, 360.0 / count_x
    else:
        count_y = count_x
        half = math.pi * 6378137.0
        left, top, span = -half, half, 2 * half / count_x
    xmin, ymin, xmax, ymax = bounds
    x0 = max(0, int(math.floor((xmin - left) / span)))
    x1 = min(count_x - 1, int(math.ceil((xmax - left) / span)) - 1)
    y0 = max(0, int(math.floor((top - ymax) / span)))
    y1 = min(count_y - 1, int(math.ceil((top - ymin) / span)) - 1)
    return x0, y0, x1, y1, left, top, span


def fetch_basemap(config: BasemapConfig, workspace: Path, provider: str,
                  bounds: list[float], crs: str = "EPSG:4326", zoom: int | None = None,
                  *, cancelled: Event | None = None) -> dict:
    """Fetch configured tiles only; publish immutable local artifacts with no secrets."""
    def check_cancelled():
        if cancelled is not None and cancelled.is_set():
            raise InterruptedError("Basemap download cancelled.")

    check_cancelled()
    if not config.enabled:
        raise ValueError("Basemap downloads are disabled in configuration.")
    source = config.providers.get(provider)
    if source is None:
        raise ValueError("Unknown configured basemap provider.")
    token = os.environ.get(source.token_env, "").strip()
    if not token:
        raise ValueError(f"Basemap credential environment variable {source.token_env} is unset.")
    transformed, target_crs = _grid_bounds(bounds, crs, source.grid)
    if zoom is not None and not source.min_zoom <= zoom <= source.max_zoom:
        raise ValueError("zoom is outside this provider's configured range.")
    choices = [zoom] if zoom is not None else range(source.max_zoom, source.min_zoom - 1, -1)
    selected = None
    for level in choices:
        window = _tile_window(transformed, level, source.grid)
        x0, y0, x1, y1, *_ = window
        count = (x1 - x0 + 1) * (y1 - y0 + 1)
        pixels = count * source.tile_size**2
        if count > 0 and count <= config.max_tiles and pixels <= config.max_pixels:
            selected = level, window, count
            break
    if selected is None:
        raise ValueError(
            "Requested coverage exceeds max_tiles/max_pixels; reduce zoom or split maps.")
    level, window, count = selected
    x0, y0, x1, y1, left, top, span = window
    root = _cache_root(workspace, config.cache_dir)
    expected = {"crs": target_crs, "width": (x1 - x0 + 1) * source.tile_size,
                "height": (y1 - y0 + 1) * source.tile_size,
                "bounds": [left + x0 * span, top - (y1 + 1) * span,
                           left + (x1 + 1) * span, top - y0 * span]}
    identity = hashlib.sha256(json.dumps({
        "source": hashlib.sha256(source.url_template.encode()).hexdigest(),
        "credential": hashlib.sha256(token.encode()).hexdigest(),
        "grid": source.grid, "zoom": level, "window": [x0, y0, x1, y1],
        "tile_size": source.tile_size,
    }, sort_keys=True).encode()).hexdigest()
    destination = root / identity
    if _is_link(destination):
        raise ValueError("Basemap cache entry must not be a symbolic link.")
    if destination.exists():
        return _read_result(destination, source, level, count, expected, cached=True)
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
    try:
        import numpy as np
        import rasterio
        from PIL import Image
        from rasterio.transform import from_origin

        size = source.tile_size
        width, height = (x1 - x0 + 1) * size, (y1 - y0 + 1) * size
        raster_path = stage / "basemap.tif"
        with rasterio.open(raster_path, "w", driver="GTiff", width=width, height=height,
                           count=3, dtype="uint8", crs=target_crs, photometric="RGB",
                           compress="deflate", transform=from_origin(
                               left + x0 * span, top - y0 * span, span / size, span / size)) as out:
            for y in range(y0, y1 + 1):
                for x in range(x0, x1 + 1):
                    check_cancelled()
                    url = source.url_template.format(x=x, y=y, z=level,
                                                     token=quote(token, safe=""))
                    payload = _download_tile(url, timeout=config.timeout_seconds,
                                             user_agent=config.user_agent)
                    check_cancelled()
                    try:
                        with Image.open(io.BytesIO(payload)) as image:
                            if image.format not in {"PNG", "JPEG"} or image.size != (size, size):
                                raise ValueError
                            pixels = np.array(image.convert("RGB"))
                    except Exception:
                        raise ValueError(
                            "Tile response is not a valid expected-size PNG/JPEG.") from None
                    out.write(pixels.transpose(2, 0, 1), window=rasterio.windows.Window(
                        (x - x0) * size, (y - y0) * size, size, size))
            out.update_tags(attribution=source.attribution)
        (stage / "cache.json").write_text(json.dumps({
            "sha256": _digest(raster_path), "geometry": expected,
        }), encoding="utf-8")
        # Imported here to avoid the settings -> basemaps -> publication settings cycle.
        from geoharness.files.publication import DirectoryPublicationError, publish_directory
        check_cancelled()
        try:
            publish_directory(stage, destination, before_attempt=check_cancelled)
        except DirectoryPublicationError:
            if not destination.is_dir() or _is_link(destination):
                raise
            # A concurrent publisher may have finished the same immutable tile set.
        return _read_result(destination, source, level, count, expected, cached=False)
    finally:
        if stage.exists() and stage.resolve().is_relative_to(root) and stage.resolve() != root:
            shutil.rmtree(stage)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_result(directory: Path, source: BasemapSource, zoom: int, count: int, expected: dict,
                 *, cached: bool) -> dict:
    import rasterio

    path = directory / "basemap.tif"
    if _is_link(path) or not path.is_file():
        raise ValueError("Basemap cache is incomplete or unsafe; choose a new cache directory.")
    signature = file_signature(path)
    receipt = directory / "cache.json"
    try:
        if _is_link(receipt) or receipt.stat().st_size > 16384:
            raise ValueError
        recorded = json.loads(receipt.read_text(encoding="utf-8"))
        if recorded.get("geometry") != expected or recorded.get("sha256") != _digest(path):
            raise ValueError
    except (OSError, ValueError, AttributeError):
        raise ValueError("Basemap cache integrity failed; choose a new cache directory.") from None
    with rasterio.open(path) as raster:
        if (raster.driver != "GTiff" or raster.count != 3
                or any(dtype != "uint8" for dtype in raster.dtypes)
                or str(raster.crs) != expected["crs"]
                or raster.width != expected["width"] or raster.height != expected["height"]
                or not all(math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-8)
                           for a, b in zip(raster.bounds, expected["bounds"]))):
            raise ValueError("Basemap cache geometry/format does not match the requested tiles.")
        detail = {"name": source.label, "path": str(path), "crs": str(raster.crs),
                  "bbox": list(raster.bounds), "width": raster.width, "height": raster.height,
                  "band_count": raster.count, "dtype": raster.dtypes[0],
                  "resolution": list(raster.res), "_measured_file_stat": signature}
    profile = make_profile(detail, kind="raster")
    if profile is None:
        raise ValueError("Basemap changed during inspection; retry inspection.")
    return {"status": "succeeded", "file_path": str(path), "crs": detail["crs"],
            "bounds": detail["bbox"], "resolution": detail["resolution"],
            "zoom": zoom, "tile_count": count, "attribution": source.attribution,
            "cached": cached, "layer_spec": {"file_path": str(path), "kind": "raster",
                "name": source.label, "raster_style": {"renderer": "default"}},
            "data_profiles": [profile]}
