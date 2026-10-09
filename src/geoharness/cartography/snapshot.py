"""Independently materialize guarded QGIS create inputs before validating them."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geoharness.cartography.values import TEMPORAL_FIELDS, normalize_frame


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class FrozenQgisInputs:
    directory: Path
    layers: list[dict[str, str]]
    original_sources: list[dict[str, Any]]
    source_digests: dict[Path, str]

    def remember_source(self, path: Path) -> None:
        digest = _digest(path)
        previous = self.source_digests.get(path)
        if previous is not None and previous != digest:
            raise ValueError(f"Map source changed while freezing/preflighting: {path}")
        self.source_digests[path] = digest

    def verify_sources(self) -> None:
        """Facts read original locators; they must still describe the frozen content."""
        for path, expected in self.source_digests.items():
            if _digest(path) != expected:
                raise ValueError(f"Map source changed while freezing/preflighting: {path}")


def freeze_qgis_inputs(
    layers: list[dict[str, Any]], *, directory: Path, cwd: Path, read_roots: list[Path],
) -> FrozenQgisInputs:
    """Write self-contained GPKG/GTiff inputs into a caller-owned temporary directory.

    Original inputs are checked against the original read roots before admitting
    the temporary directory to the worker. No run directory or budget is used.
    The caller keeps these files alive until native readers and threads stop.
    """
    import geopandas as gpd
    import rasterio
    from rasterio.shutil import copy as copy_raster

    from geoharness.qgis.project_worker import _RASTER_SUFFIXES, _VECTOR_SUFFIXES, _local_source
    from geoharness.qgis.worker_context import WorkerContext

    guard = WorkerContext(cwd, cwd, read_roots)
    snapshot = FrozenQgisInputs(directory.resolve(), [], [], {})
    for index, spec in enumerate(layers):
        path, options = _local_source(spec["file_path"], guard)
        snapshot.remember_source(path)
        if path.suffix.lower() == ".shp":
            # Attributes, CRS and encoding are read from companion files, not .shp.
            sidecars = {".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx",
                        ".ain", ".aih", ".atx", ".ixs", ".mxs"}
            for companion in path.parent.glob(f"{path.stem}.*"):
                if companion.suffix.lower() in sidecars or (
                    companion.name.casefold() == path.name.casefold() + ".xml"
                ):
                    snapshot.remember_source(guard.read_path(str(companion)))
        name = spec.get("name") or path.stem
        snapshot.original_sources.append({"file_path": str(path), "label": name})
        if spec.get("kind", "vector") == "raster":
            if options or path.suffix.lower() not in _RASTER_SUFFIXES:
                raise ValueError("Raster sources require one local raster file")
            target = snapshot.directory / f"layer_{index}.tif"
            with rasterio.open(path) as source:
                # Include local VRT dependencies and mask/auxiliary files. The original
                # XML references were guarded before open; this checks GDAL's resolved files.
                for dependency in source.files:
                    checked = guard.read_path(dependency)
                    snapshot.remember_source(checked)
                with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
                    copy_raster(source, target, driver="GTiff")
            snapshot.layers.append({"file_path": str(target), "name": name})
        else:
            if path.suffix.lower() not in _VECTOR_SUFFIXES:
                raise ValueError("Vector sources require GPKG, SHP, GeoJSON or FlatGeobuf")
            selected: str | int = spec.get("layer_name", "")
            if not selected and options:
                selector, value = options.split("=", 1)
                selected = int(value) if selector == "layerid" else value
            frame = gpd.read_file(path, **({"layer": selected} if selected != "" else {}))
            frame = normalize_frame(frame)
            snapshot.original_sources[-1]["temporal_fields"] = frame.attrs[TEMPORAL_FIELDS]
            target = snapshot.directory / f"layer_{index}.gpkg"
            layer_name = f"layer_{index}"
            # GPKG's default fid/geom names must not consume existing business fields.
            columns = {str(column).casefold() for column in frame.columns}
            fid, geometry = "_geoharness_fid", "_geoharness_geometry"
            while fid.casefold() in columns:
                fid += "_"
            while geometry.casefold() in columns or geometry.casefold() == fid.casefold():
                geometry += "_"
            frame.to_file(target, driver="GPKG", layer=layer_name, index=False,
                          FID=fid, GEOMETRY_NAME=geometry)
            snapshot.layers.append({"file_path": str(target), "layer_name": layer_name,
                                    "name": name})
    snapshot.verify_sources()
    return snapshot
