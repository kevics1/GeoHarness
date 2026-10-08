"""Metric vector operations and reproducible, read-only source handling."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from threading import Event
from typing import Any

from geoharness.cartography.sources import resolve_geodataframe
from geoharness.config.settings import PublicationConfig
from geoharness.files.publication import DirectoryPublicationError, publish_directory

_COUNT_SEMANTICS = (
    "A source feature counts once in each zone it intersects, including boundaries. "
    "Features crossing or touching overlapping/adjacent zones count in every matching zone; "
    "zone totals are not a deduplicated global total. affected_count uses the same predicate "
    "after selecting affected source features."
)


def _check_deadline(deadline: float, cancelled: Event) -> None:
    if cancelled.is_set() or time.monotonic() >= deadline:
        raise TimeoutError("Spatial analysis exceeded its configured timeout.")


def _validate_frame(frame: Any, label: str) -> None:
    import numpy as np

    if frame.crs is None:
        raise ValueError(f"{label}: unknown CRS; provide source.crs explicitly.")
    if frame.geometry.isna().any() or frame.geometry.is_empty.any():
        raise ValueError(f"{label}: null or empty geometries are not supported.")
    if not frame.geometry.is_valid.all():
        raise ValueError(f"{label}: invalid geometry; repair it explicitly before analysis.")
    if not frame.empty:
        from shapely import get_coordinates

        if not np.isfinite(get_coordinates(frame.geometry.array, include_z=False)).all():
            raise ValueError(f"{label}: geometry coordinates must be finite.")
        if not np.isfinite(frame.total_bounds).all():
            raise ValueError(f"{label}: geometry bounds must be finite.")
        geographic = frame.to_crs("EPSG:4326").total_bounds
        if (
            not np.isfinite(geographic).all()
            or geographic[0] < -180
            or geographic[2] > 180
            or geographic[1] < -90
            or geographic[3] > 90
        ):
            raise ValueError(f"{label}: coordinates fall outside the declared CRS domain.")


def _resolve_source(spec: Any, catalog: Any, cwd: Path, label: str) -> tuple[Any, dict]:
    kind = spec.source_type
    file_path = spec.file_path
    source_name = spec.source_name
    geojson = spec.geojson
    raw = json.dumps(geojson) if isinstance(geojson, dict) else geojson
    actual_path: Path | None = None
    if file_path or (source_name and kind in ("", "file")):
        value = file_path or source_name
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = cwd / path
        if file_path or path.exists():
            actual_path = path.resolve()
            file_path = str(actual_path)
        elif catalog is not None:
            connector = catalog.get_connector("file")
            for attribute in ("resolve_path", "resolve"):
                resolver = getattr(connector, attribute, None)
                if callable(resolver):
                    found = resolver(source_name)
                    if found is not None:
                        actual_path = Path(found).expanduser().resolve()
                        file_path = str(actual_path)
                    break
    if raw:
        # The shared reader rejects empty collections. Empty analysis selections
        # are valid inputs here, so preserve their CRS without guessing coordinates.
        parsed = json.loads(raw)
        if parsed.get("type") == "FeatureCollection" and parsed.get("features") == []:
            import geopandas as gpd

            declared = (parsed.get("crs") or {}).get("properties", {}).get("name")
            frame = gpd.GeoDataFrame(geometry=[], crs=spec.crs or declared or "EPSG:4326")
        else:
            frame = resolve_geodataframe(geojson_text=raw, catalog=catalog)
            if spec.crs:
                # Inline coordinates are explicitly declared by the caller;
                # override the shared reader's RFC 7946 WGS84 default.
                frame = frame.set_crs(spec.crs, allow_override=True)
    else:
        frame = resolve_geodataframe(
            source_type=kind,
            file_path=file_path,
            source_name=source_name,
            catalog=catalog,
        ).copy()
        if spec.crs:
            from pyproj import CRS

            declared = CRS.from_user_input(spec.crs)
            if frame.crs is None:
                frame = frame.set_crs(declared)
            elif frame.crs != declared:
                raise ValueError(
                    f"{label}: source.crs disagrees with the file/database CRS; "
                    "reproject explicitly rather than relabelling known coordinates."
                )
    _validate_frame(frame, label)
    provenance = {
        "source_type": "inline" if raw else (kind or "file"),
        "file_path": str(actual_path) if actual_path else "",
        "source_name": source_name,
        "crs": frame.crs.to_string(),
        "feature_count": len(frame),
        "loaded_features_sha256": hashlib.sha256(
            frame.to_json(default=str).encode("utf-8")
        ).hexdigest(),
    }
    if raw:
        provenance["geojson_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return frame.reset_index(drop=True), provenance


def _metric_projection(frames: list[Any], requested: str) -> Any:
    from pyproj import CRS

    if requested:
        crs = CRS.from_user_input(requested)
    else:
        import geopandas as gpd

        geometries = []
        for frame in frames:
            geometries.extend(frame.to_crs("EPSG:4326").geometry.tolist())
        if not geometries:
            raise ValueError("All inputs are empty; configure analysis.metric_crs explicitly.")
        extent = gpd.GeoSeries(geometries, crs="EPSG:4326")
        minx, miny, maxx, maxy = extent.total_bounds
        if maxx - minx > 6 or miny < -80 or maxy > 84:
            raise ValueError(
                "Input extent is unsuitable for automatic local UTM projection; "
                "configure analysis.metric_crs explicitly."
            )
        crs = extent.estimate_utm_crs()
        if crs is None:
            raise ValueError("Cannot estimate UTM; configure analysis.metric_crs explicitly.")
    if (
        not crs.is_projected
        or len(crs.axis_info) < 2
        or any(not math.isclose(axis.unit_conversion_factor, 1) for axis in crs.axis_info[:2])
    ):
        raise ValueError("analysis.metric_crs must be a projected CRS with metre units.")
    return crs


def _reserve_columns(frame: Any, names: list[str]) -> Any:
    conflicts = set(frame.columns).intersection(names)
    if conflicts:
        raise ValueError(f"Input columns conflict with result fields: {sorted(conflicts)}.")
    return frame.copy()


def _counts(source: Any, zones: Any, affected: Any = None) -> Any:
    import geopandas as gpd

    names = ["_gh_zone_id", "total_count"]
    if affected is not None:
        names.append("affected_count")
    result = _reserve_columns(zones, names)
    result["_gh_zone_id"] = range(len(result))
    result["total_count"] = 0
    if affected is not None:
        result["affected_count"] = 0
    if source.empty or zones.empty:
        return result
    selected = source[[source.geometry.name]]
    matches = gpd.sjoin(selected, zones[[zones.geometry.name]], predicate="intersects")
    total = matches.groupby("index_right").size()
    result["total_count"] = total.reindex(result.index, fill_value=0).astype(int)
    if affected is not None:
        affected_matches = matches.loc[matches.index.isin(source.index[affected])]
        counts = affected_matches.groupby("index_right").size()
        result["affected_count"] = counts.reindex(result.index, fill_value=0).astype(int)
    return result


def _run_operation(action: str, source: Any, reference: Any, zones: Any, distance: float) -> dict:
    import geopandas as gpd
    import pandas as pd
    from shapely import union_all

    if action == "buffer":
        result = source.copy()
        result.geometry = source.buffer(distance)
        return {"result": result}
    if action == "count":
        return {"result": _counts(source, zones if zones is not None else reference)}
    if action in ("clip", "intersection"):
        if reference.empty or source.empty:
            return {"result": source.iloc[:0].copy()}
        if action == "clip":
            result = source.copy()
            result.geometry = source.geometry.intersection(union_all(reference.geometry.array))
            return {"result": result.loc[~result.geometry.is_empty].reset_index(drop=True)}
        left = _reserve_columns(source, ["_gh_source_id", "_gh_reference_id"])
        right = _reserve_columns(reference, ["_gh_source_id", "_gh_reference_id"])
        left["_gh_source_id"] = range(len(left))
        right["_gh_reference_id"] = range(len(right))
        return {
            "result": gpd.overlay(
                left, right, how="intersection", keep_geom_type=False, make_valid=False
            )
        }
    if reference.empty:
        raise ValueError(f"{action}: reference contains no features; distance is undefined.")
    names = ["distance_m"] + (["affected"] if action == "impact" else [])
    result = _reserve_columns(source, names)
    distances = source.geometry.distance(union_all(reference.geometry.array))
    result["distance_m"] = pd.Series(distances, index=source.index, dtype=float)
    artifacts = {"result": result}
    if action == "impact":
        result["affected"] = distances <= distance
        artifacts["affected"] = result.loc[result["affected"]].reset_index(drop=True)
        if zones is not None:
            artifacts["zones"] = _counts(source, zones, result["affected"])
    return artifacts


def _output_stem(value: str) -> str:
    if (
        not value
        or value in (".", "..")
        or value[-1] in (" ", ".")
        or any(char in value for char in '/\\:<>"|?*')
        or any(ord(char) < 32 for char in value)
        or value.split(".")[0].upper()
        in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
            *(f"LPT{i}" for i in range(1, 10))}
    ):
        raise ValueError("output_name must be a plain file stem without path components.")
    return value


def analyze_vectors(
    args: Any,
    *,
    cwd: Path,
    catalog: Any = None,
    outputs_dir: str,
    metric_crs: str = "",
    timeout_seconds: float = 120,
    cancelled: Event | None = None,
    publication_settings: PublicationConfig | None = None,
) -> dict[str, Any]:
    """Run one operation and publish GeoJSON, CSV and a JSON manifest atomically.

    Source features retain their row order. Distances refer to any part of each
    geometry, rather than centroids. CRS changes never modify the original data.
    """
    cancelled = cancelled or Event()
    deadline = time.monotonic() + timeout_seconds
    stem = _output_stem(args.output_name or args.action)
    root = Path(outputs_dir).expanduser()
    if not root.is_absolute():
        root = cwd / root
    root = root.resolve()
    loaded = {}
    provenance = {}
    for label in ("source", "reference", "zones"):
        _check_deadline(deadline, cancelled)
        spec = getattr(args, label)
        if spec is not None:
            loaded[label], provenance[label] = _resolve_source(spec, catalog, cwd, label)
    projection = _metric_projection(list(loaded.values()), metric_crs)
    metric = {label: frame.to_crs(projection) for label, frame in loaded.items()}
    for label, frame in metric.items():
        _validate_frame(frame, label)
    _check_deadline(deadline, cancelled)
    artifacts = _run_operation(
        args.action, metric["source"], metric.get("reference"), metric.get("zones"),
        args.distance_m,
    )
    statistics = {"source_count": len(metric["source"]), "feature_count": len(artifacts["result"])}
    if args.action == "impact":
        statistics["affected_count"] = len(artifacts["affected"])
        statistics["unaffected_count"] = len(metric["source"]) - len(artifacts["affected"])
    if args.action in ("clip", "intersection", "distance", "impact"):
        artifacts["reference"] = metric["reference"]
    if args.action == "count":
        artifacts["source"] = metric["source"]
    paths = {}
    layers = []
    previews = {}
    directory = root / f"{stem}-{uuid.uuid4().hex[:12]}"
    if not directory.resolve().is_relative_to(root):
        raise ValueError("Output directory escapes analysis.outputs_dir.")
    protected = {Path(item["file_path"]).resolve() for item in provenance.values()
                 if item["file_path"]}
    _check_deadline(deadline, cancelled)
    root.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(dir=root, prefix=".staging-"))
    publication_ready = False
    try:
        for label, frame in artifacts.items():
            _check_deadline(deadline, cancelled)
            exported = frame.to_crs("EPSG:4326")
            geojson_path = directory / f"{label}.geojson"
            csv_path = directory / f"{label}.csv"
            if geojson_path.resolve() in protected or csv_path.resolve() in protected:
                raise ValueError("Analysis output would overwrite an input file.")
            (stage / geojson_path.name).write_text(exported.to_json(default=str), encoding="utf-8")
            import pandas as pd

            table = pd.DataFrame(exported.drop(columns=exported.geometry.name))
            table[exported.geometry.name] = exported.geometry.to_wkt()
            table.to_csv(stage / csv_path.name, index=False, encoding="utf-8")
            # ToolResult metadata is not sent to the language model. Supply a
            # bounded attribute preview in the manifest and the visible result.
            previews[label] = json.loads(
                table.drop(columns=exported.geometry.name).head(20).to_json(
                    orient="records", date_format="iso",
                )
            )
            paths[label] = {
                "geojson": str(geojson_path), "csv": str(csv_path), "feature_count": len(frame)
            }
            if not frame.empty and label != "affected":
                layers.append({
                    "source_type": "file", "file_path": str(geojson_path), "label": label
                })
        # Base/context layers precede the main result so assets remain visible.
        layers.sort(key=lambda item: item["label"] == "result")
        manifest_path = directory / "manifest.json"
        manifest = {
            "action": args.action,
            "metric_crs": projection.to_string(),
            "export_crs": "EPSG:4326",
            "sources": provenance,
            "parameters": {"distance_m": args.distance_m, "output_name": stem},
            "statistics": statistics,
            "paths": paths,
            "layers": layers,
            "table_previews": previews,
            "preview_row_limit": 20,
            "manifest_path": str(manifest_path),
            "geometry_semantics": "Shortest planar distance between any parts of the geometries.",
        }
        if args.action in ("count", "impact"):
            manifest["count_semantics"] = _COUNT_SEMANTICS
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        publication_ready = True
        manifest["publication"] = publish_directory(
            stage, directory, settings=publication_settings,
            before_attempt=lambda: _check_deadline(deadline, cancelled),
        )
    except BaseException as error:
        diagnostics = getattr(error, "diagnostics", None)
        retain_stage = publication_ready and (
            isinstance(error, DirectoryPublicationError)
            or (isinstance(diagnostics, dict) and diagnostics.get("status") == "interrupted")
        )
        if retain_stage:
            # Exports are complete. A blocked or interrupted publication does
            # not require rerunning GIS; preserve the original exception type.
            try:
                (stage / "publication.json").write_text(
                    json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                # Diagnostic writes must not hide the publication/timeout error.
                pass
        else:
            shutil.rmtree(stage, ignore_errors=True)
        raise
    return manifest
