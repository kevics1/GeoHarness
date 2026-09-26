"""Native cartography renderer — matplotlib (Agg) + folium/Leaflet.

Design principles (Geoharness core):
- **No QGIS.** Pure-Python rendering. No external desktop GIS, no
  ``.qgs``/``.qpt`` templates, no PyQGIS binding.
- **Headless by construction.** The ``Agg`` backend is selected *before*
  ``pyplot`` is imported, so importing this package can never try to open a
  GUI window (critical for TUI subprocesses, servers, and CI).
- **Config-driven symbolization.** Analysis type → render method / colour
  scheme / class count still comes from GeoHarness bridge rules; this module
  only knows how to *execute* a rendered method.

Static output (PNG / PDF / SVG) goes through matplotlib.
Interactive output (HTML) goes through folium, which embeds Leaflet.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Any, Literal

# CRITICAL: pick the non-interactive backend BEFORE importing pyplot.
import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import LineCollection  # noqa: E402
from matplotlib.colors import to_hex  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from geoharness._proj_fix import ensure_proj_data  # noqa: E402

# Reprojection (to_crs) must not inherit a poisoned PROJ database.
ensure_proj_data()

logger = logging.getLogger(__name__)

__all__ = ["render_map", "render_static", "render_interactive"]

RenderMethod = Literal["categorized", "graduated", "flow", "none"]

# ── Colour schemes ────────────────────────────────────────────────────────
# Bridge rules name schemes using QGIS-era vocabulary (RdBu, YlOrRd, Set1).
# Map those names onto matplotlib colormaps so existing config keeps working.
_CONTINUOUS_SCHEMES: dict[str, str] = {
    "RdBu": "RdBu_r",  # diverging: low = blue, high = red (residual convention)
    "RdYlBu": "RdYlBu_r",
    "Spectral": "Spectral_r",
    "YlOrRd": "YlOrRd",
    "YlGnBu": "YlGnBu",
    "Blues": "Blues",
    "Greens": "Greens",
    "Reds": "Reds",
    "Oranges": "Oranges",
    "Purples": "Purples",
    "Viridis": "viridis",
    "Magma": "magma",
    "Cividis": "cividis",
}

_QUALITATIVE_SCHEMES: dict[str, str] = {
    "Set1": "Set1",
    "Set2": "Set2",
    "Set3": "Set3",
    "Paired": "Paired",
    "Dark2": "Dark2",
    "Accent": "Accent",
    "Pastel1": "Pastel1",
    "tab10": "tab10",
    "tab20": "tab20",
}

# Common CJK-capable font families, in preference order. Without one of
# these, Chinese titles/labels render as tofu boxes in static maps.
_CJK_FONTS = (
    "Microsoft YaHei",
    "Microsoft YaHei UI",
    "SimHei",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Zen Hei",
    "PingFang SC",
    "Heiti SC",
    "Arial Unicode MS",
)

_MAX_CATEGORIES = 30
_configured = False


# ── Matplotlib configuration ──────────────────────────────────────────────


def _configure_matplotlib() -> str | None:
    """Select a CJK-capable font once, and harden vector font embedding."""
    global _configured
    if _configured:
        return plt.rcParams.get("font.sans-serif", [None])[0]

    chosen: str | None = None
    try:
        from matplotlib import font_manager

        available = {f.name for f in font_manager.fontManager.ttflist}
        chosen = next((f for f in _CJK_FONTS if f in available), None)
        if chosen is None:
            logger.warning(
                "No CJK font found among %s — Chinese labels may render as boxes. "
                "Install e.g. 'Noto Sans CJK SC' or use SimHei/Microsoft YaHei.",
                ", ".join(_CJK_FONTS),
            )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Font detection failed: %s", exc)

    family = list(plt.rcParams.get("font.sans-serif", []))
    if chosen:
        plt.rcParams["font.sans-serif"] = [chosen, *[f for f in family if f != chosen]]
    plt.rcParams["axes.unicode_minus"] = False
    # Embed glyphs so CJK survives SVG/PDF export on any viewer.
    plt.rcParams["svg.fonttype"] = "path"
    plt.rcParams["pdf.fonttype"] = 42
    _configured = True
    return chosen


# ── Small helpers ─────────────────────────────────────────────────────────


def _resolve_cmap(scheme: str) -> Any:
    """Resolve a scheme name to a matplotlib colormap, with fallback."""
    name = (
        _CONTINUOUS_SCHEMES.get(scheme)
        or _QUALITATIVE_SCHEMES.get(scheme)
        or scheme
        or "viridis"
    )
    try:
        return matplotlib.colormaps[name]
    except KeyError:
        logger.warning("Unknown colour scheme %r; falling back to viridis", scheme)
        return matplotlib.colormaps["viridis"]


def _discrete_colors(scheme: str, k: int, *, categorical: bool) -> list[str]:
    """Return ``k`` hex colours sampled from the scheme.

    ``categorical=True`` prefers a qualitative palette (cycling if the
    palette has fewer entries than ``k``); otherwise the continuous ramp is
    sampled at even intervals.
    """
    k = max(1, int(k))
    if categorical:
        qual_name = _QUALITATIVE_SCHEMES.get(scheme)
        if qual_name:
            cmap = matplotlib.colormaps[qual_name]
            base = [to_hex(cmap(i)) for i in range(cmap.N)]
            return [base[i % len(base)] for i in range(k)]

    cmap = _resolve_cmap(scheme)
    stops = [0.5] if k == 1 else list(np.linspace(0.08, 0.92, k))
    return [to_hex(cmap(float(s))) for s in stops]


def _numeric_values(series: pd.Series) -> np.ndarray | None:
    """Coerce a column to float, or return ``None`` if not numeric."""
    try:
        coerced = pd.to_numeric(series, errors="coerce")
    except Exception:
        return None
    if coerced.notna().sum() == 0:
        return None
    return coerced.to_numpy(dtype="float64")


def _classify_values(
    values: np.ndarray, n_classes: int
) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """Assign 0-based class ids and return per-class value ranges.

    Uses ``mapclassify`` quantiles when available, falling back to a
    rank-based equal-count split that cannot fail on degenerate input.
    """
    y = np.asarray(values, dtype="float64")
    mask = np.isfinite(y)
    k = max(1, int(n_classes))
    classes = np.zeros(y.shape, dtype=int)
    if not mask.any():
        return classes, []

    vals = y[mask]
    uniq = np.unique(vals)
    if uniq.size <= k:
        classes[mask] = np.searchsorted(uniq, vals, side="right") - 1
        k = int(uniq.size)
    else:
        classes[mask] = _quantile_classes(vals, k)

    edges: list[tuple[float, float]] = []
    for i in range(k):
        sel = vals[classes[mask] == i]
        if sel.size:
            edges.append((float(sel.min()), float(sel.max())))
        else:
            edges.append((float("nan"), float("nan")))
    return classes, edges


def _quantile_classes(vals: np.ndarray, k: int) -> np.ndarray:
    """Equal-count class ids for ``vals`` (1-D, finite)."""
    try:
        import mapclassify

        return np.asarray(mapclassify.Quantiles(vals, k=k).yb, dtype=int) - 1
    except Exception as exc:
        logger.warning("mapclassify unavailable (%s); using rank quantiles", exc)

    order = np.argsort(vals, kind="stable")
    ranks = np.empty(vals.size, dtype=int)
    ranks[order] = np.arange(vals.size)
    return np.clip((ranks * k) // vals.size, 0, k - 1)


def _row_colors_and_legend(
    gdf: Any,
    column: str,
    method: str,
    scheme: str,
    n_classes: int,
) -> tuple[list[str], list[tuple[str, str]], list[float] | None]:
    """Compute one hex colour per row plus legend entries.

    Returns ``(hex_colors, legend_items, line_weights)``. ``line_weights``
    is only populated for the flow method.
    """
    n = len(gdf)
    default = "#c9ced6"
    hex_colors = [default] * n
    legend: list[tuple[str, str]] = []

    if method == "none":
        return hex_colors, legend, None

    has_col = bool(column) and column in gdf.columns
    values = _numeric_values(gdf[column]) if has_col else None

    if method == "flow":
        cmap = _resolve_cmap(scheme)
        if values is None:
            return [to_hex(cmap(0.6))] * n, legend, None
        finite = np.isfinite(values)
        lo = float(np.nanmin(values)) if finite.any() else 0.0
        hi = float(np.nanmax(values)) if finite.any() else 1.0
        span = (hi - lo) or 1.0
        norm = np.clip((np.nan_to_num(values, nan=lo) - lo) / span, 0.0, 1.0)
        hex_colors = [to_hex(cmap(float(v))) for v in norm]
        weights = [0.7 + 5.5 * float(v) for v in norm]
        for frac in (0.0, 0.5, 1.0):
            legend.append((f"{lo + frac * span:.3g}", to_hex(cmap(frac))))
        return hex_colors, legend, weights

    if method == "graduated" and values is None:
        logger.warning("Column %r is not numeric; falling back to categorized", column)
        method = "categorized"

    if method == "graduated" and values is not None:
        classes, edges = _classify_values(values, n_classes)
        colors = _discrete_colors(scheme, max(1, len(edges)), categorical=False)
        hex_colors = [colors[int(c)] for c in classes]
        for i, (lo, hi) in enumerate(edges):
            legend.append((f"{lo:.3g} – {hi:.3g}", colors[i]))
        return hex_colors, legend, None

    # categorized (or uniform fallback)
    if not has_col or gdf[column].nunique(dropna=True) <= 1:
        color = _discrete_colors(scheme, 1, categorical=True)[0]
        return [color] * n, legend, None

    cats = list(pd.unique(gdf[column].dropna()))
    if len(cats) > _MAX_CATEGORIES:
        if values is not None:
            logger.warning(
                "categorized column %r has %d classes; using graduated instead",
                column,
                len(cats),
            )
            classes, edges = _classify_values(values, n_classes)
            colors = _discrete_colors(scheme, max(1, len(edges)), categorical=False)
            hex_colors = [colors[int(c)] for c in classes]
            for i, (lo, hi) in enumerate(edges):
                legend.append((f"{lo:.3g} – {hi:.3g}", colors[i]))
            return hex_colors, legend, None
        logger.warning(
            "categorized column %r has %d classes; showing first %d",
            column,
            len(cats),
            _MAX_CATEGORIES,
        )
        cats = cats[:_MAX_CATEGORIES]

    try:
        cats = sorted(cats)
    except TypeError:
        pass  # mixed types — keep appearance order

    colors = _discrete_colors(scheme, len(cats), categorical=True)
    index = {c: i for i, c in enumerate(cats)}
    for i, cat in enumerate(cats):
        legend.append((str(cat), colors[i]))

    for i, value in enumerate(gdf[column]):
        if pd.isna(value) or value not in index:
            hex_colors[i] = "#e4e7ec"
        else:
            hex_colors[i] = colors[index[value]]
    return hex_colors, legend, None


def _geom_lines(geom: Any) -> list[np.ndarray]:
    """Extract drawable line coordinate arrays from any geometry."""
    if geom is None or geom.is_empty:
        return []
    kind = geom.geom_type
    if kind == "LineString":
        return [np.asarray(geom.coords, dtype="float64")]
    if kind == "MultiLineString":
        return [np.asarray(part.coords, dtype="float64") for part in geom.geoms]
    if kind == "Polygon":
        return [np.asarray(geom.exterior.coords, dtype="float64")]
    if kind == "MultiPolygon":
        return [np.asarray(part.exterior.coords, dtype="float64") for part in geom.geoms]
    return []


def _validate_gdf(gdf: Any) -> None:
    if gdf is None or len(gdf) == 0:
        raise ValueError("Cannot render: the dataset contains no features.")
    if getattr(gdf, "geometry", None) is None:
        raise ValueError("Cannot render: the dataset has no geometry column.")
    if gdf.geometry.isna().all():
        raise ValueError("Cannot render: every geometry in the dataset is null.")


def _prepare_output_path(output_path: Any, default_suffix: str) -> Path:
    if output_path in (None, ""):
        raise ValueError("output_path is required.")
    path = Path(output_path).expanduser()
    if not path.suffix:
        path = path.with_suffix(default_suffix)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _to_wgs84(gdf: Any) -> Any:
    """Reproject to EPSG:4326 for web tiling (leaflet is lon/lat)."""
    crs = getattr(gdf, "crs", None)
    if crs is None:
        logger.warning("Dataset has no CRS; assuming EPSG:4326 for interactive output")
        return gdf
    if crs.is_geographic and crs.to_epsg() == 4326:
        return gdf
    try:
        return gdf.to_crs("EPSG:4326")
    except Exception as exc:
        logger.warning("Reprojection to EPSG:4326 failed (%s); using source CRS", exc)
        return gdf


# ── Composition elements (static maps) ────────────────────────────────────


def _nice_distance(meters: float) -> float:
    """Round ``meters`` down to a cartographically pleasant 1/2/5 value."""
    if meters <= 0:
        return 0.0
    exponent = math.floor(math.log10(meters))
    base = 10.0**exponent
    for factor in (5.0, 2.0, 1.0):
        if meters >= factor * base:
            return factor * base
    return base


def _draw_scalebar(ax: Any, gdf: Any) -> None:
    """Draw an approximate scale bar (kilometres)."""
    try:
        minx, miny, maxx, maxy = gdf.total_bounds
        width = float(maxx - minx)
        if width <= 0:
            return
        crs = getattr(gdf, "crs", None)
        if crs is not None and crs.is_geographic:
            mid_lat = (float(miny) + float(maxy)) / 2.0
            width_m = width * 111_320.0 * max(0.05, math.cos(math.radians(mid_lat)))
        else:
            width_m = width
        distance_m = _nice_distance(width_m * 0.22)
        if distance_m <= 0:
            return
        fraction = distance_m / width_m
        x0, y0, height = 0.06, 0.045, 0.012
        ax.add_patch(
            plt.Rectangle(
                (x0, y0),
                fraction,
                height,
                transform=ax.transAxes,
                facecolor="#2b2b2b",
                edgecolor="white",
                linewidth=0.6,
                zorder=6,
            )
        )
        label = (
            f"{distance_m / 1000:.0f} km"
            if distance_m >= 1000
            else f"{distance_m:.0f} m"
        )
        ax.text(
            x0 + fraction / 2.0,
            y0 + height + 0.012,
            label,
            transform=ax.transAxes,
            ha="center",
            va="bottom",
            fontsize=8,
            zorder=6,
        )
    except Exception as exc:  # pragma: no cover - decorative
        logger.debug("Scale bar skipped: %s", exc)


def _draw_north_arrow(ax: Any) -> None:
    try:
        ax.annotate(
            "",
            xy=(0.955, 0.16),
            xytext=(0.955, 0.07),
            xycoords="axes fraction",
            arrowprops={"facecolor": "#2b2b2b", "width": 2.5, "headwidth": 9},
            zorder=6,
        )
        ax.text(
            0.955,
            0.175,
            "N",
            transform=ax.transAxes,
            ha="center",
            va="bottom",
            fontsize=10,
            fontweight="bold",
            zorder=6,
        )
    except Exception as exc:  # pragma: no cover - decorative
        logger.debug("North arrow skipped: %s", exc)


def _attach_legend(ax: Any, legend: list[tuple[str, str]], method: str) -> None:
    if not legend:
        return
    if method == "flow":
        handles = [Line2D([0], [0], color=color, linewidth=2.2, label=label)
                   for label, color in legend]
        title = "Intensity"
    else:
        handles = [Patch(facecolor=color, edgecolor="#5a5a5a", label=label)
                   for label, color in legend]
        title = "Classes"
    leg = ax.legend(
        handles=handles,
        loc="lower left",
        bbox_to_anchor=(0.01, 0.09),
        fontsize=8,
        frameon=True,
        framealpha=0.9,
        title=title,
        title_fontsize=8,
    )
    leg.set_zorder(7)


# ── Static rendering ──────────────────────────────────────────────────────


def render_static(
    gdf: Any,
    *,
    column: str = "",
    render_method: str = "categorized",
    color_scheme: str = "Set1",
    n_classes: int = 5,
    title: str = "",
    output_path: Any,
    figsize: tuple[float, float] = (10.0, 8.0),
    dpi: int = 150,
    add_legend: bool = True,
    add_scalebar: bool = True,
    add_north_arrow: bool = True,
) -> Path:
    """Render a static map to PNG / PDF / SVG and return the written path."""
    _configure_matplotlib()
    _validate_gdf(gdf)
    path = _prepare_output_path(output_path, ".png")

    method = (render_method or "categorized").strip().lower()
    hex_colors, legend, weights = _row_colors_and_legend(
        gdf, column, method, color_scheme, n_classes
    )

    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    ax.set_facecolor("#f7f9fb")

    if method == "flow":
        segments: list[np.ndarray] = []
        seg_colors: list[str] = []
        seg_widths: list[float] = []
        for i, geom in enumerate(gdf.geometry):
            for coords in _geom_lines(geom):
                segments.append(coords)
                seg_colors.append(hex_colors[i])
                seg_widths.append(weights[i] if weights else 1.6)
        if not segments:
            raise ValueError(
                "Cannot render flow: geometries contain no lines or polygon rings."
            )
        ax.add_collection(
            LineCollection(segments, colors=seg_colors, linewidths=seg_widths, alpha=0.88)
        )
        ax.autoscale()
        ax.set_aspect("equal")
    else:
        gdf.plot(
            ax=ax,
            color=hex_colors,
            edgecolor="#5a5a5a",
            linewidth=0.4,
            aspect="equal",
        )

    if title:
        ax.set_title(title, fontsize=14, fontweight="bold", pad=12)

    if add_legend:
        _attach_legend(ax, legend, method)
    if add_scalebar:
        _draw_scalebar(ax, gdf)
    if add_north_arrow:
        _draw_north_arrow(ax)

    ax.set_axis_off()
    fig.tight_layout()
    try:
        fig.savefig(path, bbox_inches="tight", facecolor="white")
    finally:
        plt.close(fig)
    logger.info("Rendered static map: %s (%d bytes)", path, path.stat().st_size)
    return path


# ── Interactive rendering ─────────────────────────────────────────────────


def _folium_legend_html(legend: list[tuple[str, str]], method: str, title: str) -> str:
    if not legend:
        return ""
    heading = "Intensity" if method == "flow" else "Classes"
    rows = "".join(
        f'<div style="display:flex;align-items:center;margin:2px 0;">'
        f'<span style="display:inline-block;width:14px;height:14px;'
        f'background:{color};border:1px solid #666;margin-right:6px;"></span>'
        f'<span>{label}</span></div>'
        for label, color in legend
    )
    caption = f'<div style="font-weight:600;margin-bottom:4px;">{heading}</div>'
    heading_block = (
        f'<div style="font-weight:600;font-size:14px;margin-bottom:6px;">{title}</div>'
        if title
        else ""
    )
    return (
        '<div style="position:fixed;bottom:24px;left:24px;z-index:9999;'
        "background:rgba(255,255,255,0.92);padding:10px 12px;border-radius:6px;"
        "box-shadow:0 1px 6px rgba(0,0,0,0.3);font:12px/1.4 sans-serif;"
        f'max-height:45vh;overflow:auto;">{heading_block}{caption}{rows}</div>'
    )


def render_interactive(
    gdf: Any,
    *,
    column: str = "",
    render_method: str = "categorized",
    color_scheme: str = "Set1",
    n_classes: int = 5,
    title: str = "",
    output_path: Any,
    tiles: str = "OpenStreetMap",
    tooltip_fields: list[str] | None = None,
) -> Path:
    """Render an interactive Leaflet map (HTML) via folium; return the path."""
    import folium

    _validate_gdf(gdf)
    path = _prepare_output_path(output_path, ".html")
    method = (render_method or "categorized").strip().lower()

    view = _to_wgs84(gdf).copy()
    hex_colors, legend, weights = _row_colors_and_legend(
        view, column, method, color_scheme, n_classes
    )
    view["_geoh_color"] = hex_colors
    view["_geoh_weight"] = weights if weights else [1.2] * len(view)

    bounds = view.total_bounds
    center = [(bounds[1] + bounds[3]) / 2.0, (bounds[0] + bounds[2]) / 2.0]
    fmap = folium.Map(location=center, tiles=tiles, control_scale=True)

    if method == "flow":
        def style_fn(feature: dict[str, Any]) -> dict[str, Any]:
            props = feature.get("properties", {})
            color = props.get("_geoh_color", "#3388ff")
            weight = props.get("_geoh_weight", 1.5)
            return {"color": color, "weight": weight, "opacity": 0.85, "fillOpacity": 0.0}
    else:
        def style_fn(feature: dict[str, Any]) -> dict[str, Any]:
            color = feature.get("properties", {}).get("_geoh_color", "#3388ff")
            return {
                "fillColor": color,
                "color": "#555555",
                "weight": 0.8,
                "fillOpacity": 0.65,
            }

    fields = tooltip_fields or ([column] if column else [])
    fields = [f for f in fields if f in view.columns]
    tooltip = folium.GeoJsonTooltip(fields=fields) if fields else None

    folium.GeoJson(
        view.to_json(),
        name="geoharness_layer",
        style_function=style_fn,
        tooltip=tooltip,
        highlight_function=lambda _f: {"weight": 2.5, "fillOpacity": 0.85},
    ).add_to(fmap)

    html = _folium_legend_html(legend, method, title)
    if html:
        fmap.get_root().html.add_child(folium.Element(html))

    fmap.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])
    fmap.save(str(path))
    logger.info("Rendered interactive map: %s (%d bytes)", path, path.stat().st_size)
    return path


# ── Dispatcher ────────────────────────────────────────────────────────────


def render_map(gdf: Any, *, output_path: Any, **kwargs: Any) -> Path:
    """Render by output suffix: ``.html`` → interactive, otherwise static."""
    suffix = Path(str(output_path)).suffix.lower()
    if suffix in {".html", ".htm"}:
        return render_interactive(gdf, output_path=output_path, **kwargs)
    return render_static(gdf, output_path=output_path, **kwargs)


# ── Multi-layer composition ───────────────────────────────────────────────
#
# Fire-incident and thematic maps routinely stack several layers (base
# raster, boundaries, roads, water, incident points). The single-GDF API
# above cannot express that, and models trying to fake it by passing a
# directory path or wildcard burned many turns. ``render_layers`` composes
# an ordered list of styled layers onto one figure/HTML map.


def _layer_geojson_style(
    method: str, color: str
) -> "dict[str, Any]":
    """Per-layer folium style: distinct from per-feature symbolization."""
    if method == "flow":
        return {"color": color, "weight": 2.0, "opacity": 0.9, "fillOpacity": 0.0}
    return {
        "fillColor": color,
        "color": "#5a5a5a",
        "weight": 0.7,
        "fillOpacity": 0.6,
    }


def _geom_kind(gdf: Any) -> str:
    """Coarse geometry kind for default styling: point/line/polygon."""
    try:
        kinds = {str(g).split(" ")[0].lower() for g in gdf.geom_type.dropna()}
    except Exception:  # noqa: BLE001
        return "polygon"
    if kinds <= {"point", "multipoint"}:
        return "point"
    if kinds <= {"linestring", "multilinestring"}:
        return "line"
    return "polygon"


_LAYER_KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    # kind → (static colour, folium colour, fillOpacity, linewidth)
    "polygon": ("#4c78a8", "#4c78a8", 0.55, 0.7),
    "line": ("#d62728", "#d62728", 0.0, 1.8),
    "point": ("#e4572e", "#e4572e", 0.9, 1.0),
}


def render_layers(
    layers: list[dict[str, Any]],
    *,
    title: str = "",
    output_path: Any,
    figsize: tuple[float, float] = (10.0, 8.0),
    dpi: int = 150,
    basemap_raster: str = "",
) -> Path:
    """Compose several layers onto ONE output map.

    Args:
        layers: Ordered bottom-to-top. Each item::

                {
                  "gdf": GeoDataFrame,          # required
                  "label": "受威胁居民点",        # legend label (required)
                  "color": "#e4572e",           # optional, kind default else
                  "render_method": "none",      # kept simple: uniform layer colour
                  "column": "",                 # ignored for now (uniform colour)
                }

        title: Map title.
        output_path: ``.png``/``.pdf``/``.svg`` (static) or ``.html`` (interactive).
        basemap_raster: Optional raster file path drawn beneath all layers.

    Returns:
        The written output path.

    Raises:
        ValueError: If no layers are given, a layer lacks ``gdf``/``label``,
            or a layer GeoDataFrame is invalid.
    """
    if not layers:
        raise ValueError("render_layers needs at least one layer.")
    prepared: list[tuple[str, Any, str, str]] = []
    for idx, layer in enumerate(layers):
        gdf = layer.get("gdf")
        label = str(layer.get("label") or f"layer_{idx + 1}")
        if gdf is None:
            raise ValueError(f"Layer {label!r} is missing 'gdf'.")
        _validate_gdf(gdf)
        kind = _geom_kind(gdf)
        static_color, _, _, _ = _LAYER_KIND_DEFAULTS.get(
            kind, _LAYER_KIND_DEFAULTS["polygon"]
        )
        color = str(layer.get("color") or static_color)
        prepared.append((label, gdf, color, kind))

    path = _prepare_output_path(output_path, ".png")
    is_html = path.suffix.lower() in {".html", ".htm"}

    # Reproject every layer to WGS84 for folium; for static maps keep the
    # first layer's CRS so scalebar maths stays correct.
    if is_html:
        prepared = [
            (label, _to_wgs84(gdf), color, kind)
            for label, gdf, color, kind in prepared
        ]

    if is_html:
        return _render_layers_interactive(prepared, title, path)
    return _render_layers_static(
        prepared, title, path, figsize, dpi, basemap_raster
    )


def _render_layers_static(
    prepared: list[tuple[str, Any, str, str]],
    title: str,
    path: Path,
    figsize: tuple[float, float],
    dpi: int,
    basemap_raster: str,
) -> Path:
    _configure_matplotlib()
    fig, ax = plt.subplots(figsize=figsize, dpi=dpi)
    ax.set_facecolor("#f7f9fb")

    # ── CRS reconciliation ────────────────────────────────────────
    # A raster basemap is usually projected (e.g. UTM metres) while vector
    # layers are usually EPSG:4326 degrees. Plotting both on one axis squeezes
    # everything except the basemap into an invisible speck. When a basemap is
    # present, reproject every vector layer into the raster's CRS and use the
    # raster's extent as the canvas.
    raster_crs = None
    if basemap_raster:
        import rasterio
        from rasterio.plot import plotting_extent

        with rasterio.open(basemap_raster) as ds:
            arr = ds.read(1, masked=True)
            extent = plotting_extent(ds)
            raster_crs = ds.crs
            ax.imshow(
                arr,
                extent=extent,
                cmap="terrain" if ds.dtypes[0] in ("int16", "float32") else "gray",
                alpha=0.75,
                zorder=0,
            )
        if raster_crs is not None:
            # geopandas.to_crs accepts EPSG integers / WKT text, NOT rasterio
            # CRS objects — pass a string it understands.
            try:
                target_epsg = raster_crs.to_epsg()
            except Exception:  # noqa: BLE001
                target_epsg = None
            target_crs: Any = target_epsg or raster_crs.to_wkt()
            reprojected: list[tuple[str, Any, str, str]] = []
            for label, gdf, color, kind in prepared:
                try:
                    if gdf.crs is not None:
                        gdf = gdf.to_crs(target_crs)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Could not reproject layer %r to raster CRS: %s",
                        label,
                        exc,
                    )
                reprojected.append((label, gdf, color, kind))
            prepared = reprojected

    for label, gdf, color, kind in prepared:
        if kind == "line":
            gdf.plot(ax=ax, color=color, linewidth=1.8, aspect="equal", zorder=2)
        elif kind == "point":
            gdf.plot(
                ax=ax, color=color, markersize=18, edgecolor="#333333",
                linewidth=0.4, aspect="equal", zorder=3,
            )
        else:
            gdf.plot(
                ax=ax, color=color, edgecolor="#5a5a5a", linewidth=0.5,
                alpha=0.75, aspect="equal", zorder=1,
            )

    # Extent = union of all vector layers (plus raster when present) so the
    # figure never shows a blank canvas around invisible data.
    try:
        import pandas as _pd

        frames = [gdf[["geometry"]] for _label, gdf, _c, _k in prepared]
        stacked = _pd.concat(frames, ignore_index=True)
        union = type(prepared[0][1])(stacked, geometry="geometry")
        minx, miny, maxx, maxy = union.total_bounds
        if basemap_raster and raster_crs is not None:
            # Clip the canvas to the raster footprint: vectors (e.g. rivers)
            # can extend far beyond it, and letting them drive the extent
            # shrinks the map of interest into a corner. NB: rasterio's
            # plotting_extent returns (xmin, xmax, ymin, ymax).
            ext_xmin, ext_xmax, ext_ymin, ext_ymax = extent
            minx, miny = max(minx, ext_xmin), max(miny, ext_ymin)
            maxx, maxy = min(maxx, ext_xmax), min(maxy, ext_ymax)
            if maxx <= minx or maxy <= miny:  # no overlap — keep vector extent
                minx, miny, maxx, maxy = union.total_bounds
        pad_x = (maxx - minx) * 0.04 or 1.0
        pad_y = (maxy - miny) * 0.04 or 1.0
        ax.set_xlim(minx - pad_x, maxx + pad_x)
        ax.set_ylim(miny - pad_y, maxy + pad_y)
        union_for_scale = union
    except Exception as exc:  # noqa: BLE001 — extent is best-effort
        logger.debug("Extent union failed (%s); falling back to autoscale", exc)
        ax.autoscale()
        union_for_scale = prepared[0][1]

    # One merged legend from the layer labels. NB: Line2D needs an explicit
    # `color` — without it the line is drawn in matplotlib's default blue,
    # disagreeing with what the layer actually rendered as.
    handles = [
        Line2D(
            [0], [0],
            color=color if kind == "line" else "none",
            marker="o" if kind == "point" else ("s" if kind == "polygon" else None),
            linestyle="-" if kind == "line" else "None",
            linewidth=2.0 if kind == "line" else 0,
            markerfacecolor=color,
            markeredgecolor="#333333" if kind == "point" else color,
            markersize=9,
            label=label,
        )
        for label, _gdf, color, kind in prepared
    ]
    if handles:
        ax.legend(
            handles=handles, loc="upper right", fontsize=9,
            framealpha=0.92, title="图层",
        )

    # Title + scalebar (uses the union extent computed above).
    if title:
        ax.set_title(title, fontsize=14, fontweight="bold", pad=12)
    try:
        _draw_scalebar(ax, union_for_scale)
    except Exception:  # noqa: BLE001 — scalebar is cosmetic
        _draw_scalebar(ax, prepared[0][1])
    _draw_north_arrow(ax)

    ax.set_axis_off()
    fig.tight_layout()
    try:
        fig.savefig(path, bbox_inches="tight", facecolor="white")
    finally:
        plt.close(fig)
    logger.info("Rendered %d-layer static map: %s", len(prepared), path)
    return path


def _render_layers_interactive(
    prepared: list[tuple[str, Any, str, str]],
    title: str,
    path: Path,
) -> Path:
    import folium

    # Union of every layer's bounds, tracked as scalars (nested-list min/max
    # on numpy arrays is ambiguous and crashes folium fit_bounds).
    xs0: list[float] = []
    ys0: list[float] = []
    xs1: list[float] = []
    ys1: list[float] = []
    for label, gdf, color, _kind in prepared:
        b = gdf.total_bounds  # [minx, miny, maxx, maxy]
        xs0.append(float(b[0]))
        ys0.append(float(b[1]))
        xs1.append(float(b[2]))
        ys1.append(float(b[3]))
    minx, miny = min(xs0), min(ys0)
    maxx, maxy = max(xs1), max(ys1)
    fmap = folium.Map(
        location=[(miny + maxy) / 2.0, (minx + maxx) / 2.0],
        tiles="OpenStreetMap",
        control_scale=True,
    )
    for label, gdf, color, _kind in prepared:
        gj = folium.GeoJson(
            gdf.to_json(),
            name=label,
            style_function=lambda _f, _c=color: _layer_geojson_style(
                "categorized", _c
            ),
            highlight_function=lambda _f: {"weight": 2.5, "fillOpacity": 0.85},
        )
        gj.add_to(fmap)

    folium.LayerControl(collapsed=False).add_to(fmap)
    if title:
        fmap.get_root().html.add_child(
            folium.Element(
                f'<div style="position:fixed;top:12px;left:50%;transform:'
                f'translateX(-50%);z-index:9999;background:rgba(255,255,255,0.9);'
                f'padding:6px 14px;border-radius:6px;font:600 15px sans-serif;">'
                f"{title}</div>"
            )
        )
    fmap.fit_bounds([[miny, minx], [maxy, maxx]])
    fmap.save(str(path))
    logger.info("Rendered %d-layer interactive map: %s", len(prepared), path)
    return path
