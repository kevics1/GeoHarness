"""Explicit map views of immutable source features, without executing expressions."""

from __future__ import annotations

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator


class MapDerivation(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    feature_indices: list[StrictInt] | None = Field(
        default=None, max_length=100000,
        description="Zero-based source row positions, in map order. Omit for all features.",
    )
    keep_fields: list[str] | None = Field(
        default=None, max_length=256,
        description="Source attributes to retain; geometry is always preserved.",
    )
    annotations: dict[str, list[str | int | float | bool | None]] = Field(
        default_factory=dict, max_length=32,
        description="New map-only fields, with one value per selected feature; never statistics.",
    )
    number_field: str = Field(default="", max_length=128, description=(
        "Optional new map-only ordinal field, 1..N in selected source order. "
        "Use as label_field and first label_table field; no hand-written number arrays."
    ))

    @model_validator(mode="after")
    def unique_selection(self):
        if self.feature_indices is not None and (
            len(set(self.feature_indices)) != len(self.feature_indices)
            or any(i < 0 for i in self.feature_indices)
        ):
            raise ValueError("derivation feature_indices must be unique nonnegative positions")
        if self.keep_fields is not None and len(set(self.keep_fields)) != len(self.keep_fields):
            raise ValueError("derivation keep_fields must be unique")
        return self


def derive_frame(frame, specification):
    """Selections cannot move geometries or replace any original business field."""
    spec = MapDerivation.model_validate(specification)
    geometry = frame.geometry.name
    indices = spec.feature_indices if spec.feature_indices is not None else list(range(len(frame)))
    if any(index >= len(frame) for index in indices):
        raise ValueError("derivation feature_indices exceed source feature count")
    fields = spec.keep_fields
    if fields is not None and any(name not in frame.columns or name == geometry for name in fields):
        raise ValueError("derivation keep_fields must name existing attributes, excluding geometry")
    reserved = {str(name).casefold() for name in frame.columns} | {"geometry", "geom", "fid"}
    names = [name.casefold() for name in spec.annotations]
    if len(set(names)) != len(names) or any(not name or name.casefold() in reserved
                                          for name in spec.annotations):
        raise ValueError("derivation annotations cannot overwrite source or geometry fields")
    if spec.number_field and spec.number_field.casefold() in reserved | set(names):
        raise ValueError("derivation number_field cannot overwrite an existing attribute")
    if any(len(values) != len(indices) for values in spec.annotations.values()):
        raise ValueError("derivation annotations require one value per selected feature")
    result = frame.iloc[indices].copy()
    if fields is not None:
        result = result[[*fields, geometry]].copy()
    for name, values in spec.annotations.items():
        series = pd.Series(values, index=result.index)
        if series.dtype == object:
            # GPKG stores mixed scalar/object attributes as text. Normalize the
            # new annotations before freezing so validation compares actual data.
            series = series.map(lambda value: None if pd.isna(value) else str(value))
        result[name] = series
    if spec.number_field:
        result[spec.number_field] = range(1, len(result) + 1)
    return result
