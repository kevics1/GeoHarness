"""Shared data and extent checks before native or QGIS map rendering.

Bounds in this module always use ``xmin, ymin, xmax, ymax``. Raster visibility
only proves overlap of the raster footprint; it does not inspect pixel values.
No QGIS runtime is imported, and numeric coercion never modifies a source file.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from numbers import Real
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_complex_dtype, is_numeric_dtype
from pyproj import CRS, Transformer
from shapely import get_coordinates
from shapely.geometry import box

from geoharness._proj_fix import ensure_proj_data
from geoharness.cartography.values import normalize_frame, normalize_style

_BOUND_NAMES = ("xmin", "ymin", "xmax", "ymax")
_FIELD_LIMIT = 64


def _bounds(values: Any, *, name: str, positive: bool = True) -> list[float]:
    try:
        if isinstance(values, (str, bytes, dict)) or len(values) != 4 or any(
            isinstance(value, bool) for value in values
        ):
            raise ValueError
        result = [float(value) for value in values]
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be xmin,ymin,xmax,ymax numeric bounds.") from exc
    if not all(math.isfinite(value) for value in result):
        raise ValueError(f"{name} must contain finite coordinates.")
    xmin, ymin, xmax, ymax = result
    invalid = xmin >= xmax or ymin >= ymax if positive else xmin > xmax or ymin > ymax
    if invalid:
        raise ValueError(f"{name} must be xmin,ymin,xmax,ymax with positive area.")
    return result


def named_bounds(bounds: list[float]) -> dict[str, float]:
    """Expose axis names explicitly, including zero-area source point bounds."""
    values = _bounds(bounds, name="bounds", positive=False)
    return dict(zip(_BOUND_NAMES, values))


def to_imshow_extent(bounds: list[float]) -> list[float]:
    """Convert GIS bounds to matplotlib's ``xmin,xmax,ymin,ymax`` order."""
    xmin, ymin, xmax, ymax = _bounds(bounds, name="imshow source bounds")
    return [xmin, xmax, ymin, ymax]


def _crs(value: Any, *, name: str) -> CRS:
    if value is None or value == "":
        raise ValueError(f"{name} has no declared CRS.")
    try:
        return CRS.from_user_input(value)
    except Exception as exc:
        raise ValueError(f"{name} has an invalid CRS: {value!r}.") from exc


def _geographic_bounds(bounds: list[float], crs: CRS, *, name: str) -> None:
    if crs.is_geographic and (
        bounds[0] < -180 or bounds[2] > 180 or bounds[1] < -90 or bounds[3] > 90
    ):
        raise ValueError(f"{name} geographic coordinates exceed longitude/latitude limits.")


def _transform_bounds(bounds: list[float], source: CRS, target: CRS, name: str) -> list[float]:
    if source == target:
        return bounds.copy()
    try:
        transformed = Transformer.from_crs(source, target, always_xy=True).transform_bounds(
            *bounds, densify_pts=21, errcheck=True,
        )
    except Exception as exc:
        raise ValueError(f"Cannot align {name} with display CRS.") from exc
    result = _bounds(transformed, name=f"{name} display bounds", positive=False)
    _geographic_bounds(result, target, name=name)
    return result


def _numeric_series(series: pd.Series) -> tuple[pd.Series | None, str]:
    """Accept finite real numbers or numeric strings that survive decimal round trip."""
    non_null = series.dropna()
    if non_null.empty:
        return None, "contains no non-null numeric values"
    if is_bool_dtype(series.dtype) or is_complex_dtype(series.dtype):
        return None, "must contain real numeric values"
    if is_numeric_dtype(series.dtype):
        if not all(math.isfinite(value) for value in non_null):
            return None, "contains non-finite numeric values"
        return series, ""
    try:
        converted = pd.to_numeric(series, errors="raise")
        for original, numeric in zip(series, converted):
            if pd.isna(original):
                continue
            if isinstance(original, bool) or not isinstance(original, (str, Real)):
                return None, "must contain real numbers or numeric strings"
            if not math.isfinite(numeric):
                return None, "contains non-finite numeric values"
            if Decimal(str(original).strip()) != Decimal(str(numeric)):
                return None, "numeric string conversion would lose precision"
    except (TypeError, ValueError, InvalidOperation, OverflowError):
        return None, "contains non-numeric values"
    return converted, ""


