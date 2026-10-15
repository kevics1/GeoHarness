"""Measured execution contracts, independent of model-written success messages.

These checks establish data integrity, not scientific validity of a method.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any


def preflight_code(code: str) -> list[dict]:
    """Compile before starting GIS; check unconditional top-level dependencies only."""
    tree = ast.parse(code)
    compile(tree, "analysis.py", "exec")
    checks = []
    for node in tree.body:
        names = ([alias.name.split('.')[0] for alias in node.names]
                 if isinstance(node, ast.Import) else
                 [node.module.split('.')[0]] if isinstance(node, ast.ImportFrom)
                 and node.module and not node.level else [])
        for name in names:
            if importlib.util.find_spec(name) is None:
                raise ModuleNotFoundError(f"Required dependency '{name}' is not installed; "
                                          "use an installed implementation before execution")
            checks.append({"check": "import_available", "module": name})
    return checks


def validate_vector(frame: Any) -> dict:
    import numpy as np

    if frame.crs is None:
        raise ValueError("Vector output has no CRS; establish its real source CRS first")
    if len(frame):
        if frame.geometry.isna().any() or frame.geometry.is_empty.any():
            raise ValueError("Vector output contains null/empty geometry; keep explicit "
                             "unreachable statuses in a table instead of empty routes")
        if not frame.geometry.is_valid.all() or not np.isfinite(frame.total_bounds).all():
            raise ValueError("Vector output contains invalid/nonfinite geometry")
        b = frame.total_bounds
        if frame.crs.is_geographic and (b[0] < -180 or b[2] > 180
                                       or b[1] < -90 or b[3] > 90):
            raise ValueError("Geographic output has projected coordinates; transform with "
                             "to_crs(), do not relabel CRS")
    return {"crs": frame.crs.to_string(), "feature_count": len(frame),
            "schema": {str(k): str(v) for k, v in frame.dtypes.items()},
            "bounds": frame.total_bounds.tolist() if len(frame) else None}


class EntityTracker:
    """Check preserved unique attributes against their source geometry at writes.

    Geometry-changing analyses cannot be certified by this comparison. They are
    explicitly reported as unverified rather than assigned an invented identity.
    """

    def __init__(self) -> None:
        self.sources: list[tuple[str, Any]] = []
        self.writes: dict[str, dict] = {}

    def record(self, path: Any, frame: Any) -> Any:
        if len(frame) and frame.crs is not None:
            self.sources.append((str(path), frame.copy(deep=True)))
        return frame

    def check(self, frame: Any) -> dict:
        import pandas as pd
        from shapely import normalize, set_precision, to_wkb

        detail = validate_vector(frame)
        checked = []
        for path, source in self.sources:
            # Only immutable, unique shared identifiers can establish identity.
            keys = [c for c in source.columns if c != source.geometry.name
                    and c in frame.columns and source[c].notna().all()
                    and source[c].is_unique and frame[c].notna().all()
                    and frame[c].is_unique]
            # A unique computed rank/score/distance is not an immutable identity.
            # Prefer explicit ID names, then entity names. Unknown schemas stay
            # unverified rather than inventing a key from a numeric measurement.
            id_names = {'id', 'fid', 'objectid', 'uuid', '__geoh_id', '编号', '唯一编号'}
            name_names = {'name', '名称', '名字', '地名', '居民点名称', '场所名称'}
            ids = [c for c in keys if str(c).lower() in id_names
                   or str(c).lower().endswith('_id')]
            names = [c for c in keys if str(c).lower() in name_names
                     or str(c).lower().endswith('_name')]
            keys = (['__geoh_id'] if '__geoh_id' in ids else ids or names)[:1]
            if not keys or not len(frame):
                continue
            aligned = source.to_crs(frame.crs)
            precision = 1e-8 if frame.crs.is_geographic else 1e-4

            def hashes(geometries):
                return [to_wkb(normalize(set_precision(g, precision))).hex()
                        for g in geometries]

            original = hashes(aligned.geometry)
            actual = hashes(frame.geometry)
            lookup: dict[str, list[int]] = {}
            for i, value in enumerate(original):
                lookup.setdefault(value, []).append(i)
            matched = [(i, lookup[value]) for i, value in enumerate(actual) if value in lookup]
            if not matched:
                continue  # Derived geometry: no assertion of preserved identity.
            # Compare each usable identifier independently; never use positional joins.
            for key in keys:
                source_values = set(source[key].map(str))
                if not frame[key].map(str).isin(source_values).all():
                    continue  # This field was deliberately recomputed/renamed.
                for row, candidates in matched:
                    value = frame.iloc[row][key]
                    if not any(pd.api.types.is_scalar(value)
                               and value == source.iloc[j][key] for j in candidates):
                        raise ValueError(f"Entity/geometry mismatch in '{key}' at output row "
                                         f"{row}, source={path}. Keep geometry attached while "
                                         "sorting; use entity_join(source, table, key=...) "
                                         "instead of geometry.values positional assignment")
                checked.append({"source": path, "key": str(key), "rows": len(matched)})
        binding = ('passed' if checked and any(item['rows'] == len(frame) for item in checked)
                   else 'partial' if checked else 'unverified')
        return {**detail, "entity_binding": binding,
                "identity_checks": checked}


def entity_join(source: Any, table: Any, *, key: str, how: str = "left") -> Any:
    """Join attributes by a verified unique key, retaining the source geometry."""
    if key not in source or key not in table:
        raise ValueError(f"Entity key '{key}' must exist in both inputs")
    for value in (source, table):
        if value[key].isna().any() or not value[key].is_unique:
            raise ValueError(f"Entity key '{key}' must be unique and nonnull")
    if how not in {"left", "inner"}:
        raise ValueError("entity_join supports left or inner joins only")
    if not table[key].isin(source[key]).all():
        raise ValueError("Attribute table contains unknown source entity keys")
    geometry_name = source.geometry.name
    if geometry_name in table.columns:
        raise ValueError("Attribute table must not carry a second geometry column")
    return source.merge(table, on=key, how=how, validate="one_to_one")


def score_risk(frame: Any, *, key: str, components: list[dict],
               missing_policy: str = "error") -> Any:
    """Explicit weighted score; returns assumptions, never a validated risk model.

    Each component names field/weight/minimum/maximum/direction (higher or lower).
    Missing status preserves records without inventing a zero risk value.
    """
    import math

    import numpy as np
    import pandas as pd

    if key not in frame or frame[key].isna().any() or not frame[key].is_unique:
        raise ValueError("Risk key must be an existing unique nonnull entity ID")
    if missing_policy not in {'error', 'unknown'} or not components:
        raise ValueError("Specify components and missing_policy=error or unknown")
    if 'risk_score' in frame or 'risk_status' in frame:
        raise ValueError("risk_score/risk_status already exist; preserve prior scoring explicitly")
    weights, normalized, names = [], [], set()
    for spec in components:
        if set(spec) != {'field', 'weight', 'minimum', 'maximum', 'direction'}:
            raise ValueError("Each risk component requires field/weight/minimum/maximum/direction")
        name = spec['field']
        if name not in frame or name in names:
            raise ValueError(f"Risk field missing or duplicated: {name}")
        names.add(name)
        weight, low, high = (float(spec[k]) for k in ('weight', 'minimum', 'maximum'))
        if not all(math.isfinite(v) for v in (weight, low, high)) or weight <= 0 or low >= high:
            raise ValueError("Risk weights must be positive; finite minimum < maximum required")
        values = pd.to_numeric(frame[name], errors='raise').astype(float)
        if np.isinf(values).any() or ((values < low) | (values > high)).any():
            raise ValueError(f"Risk field {name} is infinite or outside declared "
                             "normalization limits")
        if values.isna().any() and missing_policy == 'error':
            raise ValueError(f"Risk field {name} contains missing values; "
                             "choose an explicit policy")
        values = (values - low) / (high - low)
        if spec['direction'] == 'lower':
            values = 1 - values
        elif spec['direction'] != 'higher':
            raise ValueError("Risk direction must be higher or lower")
        weights.append(weight)
        normalized.append(values)
    if not math.isclose(sum(weights), 1, rel_tol=0, abs_tol=1e-9):
        raise ValueError("Risk weights must sum to 1; no silent renormalization")
    result = frame.copy()
    result['risk_score'] = sum(value * weight for value, weight in zip(normalized, weights))
    result['risk_status'] = np.where(result.risk_score.isna(), 'unknown', 'computed')
    result.attrs['risk_method'] = {'components': components, 'missing_policy': missing_policy,
                                 'scientific_correctness': 'unverified'}
    return result


def sample_raster_points(path: Any, points: Any, *, band: int = 1) -> Any:
    """Point sampling without the invalid Point.buffer(0) masking pattern."""
    import numpy as np
    import rasterio

    if getattr(points, 'crs', None) is None:
        raise ValueError("Point sampling requires CRS-bearing GeoDataFrame/GeoSeries")
    shapes = points.geometry
    if (shapes.isna().any() or shapes.is_empty.any()
            or not shapes.geom_type.eq('Point').all()):
        raise ValueError("Point sampling requires nonempty finite Point geometries")
    with rasterio.open(path) as dataset:
        if dataset.crs is None or not 1 <= band <= dataset.count:
            raise ValueError("Raster CRS/band is unknown or invalid")
        projected = points.to_crs(dataset.crs).geometry
        coords = [(point.x, point.y) for point in projected]
        if not np.isfinite(coords).all():
            raise ValueError("Point reprojection produced nonfinite coordinates")
        values = list(dataset.sample(coords, indexes=band, masked=True))
        if not values:
            return np.array([])
        result = np.ma.concatenate(values).astype(float).filled(np.nan)
        pixels = [(~dataset.transform) * xy for xy in coords]
        for index, (column, row) in enumerate(pixels):
            if not (0 <= column < dataset.width and 0 <= row < dataset.height):
                result[index] = np.nan  # Outside is unknown even without a nodata sentinel.
        return result


def artifact_metadata(path: Path, *, succeeded: bool) -> dict:
    """Read outputs back; a successful process alone does not validate artifacts."""
    record = {"path": str(path), "size_bytes": path.stat().st_size,
              "state": "succeeded" if succeeded else "failed",
              "validation_state": "unverified"}
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    record["sha256"] = digest.hexdigest()
    if not succeeded:
        return record
    try:
        if path.suffix.lower() in {".geojson", ".gpkg", ".shp"}:
            import geopandas as gpd

            record.update(validate_vector(gpd.read_file(path)))
            record["validation_state"] = "passed"
        elif path.suffix.lower() == ".csv":
            import pandas as pd

            table = pd.read_csv(path)
            record.update(row_count=len(table), schema={str(k): str(v)
                          for k, v in table.dtypes.items()})
    except Exception as exc:
        record.update(state="failed", validation_state="failed", error_summary=str(exc))
    return record


def write_contract_report(path: Path, tracker: EntityTracker) -> None:
    path.write_text(json.dumps({"vector_writes": tracker.writes,
                               "scientific_correctness": "unverified"},
                              ensure_ascii=False, indent=2), encoding="utf-8")
