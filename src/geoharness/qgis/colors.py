"""Normalize CSS rgba input to Qt's explicit ARGB format before native execution."""

from __future__ import annotations

import math
import re


def normalize_color(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("Color must be a string such as #RRGGBB")
    value = value.strip()
    if value.lower().startswith("rgba("):
        match = re.fullmatch(r"rgba\(\s*([^,]+),([^,]+),([^,]+),([^,]+)\s*\)", value, re.I)
        if match is None:
            raise ValueError("rgba requires red,green,blue in 0..255 and alpha in 0..1")
        channels = [float(part) for part in match.groups()]
        if (not all(math.isfinite(x) for x in channels)
                or any(not x.is_integer() or not 0 <= x <= 255 for x in channels[:3])
                or not 0 <= channels[3] <= 1):
            raise ValueError("rgba requires red,green,blue in 0..255 and alpha in 0..1")
        red, green, blue = (int(x) for x in channels[:3])
        return f"#{round(channels[3] * 255):02x}{red:02x}{green:02x}{blue:02x}"
    if re.fullmatch(r"#[0-9a-fA-F]{3}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}|[A-Za-z]+", value):
        return value
    raise ValueError("Use #RRGGBB with opacity, a Qt color name, or valid rgba(r,g,b,a)")
