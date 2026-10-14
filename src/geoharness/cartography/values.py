"""Lossless JSON-friendly temporal values on copied map frames."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype

TEMPORAL_FIELDS = "geoharness_temporal_fields"


def temporal_value(value: Any) -> Any:
    if value is None or pd.isna(value):
        return None
    if not isinstance(value, (str, datetime, date, np.datetime64)):
        return value
    try:
        return pd.Timestamp(value).isoformat()
    except (ValueError, TypeError):
        if isinstance(value, str):
            return value
        raise


def normalize_frame(frame):
    """Keep geometry, business IDs and ordinary strings; normalize known dates only."""
    fields = set(frame.attrs.get(TEMPORAL_FIELDS, []))
    for name in frame.columns:
        if name == frame.geometry.name:
            continue
        series = frame[name]
        if is_datetime64_any_dtype(series.dtype) or any(
            isinstance(value, (datetime, date, np.datetime64)) for value in series.dropna()
        ):
            fields.add(name)
    result = frame.copy()
    for name in fields & set(result.columns):
        result[name] = pd.Series(
            [temporal_value(value) for value in result[name]], index=result.index, dtype=object,
        )
    result.attrs[TEMPORAL_FIELDS] = sorted(fields)
    return result


def normalize_style(frame, style: dict) -> dict:
    result = deepcopy(style)
    field = result.get("column") or result.get("field_name") or result.get("field")
    if field in frame.attrs.get(TEMPORAL_FIELDS, []):
        if result.get("categories") is not None:
            result["categories"] = [
                {**item, "value": temporal_value(item["value"])} if isinstance(item, dict)
                else temporal_value(item)
                for item in result["categories"]
            ]
        if isinstance(result.get("colors"), dict):
            colors = {}
            for value, color in result["colors"].items():
                value = temporal_value(value)
                if value in colors:
                    raise ValueError("Datetime colors keys normalize to a duplicate category")
                colors[value] = color
            result["colors"] = colors
    return result


def frame_content(frame, *, generated_ids: list | None = None) -> list[dict]:
    """Compare frames using the same driver interpretation of verified source bytes."""
    import json

    frame = normalize_frame(frame).to_crs(4326)
    # GDAL promotes GeoJSON Feature.id into an attribute when no business id exists.
    # Ignore it only when every value is demonstrably the original Feature.id.
    if generated_ids is not None and "id" in frame.columns:
        if frame["id"].tolist() == generated_ids:
            frame = frame.drop(columns="id")
    features = json.loads(frame.to_json())["features"]
    return [{"geometry": f["geometry"], "properties": f["properties"]} for f in features]
