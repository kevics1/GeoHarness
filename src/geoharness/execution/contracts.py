"""Measured execution contracts, independent of model-written success messages.

These checks establish data integrity, not scientific validity of a method.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import math
from pathlib import Path
from typing import Any


def preflight_code(code: str, input_aliases=None) -> list[dict]:
    """Compile before starting GIS; check unconditional top-level dependencies only."""
    tree = ast.parse(code)
    compile(tree, "analysis.py", "exec")
    if input_aliases is not None:
        for node in ast.walk(tree):
            if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)
                    and node.value.id == 'INPUTS' and isinstance(node.slice, ast.Constant)
                    and isinstance(node.slice.value, str)
                    and node.slice.value not in input_aliases):
                raise ValueError(f"INPUTS[{node.slice.value!r}] is not bound; declare input_refs. "
                                 f"Available aliases: {sorted(input_aliases)}")
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


def validate_input(path: Any, requirement: dict) -> dict:
    """Read the actual intermediate under the child's file guards before user code."""
    import geopandas as gpd
    import numpy as np
    import pandas as pd
    from pyproj import CRS

    if Path(path).suffix.lower() == '.csv':
        frame = pd.read_csv(path)
    else:
        options = {'layer': requirement['layer']} if requirement.get('layer') else {}
        frame = gpd.read_file(path, **options)
    schema = {str(k): str(v) for k, v in frame.dtypes.items()}
    fields = set(requirement.get('fields', [])) | set(requirement.get('finite_fields', []))
    if requirement.get('key'):
        fields.add(requirement['key'])
    missing = sorted(fields - set(frame.columns))
    if missing:
        raise ValueError(f"Input {Path(path).name}: missing fields {missing}; "
                         f"actual schema={schema}")
    key = requirement.get('key')
    if key and (frame[key].isna().any() or not frame[key].is_unique):
        raise ValueError(f"Input {Path(path).name}: key {key!r} must be unique and non-null")
    for name in requirement.get('finite_fields', []):
        if not np.isfinite(pd.to_numeric(frame[name], errors='raise')).all():
            raise ValueError(f"Input {Path(path).name}: field {name!r} must be finite/non-null")
    crs = getattr(frame, 'crs', None)
    if requirement.get('crs') and (crs is None or CRS(crs) != CRS(requirement['crs'])):
        raise ValueError(f"Input {Path(path).name}: expected CRS {requirement['crs']}; "
                         f"actual={crs}. "
                         "Transform explicitly; never relabel coordinates to satisfy a contract")
    if requirement.get('metric'):
        require_metric(crs)
    detail = validate_vector(frame) if requirement.get('valid_geometry') else {}
    return {'path': str(path), 'rows': len(frame), 'schema': schema,
            'crs': str(crs) if crs else None, **detail}


def require_metric(value: Any) -> Any:
    import math

    from pyproj import CRS

    if value is None:
        raise ValueError('A verified projected CRS with metre units is required')
    crs = CRS(value)
    if (not crs.is_projected or len(crs.axis_info) < 2
            or any(not math.isclose(axis.unit_conversion_factor, 1)
                   for axis in crs.axis_info[:2])):
        raise ValueError('A verified projected CRS with metre units is required')
    return crs


def describe_input(value: Any) -> dict:
    """Actual in-memory schema/shape, not inferred fields or a second analysis run."""
    import geopandas as gpd
    import numpy as np
    import pandas as pd

    if isinstance(value, pd.DataFrame):
        result = {'kind': 'table', 'rows': len(value),
                  'schema': {str(k): str(v) for k, v in value.dtypes.items()},
                  'null_counts': {str(k): int(v) for k, v in value.isna().sum().items()}}
        if isinstance(value, gpd.GeoDataFrame):
            result.update(crs=str(value.crs) if value.crs else None,
                          geometry_column=value.geometry.name,
                          geometry_types=value.geometry.geom_type.value_counts().to_dict())
        return result
    if isinstance(value, np.ndarray):
        result = {'kind': 'array', 'shape': list(value.shape), 'ndim': value.ndim,
                  'dtype': str(value.dtype), 'size': int(value.size)}
        if np.issubdtype(value.dtype, np.number):
            result['nonfinite_count'] = int((~np.isfinite(value)).sum())
        return result
    if hasattr(value, 'count') and hasattr(value, 'transform') and hasattr(value, 'crs'):
        return {'kind': 'raster', 'shape': [value.count, value.height, value.width],
                'crs': str(value.crs) if value.crs else None, 'dtypes': list(value.dtypes),
                'nodata': str(value.nodata), 'bounds': list(value.bounds)}
    raise TypeError('describe_input accepts an actual DataFrame, ndarray or opened raster dataset')