def _scalar(value: Any, *, name: str, null_allowed: bool = True) -> Any:
    value = value.item() if isinstance(value, np.generic) else value
    if value is None and null_allowed:
        return None
    if not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"{name} must contain readable scalar category values.")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} contains a non-finite category value.")
    return value


def _style_fields(frame: dict[str, Any], gdf: Any, label: str) -> tuple[str, str, str, dict]:
    style = frame.get("style") or {}
    if not isinstance(style, dict):
        raise ValueError(f"Layer {label!r} style must be an object.")
    field = str(style.get("column") or style.get("field_name") or style.get("field") or "")
    method = str(style.get("render_method") or style.get("renderer") or (
        "categorized" if field else "none"
    )).lower()
    aliases = {"single": "none", "singlesymbol": "none", "categorizedsymbol": "categorized",
               "graduatedsymbol": "graduated"}
    method = aliases.get(method, method)
    label_field = str(frame.get("label_field") or "")
    for role, selected in (("symbolization", field), ("label", label_field)):
        if selected and selected not in gdf.columns:
            raise ValueError(f"Layer {label!r} {role} field {selected!r} does not exist.")
    if method not in {"none", "categorized", "graduated", "flow"}:
        raise ValueError(f"Layer {label!r} has unsupported renderer {method!r}.")
    if method != "none" and not field:
        raise ValueError(f"Layer {label!r} renderer {method!r} requires a field.")
    return field, method, label_field, style


def _validate_categories(series: pd.Series, style: dict, label: str) -> None:
    for value in series.dropna():
        _scalar(value, name=f"Layer {label!r} category field")
    supplied = style.get("categories")
    if supplied is None:
        return
    if not isinstance(supplied, list) or not supplied:
        raise ValueError(f"Layer {label!r} categories must be a non-empty list.")
    seen: set[Any] = set()
    for item in supplied:
        if isinstance(item, dict) and "value" not in item:
            raise ValueError(f"Layer {label!r} category object requires value.")
        value = _scalar(item.get("value") if isinstance(item, dict) else item,
                        name=f"Layer {label!r} categories")
        if value in seen:
            raise ValueError(f"Layer {label!r} duplicate category {value!r}.")
        seen.add(value)
    palette = style.get("colors")
    if isinstance(palette, list) and len(palette) != len(supplied):
        raise ValueError(f"Layer {label!r} colors must contain one color per category.")
    if "default_color" not in style:
        for value in series.dropna():
            if value not in seen:
                raise ValueError(f"Layer {label!r} category {value!r} is absent from categories.")


def _validate_ranges(style: dict, label: str, series: pd.Series) -> None:
    if "ranges" not in style:
        return
    ranges = style["ranges"]
    if not isinstance(ranges, list) or not ranges:
        raise ValueError(f"Layer {label!r} graduated ranges must be a non-empty list.")
    previous_upper = None
    covered = pd.Series(False, index=series.index)
    for item in ranges:
        if not isinstance(item, dict):
            raise ValueError(f"Layer {label!r} graduated range must be an object.")
        try:
            lower, upper = float(item["lower"]), float(item["upper"])
        except (KeyError, ValueError, TypeError) as exc:
            raise ValueError(f"Layer {label!r} graduated range requires numeric bounds.") from exc
        if not math.isfinite(lower) or not math.isfinite(upper) or lower > upper:
            raise ValueError(f"Layer {label!r} graduated range bounds must be finite and ordered.")
        if previous_upper is not None and (lower < previous_upper or upper <= previous_upper):
            raise ValueError(f"Layer {label!r} graduated ranges must be ordered/non-overlapping.")
        previous_upper = upper
        covered |= series.between(lower, upper, inclusive="both")
    if not covered.loc[series.notna()].all():
        raise ValueError(f"Layer {label!r} graduated ranges leave numeric values unclassified.")


def _field_report(gdf: Any, selected: list[str]) -> tuple[dict[str, dict], bool]:
    names = list(dict.fromkeys([*selected, *gdf.columns]))
    names = [name for name in names if name and name != gdf.geometry.name]
    report = {}
    for name in names[:_FIELD_LIMIT]:
        series = gdf[name]
        converted, reason = _numeric_series(series)
        report[str(name)] = {
            "dtype": str(series.dtype), "numeric_parseable": converted is not None,
            "non_null_count": int(series.notna().sum()),
            "numeric_issue": reason or None,
        }
    return report, len(names) > _FIELD_LIMIT