def extent_geometry(*, west: float, south: float, east: float, north: float, crs: Any) -> Any:
    """Named bounds, explicit CRS; ready for mask_raster or to_crs, never a bare box."""
    import geopandas as gpd
    import numpy as np
    from shapely.geometry import box

    if not np.isfinite([west, south, east, north]).all() or west >= east or south >= north:
        raise ValueError('Extent requires finite west < east and south < north')
    result = gpd.GeoSeries([box(west, south, east, north)], crs=crs)
    validate_vector(result.to_frame(name='geometry'))
    return result


def validate_vector(frame: Any) -> dict:
    import numpy as np

    if frame.crs is None:
        raise ValueError("Vector output has no CRS; establish its real source CRS first")
    if len(frame):
        if frame.geometry.isna().any() or frame.geometry.is_empty.any():
            raise ValueError("Vector output contains null/empty geometry; keep explicit "
                             "unreachable statuses in a table instead of empty routes")
        if not frame.geometry.is_valid.all() or not np.isfinite(frame.total_bounds).all():
            from shapely.validation import explain_validity

            invalid = ~frame.geometry.is_valid
            samples = [{'row': str(index)[:80], 'reason': explain_validity(geometry)[:240]}
                       for index, geometry in frame.loc[invalid].geometry.iloc[:8].items()]
            raise ValueError("Vector output contains invalid/nonfinite geometry; "
                             + json.dumps({'invalid_rows': int(invalid.sum()),
                                           'samples': samples}, ensure_ascii=False))
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
    if missing_policy not in {'error', 'unknown', 'renormalize'} or not components:
        raise ValueError("Specify components and missing_policy=error, unknown or renormalize")
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
    if missing_policy == 'renormalize':
        available = sum(value.notna().astype(float) * weight
                        for value, weight in zip(normalized, weights))
        total = sum(value.fillna(0) * weight for value, weight in zip(normalized, weights))
        result['risk_score'] = total / available.where(available > 0)
    else:
        result['risk_score'] = sum(value * weight for value, weight in zip(normalized, weights))
    result['risk_status'] = np.where(result.risk_score.isna(), 'unknown', 'computed')
    result.attrs['risk_method'] = {'components': components, 'missing_policy': missing_policy,
                                 'scientific_correctness': 'unverified'}
    return result


def first_coverage(points: Any, stages: Any, *, key: str, stage_key: str,
                   stage_order: list, boundary: str) -> Any:
    """最早覆盖阶段。调用者明确时序与边界规则；从未覆盖为NA，不用最近距离代替。"""
    import pandas as pd

    validate_vector(points)
    validate_vector(stages)
    if (key not in points or points[key].isna().any() or not points[key].is_unique
            or not points.geom_type.eq('Point').all()):
        raise ValueError('first_coverage requires Point geometries and a unique nonnull key')
    if (stage_key not in stages or stages[stage_key].isna().any()
            or not stages.geom_type.isin(['Polygon', 'MultiPolygon']).all()):
        raise ValueError('Stages require nonnull stage keys and polygon geometries')
    if (not stage_order or len(set(stage_order)) != len(stage_order)
            or set(stage_order) != set(stages[stage_key])):
        raise ValueError('stage_order must explicitly list every actual stage once; '
                         'ask the user when chronological order is unknown')
    if boundary not in {'covers', 'contains'}:
        raise ValueError('Specify boundary=covers (include boundary) or contains (exclude)')
    if {'first_stage_hit', 'coverage_status'} & set(points.columns):
        raise ValueError('Coverage fields already exist; preserve prior results explicitly')
    projected = points.to_crs(stages.crs)
    validate_vector(projected)
    hits = [None] * len(points)
    for stage in stage_order:
        shapes = stages.loc[stages[stage_key] == stage].geometry
        for i, point in enumerate(projected.geometry):
            if hits[i] is None and any(getattr(shape, boundary)(point) for shape in shapes):
                hits[i] = stage
    result = points.copy()
    result['first_stage_hit'] = pd.array(hits, dtype=object)
    result['coverage_status'] = ['not_covered' if value is None else 'covered' for value in hits]
    result.attrs['coverage_method'] = {'stage_key': stage_key, 'stage_order': stage_order,
        'boundary': boundary, 'scope': 'coverage_of_supplied_forecasts; actual_arrival_unverified'}
    return result


def make_leader_lines(points: Any, labels: Any, *, key: str, metric_crs: Any) -> Any:
    """按实体ID连接真实点和标签锚点；同位置不需要引线，不能输出退化线。"""
    import geopandas as gpd
    from shapely.geometry import LineString

    require_metric(metric_crs)
    for frame in (points, labels):
        validate_vector(frame)
        if (key not in frame or frame[key].isna().any() or not frame[key].is_unique
                or not frame.geom_type.eq('Point').all()):
            raise ValueError('Leader endpoints require Points and unique nonnull keys')
    if set(points[key]) != set(labels[key]):
        raise ValueError('Leader point/label entity keys must match exactly')
    if key == 'geometry':
        raise ValueError('Leader identity field cannot be geometry')
    starts, ends = points.to_crs(metric_crs), labels.to_crs(metric_crs).set_index(key)
    records, omitted = [], []
    for _, row in starts.iterrows():
        entity = row[key]
        end = ends.geometry.loc[entity]
        start = row[starts.geometry.name]
        if start.equals(end):
            omitted.append(entity)
        else:
            records.append({key: entity, 'geometry': LineString([start, end])})
    result = gpd.GeoDataFrame(records, columns=[key, 'geometry'], crs=metric_crs)
    validate_vector(result)
    result.attrs['omitted_zero_length_keys'] = omitted
    result.attrs['leader_check'] = check_leader_lines(
        points, labels, result, key=key, metric_crs=metric_crs)
    return result


def check_leader_lines(points: Any, labels: Any, lines: Any, *, key: str, metric_crs: Any,
                       tolerance_m: float = .01, source_gap_m: float = 0,
                       target_gap_m: float = 0) -> dict:
    """Check straight callouts against CURRENT anchors, including explicit symbol-edge gaps."""
    import numpy as np

    require_metric(metric_crs)
    if not np.isfinite([tolerance_m, source_gap_m, target_gap_m]).all() or min(
            tolerance_m, source_gap_m, target_gap_m) < 0:
        raise ValueError('Leader tolerance/gaps must be finite and nonnegative metres')
    for frame, kind in ((points, 'Point'), (labels, 'Point'), (lines, 'LineString')):
        validate_vector(frame)
        if (key not in frame or key == frame.geometry.name or frame[key].isna().any()
                or not frame[key].is_unique or not frame.geom_type.eq(kind).all()):
            raise ValueError('Leader geometries need the declared type and unique non-null keys')
    if set(points[key]) != set(labels[key]):
        raise ValueError('Leader point/label entity keys must match exactly')
    p, a, line_table = (f.to_crs(metric_crs).set_index(key) for f in (points, labels, lines))
    for projected in (p, a, line_table):
        validate_vector(projected)
    if not set(line_table.index) <= set(p.index):
        raise ValueError('Leader lines contain unknown entity keys')
    issues, omitted, maximum = [], [], 0.
    for entity in p.index:
        entity_value = entity.item() if hasattr(entity, 'item') else entity
        start = np.array(p.geometry.loc[entity].coords[0])[:2]
        target = np.array(a.geometry.loc[entity].coords[0])[:2]
        length = float(np.linalg.norm(target - start))
        if length <= source_gap_m + target_gap_m:
            omitted.append(entity_value)
            if entity in line_table.index:
                issues.append({'key': entity_value, 'reason': 'unnecessary_or_degenerate_line'})
            continue
        if entity not in line_table.index:
            issues.append({'key': entity_value, 'reason': 'missing_line'})
            continue
        vector = (target - start) / length
        expected = np.array([start + vector * source_gap_m, target - vector * target_gap_m])
        coords = np.array(line_table.geometry.loc[entity].coords)[:, :2]
        error = min(np.max(np.linalg.norm(coords[[0, -1]] - expected, axis=1)),
                    np.max(np.linalg.norm(coords[[-1, 0]] - expected, axis=1)))
        perpendicular = np.abs((coords[:, 0] - start[0]) * vector[1]
                               - (coords[:, 1] - start[1]) * vector[0]).max()
        error = float(max(error, perpendicular))
        maximum = max(maximum, error)
        if error > tolerance_m:
            issues.append({'key': entity_value, 'reason': 'line_does_not_connect_current_anchor',
                           'endpoint_or_direction_error_m': error})
    return {'status': 'failed' if issues else 'passed', 'checked_count': len(p),
            'line_count': len(line_table), 'max_endpoint_error_m': maximum, 'issues': issues,
            'omitted_zero_length_keys': omitted, 'metric_crs': str(metric_crs)}