def _count_option(frame: dict, key: str, default: int | None, label: str) -> int | None:
    value = frame.get(key, default)
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValueError(f"Layer {label!r} {key} must be a non-negative integer.")
    return value


def _vector(frame: dict, label: str, required: bool, coerce_numeric: bool) -> dict:
    import geopandas as gpd

    gdf = frame["gdf"]
    if not isinstance(gdf, gpd.GeoDataFrame):
        raise ValueError(f"Layer {label!r} gdf must be a GeoDataFrame.")
    try:
        geometry = gdf.geometry
    except AttributeError as exc:
        raise ValueError(f"Layer {label!r} has no active geometry column.") from exc
    if not gdf.columns.is_unique:
        raise ValueError(f"Layer {label!r} fields must have unique names.")
    gdf = normalize_frame(gdf)
    frame["gdf"] = gdf
    frame["style"] = normalize_style(gdf, frame.get("style") or {})
    source_crs = _crs(gdf.crs, name=f"Layer {label!r}")
    count = len(gdf)
    expected = _count_option(frame, "expected_feature_count", None, label)
    if expected is not None and count != expected:
        raise ValueError(f"Layer {label!r} expected_feature_count={expected}, actual={count}.")
    minimum = _count_option(frame, "minimum_visible_features", 1, label)
    if minimum is None or (required and minimum < 1):
        raise ValueError(f"Layer {label!r} required minimum_visible_features must be at least 1.")
    if required and count == 0:
        raise ValueError(f"Required layer {label!r} is empty (feature_count=0).")
    source_bounds = None
    if count:
        if geometry.isna().any() or geometry.is_empty.any():
            raise ValueError(f"Layer {label!r} contains null or empty geometries.")
        if not np.isfinite(get_coordinates(gdf.geometry.array)).all():
            raise ValueError(f"Layer {label!r} geometry coordinates must be finite.")
        if not gdf.geometry.is_valid.all():
            raise ValueError(f"Layer {label!r} contains invalid geometries.")
        source_bounds = _bounds(gdf.total_bounds, name=f"Layer {label!r}", positive=False)
        _geographic_bounds(source_bounds, source_crs, name=f"Layer {label!r}")
    field, method, label_field, style = _style_fields(frame, gdf, label)
    converted_fields = []
    if field and method in {"graduated", "flow"} and count:
        converted, reason = _numeric_series(gdf[field])
        if converted is None:
            raise ValueError(f"Layer {label!r} {method} field {field!r} must be numeric: {reason}.")
        if not is_numeric_dtype(gdf[field].dtype):
            if not coerce_numeric:
                raise ValueError(
                    f"Layer {label!r} {method} field {field!r} must have numeric dtype; "
                    "numeric strings require coercion into a copied data source."
                )
            gdf = gdf.copy()
            gdf[field] = converted
            frame["gdf"] = gdf
            converted_fields.append(field)
    if method == "flow" and count and not gdf.geom_type.isin(
        ["LineString", "MultiLineString"]
    ).all():
        raise ValueError(f"Layer {label!r} flow renderer requires line geometries.")
    if field and method == "categorized":
        _validate_categories(gdf[field], style, label)
    if method == "graduated":
        _validate_ranges(style, label, gdf[field])
    fields, truncated = _field_report(gdf, [field, label_field])
    return {
        "kind": "vector", "label": label, "required": required,
        "source_crs": source_crs.to_string(), "bounds": source_bounds,
        "named_bounds": named_bounds(source_bounds) if source_bounds else None,
        "geometry_types": sorted(gdf.geom_type.unique().tolist()) if count else [],
        "feature_count": count, "expected_feature_count": expected,
        "minimum_visible_features": minimum, "fields": fields, "fields_truncated": truncated,
        "converted_fields": converted_fields, "symbolization_field": field,
        "label_field": label_field, "renderer": method,
        "visibility_basis": "geometry_intersection", "_gdf": gdf, "_crs": source_crs,
    }


def _raster(frame: dict, label: str, required: bool) -> dict:
    import rasterio

    try:
        with rasterio.open(frame["raster_path"]) as raster:
            source_crs = _crs(raster.crs, name=f"Raster {label!r}")
            bounds = _bounds(raster.bounds, name=f"Raster {label!r}")
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Cannot read raster {label!r} bounds/CRS: {exc}.") from exc
    _geographic_bounds(bounds, source_crs, name=f"Raster {label!r}")
    return {
        "kind": "raster", "label": label, "required": required,
        "source_crs": source_crs.to_string(), "bounds": bounds,
        "named_bounds": named_bounds(bounds), "geometry_types": [], "feature_count": None,
        "fields": {}, "converted_fields": [], "visibility_basis": "raster_bounds_only",
        "semantic_correctness": "unverified", "_crs": source_crs,
    }