def fit_frame_extent(frame: Any, *, metric_crs: Any, width_mm: float, height_mm: float,
                     padding_ratio: float = .08) -> dict:
    """Fit all declared subjects to an ACTUAL map-frame rectangle, not the paper size."""
    import numpy as np

    require_metric(metric_crs)
    validate_vector(frame)
    if (frame.empty or not np.isfinite([width_mm, height_mm, padding_ratio]).all()
            or min(width_mm, height_mm) <= 0 or padding_ratio < 0):
        raise ValueError('Fit requires nonempty subjects, finite positive frame size and padding')
    projected = frame.to_crs(metric_crs)
    validate_vector(projected)
    xmin, ymin, xmax, ymax = projected.total_bounds
    w, h = xmax - xmin, ymax - ymin
    if max(w, h) == 0:
        raise ValueError('Coincident subjects require an explicit spatial scale, '
                         'not a guessed extent')
    w, h = max(w, h * width_mm / height_mm), max(h, w * height_mm / width_mm)
    w, h = w * (1 + 2 * padding_ratio), h * (1 + 2 * padding_ratio)
    cx, cy = (xmin + xmax) / 2, (ymin + ymax) / 2
    return {'map_extent': [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2],
            'crs': str(metric_crs), 'subject_count': len(frame),
            'scale_denominator': float(w / (width_mm / 1000)),
            'frame_width_mm': width_mm, 'frame_height_mm': height_mm}


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
        elif path.suffix.lower() in {".tif", ".tiff"}:
            import rasterio

            with rasterio.open(path) as source:
                if source.crs is None or min(source.count, source.height, source.width) <= 0:
                    raise ValueError("Raster output requires CRS and positive "
                                     "band/row/column sizes")
                transform = tuple(source.transform)
                if not all(math.isfinite(v) for v in transform) \
                        or source.transform.determinant == 0:
                    raise ValueError("Raster output has invalid/nonfinite transform")
                record.update(crs=str(source.crs),
                              shape=[source.count, source.height, source.width],
                              transform=list(transform), dtypes=list(source.dtypes),
                              validation_state="passed",
                              validation_scope="raster shape/CRS/transform metadata only; "
                                               "pixel/scientific correctness unverified")
    except Exception as exc:
        record.update(state="failed", validation_state="failed", error_summary=str(exc))
    return record


def artifact_facts(path: Any, *, key: str, numeric_fields=(), predicates=(),
                   metric_crs=None, group_by=(), include_median: bool = False,
                   stratify_by=()) -> dict:
    """Recompute bounded statistics from a persisted table/vector, with its fingerprint.

    Predicates are {name, field, op, value}; missing values never count as matches.
    Area, when requested, is a sum of feature areas, not a union or fire forecast.
    """
    import operator

    import geopandas as gpd
    import numpy as np
    import pandas as pd

    path = Path(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    frame = pd.read_csv(path) if path.suffix.lower() == '.csv' else gpd.read_file(path)
    if key not in frame or frame[key].isna().any() or not frame[key].is_unique:
        raise ValueError('Facts require an existing unique non-null source key')
    result = {'scope': 'recomputed_persisted_values; scientific_method_unverified',
              'source': str(path), 'sha256': digest, 'key': key, 'row_count': len(frame),
              'crs': str(frame.crs) if getattr(frame, 'crs', None) else None,
              'numeric': {}, 'counts': {}, 'groups': {}}
    if stratify_by:
        if len(set(stratify_by)) != len(stratify_by) or any(f not in frame for f in stratify_by):
            raise ValueError('Stratification requires distinct existing fields')
        result.update(stratify_by=list(stratify_by), strata=[])
    for field in group_by:
        if field not in frame or field in result['groups']:
            raise ValueError(f'Grouping field missing or duplicated: {field!r}')
        result['groups'][field] = [
            {'value': None if pd.isna(value) else value.item()
                if hasattr(value, 'item') else value, 'count': int(count)}
            for value, count in frame[field].value_counts(dropna=False, sort=False).items()]
    for field in numeric_fields:
        if field not in frame:
            raise ValueError(f'Fact field {field!r} missing; actual fields={list(frame.columns)}')
        values = pd.to_numeric(frame[field], errors='raise')
        if np.isinf(values).any():
            raise ValueError(f'Fact field {field!r} contains infinity')
        valid = values.dropna()
        result['numeric'][field] = {
            'missing': int(values.isna().sum()), 'count': len(valid),
            **{name: float(getattr(valid, name)()) if len(valid) else None
               for name in ('min', 'max', 'sum', 'mean', *(['median'] if include_median else []))}}
    if stratify_by:
        for values, subset in frame.groupby(list(stratify_by), dropna=False, sort=False):
            if not isinstance(values, tuple):
                values = (values,)
            result['strata'].append({'group': {
                field: None if pd.isna(value) else value.item()
                if hasattr(value, 'item') else value
                for field, value in zip(stratify_by, values)}, 'row_count': len(subset),
                'numeric': {field: {stat: float(getattr(pd.to_numeric(subset[field],
                    errors='raise').dropna(), stat)()) if subset[field].notna().any() else None
                    for stat in ('min', 'max', 'median')}
                    for field in numeric_fields}})
    operators = {'<': operator.lt, '<=': operator.le, '>': operator.gt,
                 '>=': operator.ge, '==': operator.eq, '!=': operator.ne}
    for predicate in predicates:
        if set(predicate) != {'name', 'field', 'op', 'value'}:
            raise ValueError('Fact predicates require name/field/op/value')
        name, field, op = (predicate[k] for k in ('name', 'field', 'op'))
        if (not isinstance(name, str) or not name or name in result['counts']
                or field not in frame or op not in operators or predicate['value'] is None):
            raise ValueError('Fact predicates need unique names, existing fields and supported op')
        if isinstance(predicate['value'], (int, float)) and not np.isfinite(predicate['value']):
            raise ValueError('Fact predicate values must be finite')
        mask = frame[field].notna() & operators[op](frame[field], predicate['value'])
        result['counts'][name] = {'count': int(mask.sum()), 'predicate': predicate,
                                  'missing_excluded': int(frame[field].isna().sum())}
    if metric_crs is not None:
        require_metric(metric_crs)
        if not isinstance(frame, gpd.GeoDataFrame):
            raise ValueError('Area facts require a CRS-bearing vector')
        validate_vector(frame)
        shapes = frame.to_crs(metric_crs)
        validate_vector(shapes)
        result['area'] = {'sum_feature_area_m2': float(shapes.area.sum()),
                          'metric_crs': str(shapes.crs), 'semantics': 'sum; overlaps may repeat'}
    if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('Fact source changed while reading; discard these facts')
    return result


def write_analysis_report(path: Any, *, source: Any, key: str, numeric_fields=(),
                          group_by=(), predicates=(), method: dict, claims=(),
                          stratify_by=()) -> dict:
    """从落盘结果生成同源JSON/中文Markdown；方法为声明，任意报告文本不受认证。"""
    facts = artifact_facts(source, key=key, numeric_fields=numeric_fields,
                           group_by=group_by, predicates=predicates, include_median=True,
                           stratify_by=stratify_by)
    checked = check_claims(facts, list(claims)) if claims else None
    if checked and checked['status'] != 'passed':
        raise ValueError('Declared analysis claims disagree with persisted facts: '
                         + json.dumps(checked, ensure_ascii=False))
    if not isinstance(method, dict) or not method:
        raise ValueError('Record the explicit method/weights/missing-data policy; do not guess')
    if 'weights' in method:
        weights = method['weights']
        if (not isinstance(weights, dict) or not weights
                or any(type(v) not in (int, float) or not math.isfinite(v) or v <= 0
                       for v in weights.values())
                or not math.isclose(sum(weights.values()), 1, rel_tol=0, abs_tol=1e-9)):
            raise ValueError('Declared weights must be positive fractions summing to 1; '
                             'ask the user rather than silently normalizing')
    # Reject nonfinite/unserializable declarations before writing either output.
    json.dumps(method, allow_nan=False)
    report = {'schema_version': 1, 'kind': 'persisted_analysis_report', 'facts': facts,
              'method': method, 'method_status': 'declared_not_independently_verified',
              'claims': checked, 'analysis_correctness': 'unverified'}
    path = Path(path)
    if path.suffix.lower() != '.json':
        raise ValueError('Analysis report path must end in .json')
    source_path = Path(source).resolve()
    if source_path in {path.resolve(), path.with_suffix('.md').resolve()}:
        raise ValueError('Report must not overwrite its source')
    lines = ['# 分析事实与方法记录', '', f"数据文件：{Path(source).name}",
             f"数据 SHA256：{facts['sha256']}", f"实体数：{facts['row_count']}", '',
             '以下统计从落盘结果重算；评分方法为声明，科学正确性尚未独立验证。', '',
             '## 方法与假设', '', '```json',
             json.dumps(method, ensure_ascii=False, indent=2), '```', '', '## 统计事实', '',
             '```json', json.dumps({k: facts[k] for k in ('numeric', 'counts', 'groups')},
                                  ensure_ascii=False, indent=2), '```', '']
    markdown = '\n'.join(lines)
    markdown_path = path.with_suffix('.md')
    markdown_path.write_text(markdown, encoding='utf-8')
    report['companions'] = {'markdown': {'path': str(markdown_path.resolve()),
        'sha256': hashlib.sha256(markdown_path.read_bytes()).hexdigest()}}
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False),
                    encoding='utf-8')
    return report