def _automatic_extent(bounds: list[list[float]], crs: CRS, span: float, padding: float) -> list:
    if not bounds:
        raise ValueError("Map has no non-empty layer bounds; supply a declared map_extent.")
    xmin = min(item[0] for item in bounds)
    ymin = min(item[1] for item in bounds)
    xmax = max(item[2] for item in bounds)
    ymax = max(item[3] for item in bounds)
    width, height = max(xmax - xmin, span), max(ymax - ymin, span)
    center_x, center_y = (xmin + xmax) / 2, (ymin + ymax) / 2
    width, height = width * (1 + 2 * padding), height * (1 + 2 * padding)
    result = [center_x - width / 2, center_y - height / 2,
              center_x + width / 2, center_y + height / 2]
    if crs.is_geographic:
        result = [max(-180, result[0]), max(-90, result[1]),
                  min(180, result[2]), min(90, result[3])]
    return _bounds(result, name="Automatic map extent")


def validate_frames(
    frames: list[dict[str, Any]], *, map_crs: str = "", map_extent: list[float] | None = None,
    detail_extent: dict[str, Any] | None = None, minimum_span_geographic: float = 0.02,
    minimum_span_projected: float = 1000, extent_padding: float = 0.05,
    coerce_numeric: bool = True,
) -> dict[str, Any]:
    """Validate prepared map layers; return JSON evidence or raise ``ValueError``.

    Explicit main/detail extents require ``map_crs``. A detail extent accepts
    named bounds and optionally its own ``crs``; it is aligned to display CRS.
    Required vectors need the threshold in at least one map window. Optional
    empty/outside layers remain in the report with warnings. When enabled,
    numeric strings are converted only in a copy assigned to ``frame['gdf']``.
    """
    ensure_proj_data()
    if not isinstance(frames, list) or not frames:
        raise ValueError("Map preflight requires at least one layer.")
    for name, value, allow_zero in (
        ("minimum_span_geographic", minimum_span_geographic, False),
        ("minimum_span_projected", minimum_span_projected, False),
        ("extent_padding", extent_padding, True),
    ):
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value) or (
            value < 0 if allow_zero else value <= 0
        ):
            requirement = "non-negative" if allow_zero else "positive"
            raise ValueError(f"{name} must be finite and {requirement}.")
    if (map_extent is not None or detail_extent is not None) and not map_crs:
        raise ValueError("Explicit map_extent/detail_extent requires a declared map_crs.")
    layers = []
    for index, frame in enumerate(frames):
        if not isinstance(frame, dict):
            raise ValueError(f"Layer {index + 1} frame must be an object.")
        label = str(frame.get("label") or f"Layer {index + 1}")
        required = frame.get("required", True)
        if not isinstance(required, bool):
            raise ValueError(f"Layer {label!r} required must be boolean.")
        if ("gdf" in frame) == ("raster_path" in frame):
            raise ValueError(f"Layer {label!r} requires exactly one gdf or raster_path.")
        layer = _vector(frame, label, required, coerce_numeric) if "gdf" in frame else (
            _raster(frame, label, required)
        )
        layer["role"] = frame.get("role", "map")
        layers.append(layer)
    if map_crs:
        display_crs, crs_source = _crs(map_crs, name="Map"), "explicit_map_crs"
    else:
        mapped = [layer for layer in layers if layer["role"] != "table_only"]
        if not mapped:
            raise ValueError("Map preflight requires a map layer")
        chosen = next((layer for layer in mapped if layer["kind"] == "raster"), mapped[0])
        display_crs = chosen["_crs"]
        crs_source = f"first_{chosen['kind']}_layer:{chosen['label']}"
    all_bounds = []
    for layer in layers:
        if layer["role"] == "table_only":
            layer["display_bounds"] = None
            continue
        label = layer["label"]
        if layer["kind"] == "vector":
            try:
                aligned = layer["_gdf"].to_crs(display_crs)
            except Exception as exc:
                raise ValueError(f"Cannot align layer {label!r} with display CRS.") from exc
            layer["_aligned"] = aligned
            bounds = None if aligned.empty else _bounds(
                aligned.total_bounds, name=f"Layer {label!r} display bounds", positive=False,
            )
            if not aligned.empty and not np.isfinite(get_coordinates(aligned.geometry.array)).all():
                raise ValueError(f"Layer {label!r} reprojection produced non-finite coordinates.")
        else:
            bounds = _transform_bounds(layer["bounds"], layer["_crs"], display_crs, label)
        if bounds is not None:
            _geographic_bounds(bounds, display_crs, name=f"Layer {label!r} display bounds")
            if layer["role"] != "table_only":
                all_bounds.append(bounds)
        layer["display_bounds"] = bounds
    main = _bounds(map_extent, name="map_extent") if map_extent is not None else (
        _automatic_extent(all_bounds, display_crs, minimum_span_geographic if (
            display_crs.is_geographic
        ) else minimum_span_projected, extent_padding)
    )
    _geographic_bounds(main, display_crs, name="map_extent")
    detail = None
    if detail_extent is not None:
        if not isinstance(detail_extent, dict):
            raise ValueError("detail_extent must be an object with named bounds.")
        try:
            detail = _bounds([detail_extent[key] for key in _BOUND_NAMES], name="detail_extent")
        except KeyError as exc:
            raise ValueError("detail_extent requires xmin,ymin,xmax,ymax named bounds.") from exc
        detail_crs = _crs(detail_extent.get("crs") or map_crs, name="Detail map")
        _geographic_bounds(detail, detail_crs, name="detail_extent")
        detail = _transform_bounds(detail, detail_crs, display_crs, "detail_extent")
        detail = _bounds(detail, name="detail_extent display bounds")
    warnings = []
    main_window, detail_window = box(*main), box(*detail) if detail is not None else None
    for layer in layers:
        if layer["role"] == "table_only":
            layer.update(visible_main=False, visible_detail=False,
                         visible_feature_count=0, visibility_basis="table_only")
            continue
        if layer["kind"] == "vector":
            geometry = layer["_aligned"].geometry
            in_main = geometry.intersects(main_window)
            in_detail = geometry.intersects(detail_window) if detail_window else False
            main_count = int(in_main.sum())
            detail_count = int(in_detail.sum()) if detail_window else 0
            union_count = int((in_main | in_detail).sum())
            visible = union_count >= layer["minimum_visible_features"]
            layer.update(visible_main_feature_count=main_count,
                         visible_detail_feature_count=detail_count,
                         visible_feature_count=union_count)
            if layer["feature_count"] == 0:
                warnings.append(f"Optional layer {layer['label']!r} is empty (feature_count=0).")
        else:
            footprint = box(*layer["display_bounds"])
            main_count = int(footprint.intersects(main_window))
            detail_count = int(footprint.intersects(detail_window)) if detail_window else 0
            visible = bool(main_count or detail_count)
            layer.update(visible_main_feature_count=None, visible_detail_feature_count=None)
        layer.update(visible_main=bool(main_count), visible_detail=bool(detail_count))
        if not visible and layer["required"]:
            raise ValueError(
                f"Required layer {layer['label']!r} does not meet visible feature/intersection "
                f"minimum in main or detail extent (main={main_count}, detail={detail_count})."
            )
        if not visible or (not main_count and not detail_count and layer["feature_count"] != 0):
            warnings.append(f"Optional layer {layer['label']!r} is outside map/detail extent.")
    public_layers = [{key: value for key, value in layer.items() if not key.startswith("_")}
                     for layer in layers]
    return {
        "status": "passed", "scope": "map_data_preflight",
        "display_crs": display_crs.to_string(), "display_crs_source": crs_source,
        "map_extent": main, "map_extent_source": "explicit" if map_extent is not None else "auto",
        "named_bounds": named_bounds(main), "imshow_extent": to_imshow_extent(main),
        "detail_extent": detail, "detail_named_bounds": named_bounds(detail) if detail else None,
        "layers": public_layers, "warnings": warnings,
        "checks": {
            "extent_order": "xmin,ymin,xmax,ymax", "extent_finite_positive": "passed",
            "source_crs_declared": "passed", "geographic_coordinate_limits": "passed",
            "geometry_valid_finite_non_empty": "passed", "symbol_label_fields": "passed",
            "numeric_symbol_fields": "passed", "category_values": "passed",
            "expected_feature_counts": "passed", "required_layer_visibility": "passed",
            "visibility_uses": "geometry_intersection; raster_bounds_only",
        },
        "semantic_correctness": "unverified",
    }