def check_claims(facts: dict, claims: list[dict]) -> dict:
    """Compare explicit leaf values; this never certifies arbitrary prose or a risk model."""
    import math

    checks = []
    for claim in claims:
        if set(claim) - {'path', 'expected', 'absolute_tolerance'} or not claim.get('path'):
            raise ValueError('Claim requires path list, expected and optional absolute_tolerance')
        if 'expected' not in claim or not isinstance(claim['path'], list):
            raise ValueError('Claim requires path list and expected')
        actual = facts
        for key in claim['path']:
            actual = actual[key]
        tolerance = float(claim.get('absolute_tolerance', 0))
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError('Claim tolerance must be finite and nonnegative')
        expected = claim['expected']
        if type(actual) in (int, float) and type(expected) in (int, float):
            matched = math.isfinite(actual) and math.isfinite(expected) and math.isclose(
                actual, expected, abs_tol=tolerance, rel_tol=0)
        else:
            matched = type(actual) is type(expected) and actual == expected
        checks.append({**claim, 'actual': actual, 'matched': bool(matched)})
    return {'scope': 'explicit_claims_only; prose_and_method_unverified',
            'status': 'passed' if checks and all(c['matched'] for c in checks) else 'failed',
            'checks': checks}


def write_contract_report(path: Path, tracker: EntityTracker, *, input_checks=None) -> None:
    path.write_text(json.dumps({"vector_writes": tracker.writes,
                               "input_checks": input_checks or {},
                               "scientific_correctness": "unverified"},
                              ensure_ascii=False, indent=2), encoding="utf-8")
