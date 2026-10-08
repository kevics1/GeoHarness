"""Portable, editable map deliveries without requiring QGIS at render time.

The QGS writer follows the QGIS 3.40 project fixtures and XML readers:
https://github.com/qgis/QGIS/tree/release-3_40/tests/testdata/layouts
https://api.qgis.org/api/3.40/qgslayoutitemmap_8cpp_source.html
https://api.qgis.org/api/3.40/qgslayoutitemlegend_8cpp_source.html
https://api.qgis.org/api/3.40/qgslayoutitemscalebar_8cpp_source.html

QGIS gets real data, editable symbology, a canvas and a basic print layout.
It is a second rendering engine, so its text metrics and layout need not be
pixel-identical to matplotlib. ``project.json`` and ``redraw.py`` retain the
native rendering parameters as a complementary reproducible entry point.
"""

from __future__ import annotations

import hashlib
import math
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path, PureWindowsPath
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

from geoharness.config.settings import MapLayout

__all__ = ["export_map_project", "redraw_map_project"]

# Deliberately omit source specs, catalog settings and database connection strings.
_RENDER_KEYS = {
    "column",
    "render_method",
    "color_scheme",
    "n_classes",
    "title",
    "figsize",
    "dpi",
    "add_legend",
    "add_scalebar",
    "add_north_arrow",
}
_LAYER_KEYS = {
    "color", "column", "field_name", "render_method", "color_scheme", "n_classes",
    "categories", "colors", "default_color", "style",
}


class ProjectLayer(BaseModel):
    """One self-contained data snapshot and its effective symbolization."""

    model_config = ConfigDict(extra="forbid")
    id: str
    kind: Literal["vector", "raster"]
    file: str
    label: str
    crs: str
    bounds: list[float]
    options: dict[str, Any] = Field(default_factory=dict)
    style: dict[str, Any] = Field(default_factory=dict)


class MapProject(BaseModel):
    """Versioned user-editable manifest; contains no external source paths."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    renderer: Literal["single", "layers"]
    preview: str
    preview_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    project_crs: str
    extent: list[float]
    extent_locked: bool = False
    layout: MapLayout
    render_kwargs: dict[str, Any]
    layers: list[ProjectLayer]
    qgis_notes: list[str]


def _text(parent: ET.Element, tag: str, value: Any) -> ET.Element:
    child = ET.SubElement(parent, tag)
    child.text = str(value)
    return child


def _extent(parent: ET.Element, bounds: list[float], tag: str = "extent") -> ET.Element:
    child = ET.SubElement(parent, tag)
    for name, value in zip(("xmin", "ymin", "xmax", "ymax"), bounds):
        _text(child, name, format(value, ".17g"))
    return child


def _crs(parent: ET.Element, definition: str) -> None:
    from pyproj import CRS

    crs = CRS.from_user_input(definition)
    spatial = ET.SubElement(parent, "spatialrefsys")
    _text(spatial, "wkt", crs.to_wkt())
    authority = crs.to_authority()
    _text(spatial, "authid", ":".join(authority) if authority else "")
    _text(spatial, "description", crs.name)
    _text(spatial, "geographicflag", str(crs.is_geographic).lower())


def _rgba(color: str) -> str:
    from matplotlib.colors import to_rgba

    return ",".join(str(round(value * 255)) for value in to_rgba(color))


def _symbol(
    parent: ET.Element,
    name: str,
    kind: str,
    color: str,
    layout: MapLayout,
    *,
    width: float | None = None,
    alpha: float = 1,
) -> None:
    """Store QGIS simple symbols, converting matplotlib points to millimetres."""
    symbol_type = {"point": "marker", "line": "line", "polygon": "fill"}[kind]
    symbol = ET.SubElement(
        parent,
        "symbol",
        {
            "name": name,
            "type": symbol_type,
            "alpha": str(alpha),
            "clip_to_extent": "1",
        },
    )
    layer = ET.SubElement(
        symbol,
        "layer",
        {
            "class": {"point": "SimpleMarker", "line": "SimpleLine", "polygon": "SimpleFill"}[kind],
            "enabled": "1",
            "locked": "0",
            "pass": "0",
        },
    )
    line_width = (layout.line_width if width is None else width) * 25.4 / 72
    if kind == "point":
        props = {
            "name": "circle",
            "color": _rgba(color),
            "outline_color": _rgba("#333333"),
            "outline_width": str(0.4 * 25.4 / 72),
            "outline_width_unit": "MM",
            "size": str(math.sqrt(layout.point_size) * 25.4 / 72),
            "size_unit": "MM",
            "scale_method": "diameter",
            "outline_style": "solid",
        }
    elif kind == "line":
        props = {
            "line_color": _rgba(color),
            "line_width": str(line_width),
            "line_width_unit": "MM",
            "line_style": "solid",
            "capstyle": "round",
        }
    else:
        props = {
            "color": _rgba(color),
            "outline_color": _rgba("#5a5a5a"),
            "outline_width": str(line_width),
            "outline_width_unit": "MM",
            "outline_style": "solid",
            "style": "solid",
        }
    for key, value in props.items():
        ET.SubElement(layer, "prop", {"k": key, "v": value})


def _vector_style(gdf: Any, options: dict[str, Any], mode: str) -> tuple[Any, dict[str, Any]]:
    """Derive the QGIS style from the same color/classification code as the PNG."""
    from geoharness.cartography.renderer import (
        _LAYER_KIND_DEFAULTS,
        _classify_values,
        _geom_kind,
        _layer_symbolization,
        _numeric_values,
        _row_colors_and_legend,
    )

    kind = _geom_kind(gdf)
    if mode == "layers":
        options, colors, legend, weights, categories = _layer_symbolization(gdf, options)
        if categories is not None:
            return gdf, {
                "type": "categorizedSymbol", "geometry": kind,
                "column": options["column"], "categories": categories,
            }
        if options["render_method"] == "none":
            color = colors[0] if colors else str(
                options.get("color") or _LAYER_KIND_DEFAULTS[kind][0]
            )
            return gdf, {"type": "singleSymbol", "geometry": kind, "color": color}

    column = str(options.get("column", ""))
    method = str(options.get("render_method", "categorized")).strip().lower()
    scheme = str(options.get("color_scheme", "Set1"))
    n_classes = int(options.get("n_classes", 5))
    if mode != "layers":
        colors, legend, weights = _row_colors_and_legend(gdf, column, method, scheme, n_classes)
    if method == "flow":
        from shapely.geometry import MultiLineString

        from geoharness.cartography.renderer import _geom_lines

        gdf = gdf.copy()
        gdf.geometry = [MultiLineString(_geom_lines(geom)) for geom in gdf.geometry]
        kind = "line"
    style: dict[str, Any] = {"geometry": kind}
    values = _numeric_values(gdf[column]) if column and column in gdf else None
    # The native categorized renderer promotes >30 numeric categories to quantiles.
    graduated = values is not None and (
        method == "graduated" or (method == "categorized" and gdf[column].nunique() > 30)
    )
    if graduated and np.isfinite(values).all():
        _classes, edges = _classify_values(values, n_classes)
        if (
            edges
            and all(np.isfinite(edge).all() for edge in edges)
            and all(edges[i][0] > edges[i - 1][1] for i in range(1, len(edges)))
        ):
            style.update(
                type="graduatedSymbol",
                column=column,
                ranges=[
                    {
                        "lower": edges[i - 1][1] if i else low,
                        "upper": high,
                        "label": legend[i][0],
                        "color": legend[i][1],
                    }
                    for i, (low, high) in enumerate(edges)
                ],
            )
            return gdf, style
    if len(set(colors)) == 1 and weights is None and not (mode == "layers" and column):
        style.update(type="singleSymbol", color=colors[0])
        return gdf, style
    if method in {"categorized", "graduated"} and not graduated and column in gdf:
        categories = []
        for value in pd.unique(gdf[column].dropna()):
            idx = np.flatnonzero((gdf[column] == value).to_numpy())[0]
            scalar = value.item() if hasattr(value, "item") else value
            categories.append({"value": scalar, "label": str(value), "color": colors[idx]})
        categories.append({"value": None, "label": "Other / NULL", "color": "#e4e7ec"})
        style.update(type="categorizedSymbol", column=column, categories=categories)
        return gdf, style
    # Flow colors/widths and tied or non-finite numeric classes cannot always be
    # represented by ordinary numeric ranges. Preserve the actual row styles.
    copied = gdf.copy()
    field = "_geoh_style"
    while field in copied.columns:
        field += "_"
    keys = list(zip(colors, weights or [None] * len(gdf)))
    unique = list(dict.fromkeys(keys))
    index = {key: i for i, key in enumerate(unique)}
    copied[field] = [index[key] for key in keys]
    style.update(
        type="categorizedSymbol",
        column=field,
        categories=[
            {"value": i, "label": f"Style {i + 1}", "color": color, "width": width}
            for i, (color, width) in enumerate(unique)
        ],
    )
    if method == "flow":
        style["geometry"] = "line"
    style["note"] = "Stored row styles; edit the original field and reclassify in QGIS as needed."
    return copied, style


def _qgis_vector_renderer(
    parent: ET.Element, layer: ProjectLayer, layout: MapLayout, mode: str
) -> None:
    style = layer.style
    renderer = ET.SubElement(
        parent,
        "renderer-v2",
        {
            "type": style["type"],
            "attr": style.get("column", ""),
            "symbollevels": "0",
            "enableorderby": "0",
            "forceraster": "0",
        },
    )
    symbols = ET.SubElement(renderer, "symbols")
    alpha = layout.polygon_alpha if style["geometry"] == "polygon" else 1.0
    width = 0.5 if mode == "layers" and style["geometry"] == "polygon" else None
    if style["type"] == "singleSymbol":
        _symbol(symbols, "0", style["geometry"], style["color"], layout, alpha=alpha, width=width)
        return
    items = style.get("ranges", style.get("categories", []))
    entries = ET.SubElement(renderer, "ranges" if "ranges" in style else "categories")
    for i, item in enumerate(items):
        attrs = {"symbol": str(i), "label": item["label"], "render": "true"}
        if "ranges" in style:
            attrs.update(lower=str(item["lower"]), upper=str(item["upper"]))
        else:
            value = item["value"]
            value_type = (
                "NULL"
                if value is None
                else (
                    "bool"
                    if isinstance(value, bool)
                    else "long"
                    if isinstance(value, int)
                    else "double"
                    if isinstance(value, float)
                    else "string"
                )
            )
            attrs.update(value="NULL" if value is None else str(value), type=value_type)
        ET.SubElement(entries, "range" if "ranges" in style else "category", attrs)
        item_width = item.get("width", width)
        if item_width is not None and layer.options.get("render_method") == "flow":
            item_width *= layout.line_width / 1.8
        _symbol(
            symbols, str(i), style["geometry"], item["color"], layout, width=item_width, alpha=alpha
        )


def _snapshot_raster(
    source: Path, destination: Path, cached: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Expand VRT/sidecars into a standalone TIFF; retain no source metadata."""
    import rasterio
    from rasterio.crs import CRS
    from rasterio.transform import from_bounds

    from geoharness.cartography.renderer import _resolve_cmap

    if cached is not None:
        first = np.ma.asarray(cached["array"])
        if cached["crs"] is None:
            raise ValueError("Raster basemap has no CRS.")
        crs = CRS.from_user_input(cached["crs"])
        xmin, xmax, ymin, ymax = cached["extent"]
        bounds = [xmin, ymin, xmax, ymax]
        height, width = first.shape
        dtype = str(first.dtype)
        profile = {
            "driver": "GTiff",
            "width": width,
            "height": height,
            "count": 1,
            "crs": crs,
            "dtype": dtype,
            "compress": "deflate",
            "transform": from_bounds(xmin, ymin, xmax, ymax, width, height),
        }
        with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
            with rasterio.open(destination, "w", **profile) as target:
                target.write(first.data, 1)
                target.write_mask((~np.ma.getmaskarray(first)).astype("uint8") * 255)
    else:
        with rasterio.open(source) as ds:
            if ds.crs is None:
                raise ValueError("Raster basemap has no CRS.")
            profile = {
                key: ds.profile[key]
                for key in ("width", "height", "count", "dtype", "crs", "transform", "nodata")
            }
            profile.update(driver="GTiff", compress="deflate")
            with rasterio.Env(GDAL_TIFF_INTERNAL_MASK=True):
                with rasterio.open(destination, "w", **profile) as target:
                    for _index, window in ds.block_windows(1):
                        target.write(ds.read(window=window), window=window)
                    target.write_mask(ds.dataset_mask())
                    target.colorinterp = ds.colorinterp
            first = ds.read(1, masked=True)
            crs, bounds, dtype = ds.crs, list(ds.bounds), ds.dtypes[0]
    finite = first.compressed()
    finite = finite[np.isfinite(finite)]
    low = float(finite.min()) if finite.size else 0.0
    high = float(finite.max()) if finite.size else 1.0
    cmap = "terrain" if dtype in {"int16", "float32"} else "gray"
    ramp = _resolve_cmap(cmap)
    from matplotlib.colors import to_hex

    stops = [
        {"value": low + (high - low) * value, "color": to_hex(ramp(value))}
        for value in np.linspace(0, 1, 17)
    ]
    return {
        "crs": crs.to_wkt(),
        "bounds": list(bounds),
        "style": {
            "band": 1,
            "opacity": 0.75,
            "min": low,
            "max": high,
            "cmap": cmap,
            "stops": stops,
        },
    }


def _layout_item(
    parent: ET.Element,
    item_type: int,
    identifier: str,
    x: float,
    y: float,
    width: float,
    item_height: float,
    z: int = 1,
    **attributes: str,
) -> ET.Element:
    item_uuid = "{" + str(uuid.uuid4()) + "}"
    attrs = {
        "type": str(item_type),
        "id": identifier,
        "uuid": item_uuid,
        "templateUuid": item_uuid,
        "groupUuid": "",
        "referencePoint": "0",
        "position": f"{x},{y},mm",
        "positionOnPage": f"{x},{y},mm",
        "size": f"{width},{item_height},mm",
        "zValue": str(z),
        "visibility": "1",
        "positionLock": "false",
        "itemRotation": "0",
        "opacity": "1",
        "background": "false",
        "frame": "false",
        "blendMode": "0",
        "excludeFromExports": "0",
        "outlineWidthM": "0.3,mm",
        "frameJoinStyle": "miter",
    }
    attrs.update(attributes)
    item = ET.SubElement(parent, "LayoutItem", attrs)
    ET.SubElement(item, "FrameColor", {"red": "0", "green": "0", "blue": "0", "alpha": "255"})
    ET.SubElement(
        item,
        "BackgroundColor",
        {
            "red": "255",
            "green": "255",
            "blue": "255",
            "alpha": "255",
        },
    )
    ET.SubElement(ET.SubElement(item, "LayoutObject"), "customproperties")
    return item


def _qgis_print_layout(parent: ET.Element, project: MapProject) -> None:
    """Add genuine editable layout objects; never flatten the preview into a map."""
    options = project.layout
    width, height = options.width * 25.4, options.height * 25.4
    layout = ET.SubElement(
        parent,
        "Layout",
        {
            "name": "GeoHarness Map",
            "units": "mm",
            "printResolution": str(options.dpi),
        },
    )
    pages = ET.SubElement(layout, "PageCollection")
    _symbol(pages, "", "polygon", "white", options, width=0)
    _layout_item(pages, 65638, "Page", 0, 0, width, height, z=0, background="true")
    ET.SubElement(pages, "GuideCollection", {"visible": "1"})
    legend_width = min(55.0, width * 0.27)
    map_width, map_height = width - 24 - legend_width, height - 48
    map_item = _layout_item(
        layout,
        65639,
        "Map",
        10,
        26,
        map_width,
        map_height,
        frame="true",
        background="true",
        mapRotation="0",
        keepLayerSet="false",
        followPreset="false",
        drawCanvasItems="false",
    )
    # QGIS restores the map item's height from its extent aspect ratio. Match
    # that ratio to the declared frame so reading XML cannot grow it off-page.
    xmin, ymin, xmax, ymax = project.extent
    center_x, center_y = (xmin + xmax) / 2, (ymin + ymax) / 2
    extent_width, extent_height = xmax - xmin, ymax - ymin
    frame_ratio = map_width / map_height
    if project.extent_locked:
        extent_ratio = extent_width / extent_height
        if extent_ratio > frame_ratio:
            map_height = map_width / extent_ratio
        else:
            map_width = map_height * extent_ratio
        map_item.set("size", f"{map_width},{map_height},mm")
        frame_extent = project.extent
    else:
        extent_width = max(extent_width, extent_height * frame_ratio)
        extent_height = extent_width / frame_ratio
        frame_extent = [
            center_x - extent_width / 2,
            center_y - extent_height / 2,
            center_x + extent_width / 2,
            center_y + extent_height / 2,
        ]
    ET.SubElement(
        map_item, "Extent", dict(zip(("xmin", "ymin", "xmax", "ymax"), map(str, frame_extent)))
    )
    ET.SubElement(map_item, "LayerSet")
    ET.SubElement(map_item, "AtlasMap", {"atlasDriven": "0", "margin": "0.1"})
    title = _layout_item(
        layout,
        65641,
        "Title",
        10,
        8,
        width - 20,
        14,
        labelText=str(project.render_kwargs.get("title", "")),
        htmlState="0",
        marginX="0",
        marginY="0",
        halign="4",
        valign="128",
    )
    title.set("font", f"sans-serif,{options.title_font_size},-1,5,75,0,0,0,0,0")
    legend = _layout_item(
        layout,
        65642,
        "Legend",
        width - legend_width - 10,
        26,
        legend_width,
        map_height,
        map_uuid=map_item.get("uuid", ""),
        title="",
        columnCount="1",
        resizeToContents="1",
        boxSpace="2",
        symbolWidth="7",
        symbolHeight="4",
        lineSpacing="1",
        columnSpace="2",
        fontColor="#000000",
        legendFilterByMap="0",
    )
    styles = ET.SubElement(legend, "styles")
    for style_name in ("title", "group", "subgroup", "symbol", "symbolLabel"):
        style = ET.SubElement(styles, "style", {"name": style_name, "marginBottom": "2"})
        ET.SubElement(
            style,
            "styleFont",
            {
                "description": f"sans-serif,{options.legend_font_size},-1,5,50,0,0,0,0,0",
            },
        )
    _layout_item(
        layout,
        65646,
        "Scale bar",
        10,
        height - 19,
        65,
        12,
        mapUuid=map_item.get("uuid", ""),
        style="Single Box",
        unitType="meters",
        unitLabel="m",
        numSegments="2",
        numSegmentsLeft="0",
        height="2",
        labelBarSpace="2",
        boxContentSpace="1",
        segmentSizeMode="1",
        minBarWidth="35",
        maxBarWidth="55",
        numUnitsPerSegment="1",
        numMapUnitsPerScaleBarUnit="1",
        outlineWidth="0.3",
    )
    north = _layout_item(
        layout,
        65641,
        "North",
        10 + map_width - 14,
        28,
        12,
        17,
        labelText="N\n↑",
        htmlState="0",
        marginX="0",
        marginY="0",
        halign="4",
        valign="128",
        z=2,
    )
    north.set("font", "sans-serif,12,-1,5,75,0,0,0,0,0")
    ET.SubElement(layout, "Atlas", {"enabled": "0"})


def _write_qgis(project: MapProject, destination: Path) -> None:
    root = ET.Element(
        "qgis", {"version": "3.40.0", "projectname": str(project.render_kwargs.get("title", ""))}
    )
    _text(root, "title", project.render_kwargs.get("title", ""))
    ET.SubElement(root, "homePath", {"path": "."})
    _crs(ET.SubElement(root, "projectCrs"), project.project_crs)
    tree = ET.SubElement(root, "layer-tree-group", {"name": "", "checked": "Qt::Checked"})
    # QGIS order is top-to-bottom; native composition inputs are bottom-to-top.
    for layer in reversed(project.layers):
        ET.SubElement(
            tree,
            "layer-tree-layer",
            {
                "id": layer.id,
                "name": layer.label,
                "source": "./" + layer.file,
                "providerKey": "ogr" if layer.kind == "vector" else "gdal",
                "checked": "Qt::Checked",
                "expanded": "1",
            },
        )
    canvas = ET.SubElement(root, "mapcanvas", {"name": "theMapCanvas"})
    _extent(canvas, project.extent)
    _text(canvas, "rotation", 0)
    _crs(ET.SubElement(canvas, "destinationsrs"), project.project_crs)
    project_layers = ET.SubElement(root, "projectlayers")
    for layer in project.layers:
        attrs = {"type": layer.kind, "hasScaleBasedVisibilityFlag": "0"}
        if layer.kind == "vector":
            attrs.update(
                geometry={"point": "Point", "line": "Line", "polygon": "Polygon"}[
                    layer.style["geometry"]
                ],
                readOnly="0",
                labelsEnabled="0",
            )
        node = ET.SubElement(project_layers, "maplayer", attrs)
        _text(node, "id", layer.id)
        _text(node, "layername", layer.label)
        _text(node, "datasource", "./" + layer.file)
        _text(node, "provider", "ogr" if layer.kind == "vector" else "gdal")
        _crs(ET.SubElement(node, "srs"), layer.crs)
        _extent(node, layer.bounds)
        if layer.kind == "vector":
            _qgis_vector_renderer(node, layer, project.layout, project.renderer)
            _text(node, "layerOpacity", 1)
        else:
            style = layer.style
            renderer = ET.SubElement(
                ET.SubElement(node, "pipe"),
                "rasterrenderer",
                {
                    "type": "singlebandpseudocolor",
                    "band": str(style["band"]),
                    "opacity": str(style["opacity"]),
                    "alphaBand": "-1",
                    "classificationMin": str(style["min"]),
                    "classificationMax": str(style["max"]),
                },
            )
            shader = ET.SubElement(
                ET.SubElement(renderer, "rastershader"),
                "colorrampshader",
                {
                    "colorRampType": "INTERPOLATED",
                    "minimumValue": str(style["min"]),
                    "maximumValue": str(style["max"]),
                    "clip": "0",
                    "classificationMode": "1",
                },
            )
            for stop in style["stops"]:
                ET.SubElement(
                    shader,
                    "item",
                    {
                        "value": str(stop["value"]),
                        "label": format(stop["value"], ".4g"),
                        "color": stop["color"],
                        "alpha": "255",
                    },
                )
    order = ET.SubElement(root, "layerorder")
    for layer in reversed(project.layers):
        ET.SubElement(order, "layer", {"id": layer.id})
    properties = ET.SubElement(root, "properties")
    _text(ET.SubElement(properties, "Paths"), "Absolute", "false").set("type", "bool")
    _text(ET.SubElement(properties, "SpatialRefSys"), "ProjectionsEnabled", "1").set(
        "type", "int"
    )
    ET.SubElement(root, "transformContext")
    _qgis_print_layout(ET.SubElement(root, "Layouts"), project)
    ET.indent(root, space="  ")
    destination.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"
    )


def _canvas_extent(
    layers: list[dict[str, Any]], raster: ProjectLayer | None, mode: str
) -> tuple[str, list[float]]:
    crs = raster.crs if raster else layers[0]["gdf"].crs.to_wkt()
    bounds = np.array(
        [layer["gdf"].to_crs(crs).total_bounds for layer in layers if not layer["gdf"].empty]
    )
    if not bounds.size:
        if raster is None:
            raise ValueError("Map project has no finite geometry extent.")
        bounds = np.array([raster.bounds])
    xmin, ymin = bounds[:, :2].min(axis=0)
    xmax, ymax = bounds[:, 2:].max(axis=0)
    if raster:
        rxmin, rymin, rxmax, rymax = raster.bounds
        intersection = max(xmin, rxmin), max(ymin, rymin), min(xmax, rxmax), min(ymax, rymax)
        if intersection[0] < intersection[2] and intersection[1] < intersection[3]:
            xmin, ymin, xmax, ymax = intersection
    if not np.isfinite([xmin, ymin, xmax, ymax]).all():
        raise ValueError("Map project has no finite geometry extent.")
    pad = 0.04 if mode == "layers" else 0.05
    px, py = (xmax - xmin) * pad or 1, (ymax - ymin) * pad or 1
    return crs, [float(xmin - px), float(ymin - py), float(xmax + px), float(ymax + py)]


_REDRAW_SCRIPT = '''"""Redraw the native map after editing project.json or data/*.gpkg."""
from pathlib import Path
import argparse
from geoharness.cartography.project import redraw_map_project

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, help="Output PNG/PDF/SVG/HTML path")
args = parser.parse_args()
print(redraw_map_project(Path(__file__).with_name("project.json"), output_path=args.output))
'''

_README = """# 可编辑地图工程

用 QGIS 3.x 打开 `map.qgs`。图层面板可修改图层顺序；双击图层可调整符号、颜色、
分级和透明度。数据副本在 `data/` 中，矢量为 GeoPackage，栅格为自包含 GeoTIFF。
开启矢量图层编辑可修改几何和属性，请保存图层修改及 QGIS 工程。

通过“项目 → 布局”打开 `GeoHarness Map`，可移动和调整地图框、标题、图例、
比例尺和北向标记，再导出 PNG/PDF。这些是独立可编辑项，预览图不是工程底图。
北向标记是当前零旋转地图的文字标记，旋转地图后需相应调整。

QGIS 与 matplotlib 使用不同渲染引擎，打印布局是可编辑的基础布局，不保证与
原始预览逐像素相同；字体、图例排版、流线样式及栅格色带插值可能略有差异。
打开工程后请核对布局和字体，再导出最终成图。Python 自定义注记不会自动
转换成 QGIS 布局项；需要时参照预览图和原始脚本补充。

`project.json` 保存原生布局及参数。安装 GeoHarness 后，在本目录运行
`python redraw.py --output edited.png` 可从数据副本重绘。
Python 重绘读取 JSON，不读取 QGIS 中修改的样式或布局；QGIS 中的样式修改请从
QGIS 布局导出。QGIS 保存的 GeoPackage 数据修改会被 Python 重绘读取。

移动或分享时请携带整个目录，不能只发送 `.qgs`。所有数据引用均相对于本目录。
"""


def export_map_project(
    project_dir: str | Path,
    *,
    layers: list[dict[str, Any]],
    output_path: str | Path,
    render_kwargs: dict[str, Any],
    layout: MapLayout | None = None,
    extent: list[float] | None = None,
    publication_settings: Any = None,
) -> dict[str, Any]:
    """Snapshot an actual native render into a portable editable project.

    ``layers`` contains the actual GeoDataFrames passed to rendering, ordered
    bottom-to-top. ``render_kwargs['mode']`` is ``single`` or ``layers``.
    The current native composition API supports one raster backdrop; it is
    copied from ``render_kwargs['basemap_raster']``. No source/catalog metadata
    is persisted. ``extent`` optionally records the actual map-frame bounds in
    the project CRS (xmin, ymin, xmax, ymax), without expanding to source bounds.
    Existing project directories are never removed or overwritten.
    """
    mode = str(render_kwargs.get("mode", "layers"))
    if extent is None:
        extent = render_kwargs.get("extent")
    if mode not in {"single", "layers"}:
        raise ValueError("Project mode must be 'single' or 'layers'.")
    if not layers or (mode == "single" and len(layers) != 1):
        raise ValueError("A project needs layers; single mode requires exactly one vector layer.")
    for layer in layers:
        gdf = layer.get("gdf")
        if gdf is None or gdf.crs is None:
            raise ValueError("Project layers need GeoDataFrames with a declared CRS.")
    preview = Path(output_path)
    if not preview.is_file():
        raise ValueError("Render the map before exporting its editable project.")
    destination = Path(project_dir).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Editable project already exists; choose a new project directory.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        (staging / "data").mkdir()
        kwargs = {key: value for key, value in render_kwargs.items() if key in _RENDER_KEYS}
        if mode == "layers":
            kwargs = {
                key: value for key, value in kwargs.items() if key in {"title", "figsize", "dpi"}
            }
        options = layout or MapLayout(
            width=render_kwargs.get("figsize", (10, 8))[0],
            height=render_kwargs.get("figsize", (10, 8))[1],
            dpi=render_kwargs.get("dpi", 150),
        )
        copied_layers: list[ProjectLayer] = []
        raster = None
        if basemap := render_kwargs.get("basemap_raster"):
            if mode != "layers":
                raise ValueError("Raster backdrops require layers mode.")
            snapshot = _snapshot_raster(
                Path(basemap),
                staging / "data" / "basemap.tif",
                cached=(render_kwargs.get("raster_cache") or {}).get("basemap"),
            )
            raster = ProjectLayer(
                id="basemap_" + uuid.uuid4().hex,
                kind="raster",
                file="data/basemap.tif",
                label="Raster basemap",
                **snapshot,
            )
            copied_layers.append(raster)
        for i, layer in enumerate(layers, start=1):
            layer_options = {key: value for key, value in layer.items() if key in _LAYER_KEYS}
            if mode == "single":
                layer_options.update(
                    {key: value for key, value in kwargs.items() if key in _LAYER_KEYS}
                )
            gdf, style = _vector_style(layer["gdf"], layer_options, mode)
            relative = f"data/layer_{i}.gpkg"
            gdf.to_file(staging / relative, layer="features", driver="GPKG", index=False)
            copied_layers.append(
                ProjectLayer(
                    id=f"layer_{i}_" + uuid.uuid4().hex,
                    kind="vector",
                    file=relative,
                    label=str(layer.get("label") or f"Layer {i}"),
                    crs=gdf.crs.to_wkt(),
                    bounds=[float(value) for value in gdf.total_bounds] if not gdf.empty else [],
                    options=layer_options,
                    style=style,
                )
            )
        crs, canvas_extent = _canvas_extent(layers, raster, mode)
        if extent is not None:
            if (
                len(extent) != 4 or not np.isfinite(extent).all()
                or extent[0] >= extent[2] or extent[1] >= extent[3]
            ):
                raise ValueError("Project extent needs finite xmin, ymin, xmax, ymax bounds.")
            canvas_extent = [float(value) for value in extent]
            if mode == "layers":
                kwargs["extent"] = canvas_extent
        preview_name = "preview" + preview.suffix.lower()
        shutil.copyfile(preview, staging / preview_name)
        preview_sha256 = hashlib.sha256((staging / preview_name).read_bytes()).hexdigest()
        project = MapProject(
            renderer=mode,
            preview=preview_name,
            preview_sha256=preview_sha256,
            project_crs=crs,
            extent=canvas_extent,
            extent_locked=extent is not None,
            layout=options,
            render_kwargs=kwargs,
            layers=copied_layers,
            qgis_notes=[
                "Editable basic print layout; QGIS and PNG use different renderers.",
                "Validate text and layout before final QGIS export; "
                "custom annotations may need edits.",
            ],
        )
        (staging / "project.json").write_text(project.model_dump_json(indent=2), encoding="utf-8")
        _write_qgis(project, staging / "map.qgs")
        (staging / "redraw.py").write_text(_REDRAW_SCRIPT, encoding="utf-8")
        (staging / "README.md").write_text(_README, encoding="utf-8")
        from geoharness.files.publication import publish_directory

        publication = publish_directory(staging, destination, settings=publication_settings)
    except Exception as exc:
        if getattr(exc, "diagnostics", None):
            import json

            try:
                (staging / "publication.json").write_text(
                    json.dumps(exc.diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except OSError:
                pass
        elif staging.resolve().is_relative_to(
            destination.parent.resolve()
        ):
            shutil.rmtree(staging)
        raise
    return {
        "project_dir": str(destination),
        "project_file": str(destination / "project.json"),
        "qgis_project": str(destination / "map.qgs"),
        "redraw_script": str(destination / "redraw.py"),
        "project_readme": str(destination / "README.md"),
        "preview_sha256": preview_sha256,
        "publication": publication,
    }


def _project_data_path(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute() or PureWindowsPath(relative).drive:
        raise ValueError("Project data references must be relative paths.")
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("Project data reference escapes the project directory.")
    return result


def redraw_map_project(project_file: str | Path, *, output_path: str | Path | None = None) -> Path:
    """Render from portable snapshots and edited JSON using the native engine."""
    import geopandas as gpd

    from geoharness.cartography.renderer import render_layers, render_map

    manifest = Path(project_file).resolve()
    project = MapProject.model_validate_json(manifest.read_text(encoding="utf-8"))
    root = manifest.parent
    layers = []
    basemap = ""
    for layer in project.layers:
        data = _project_data_path(root, layer.file)
        if layer.kind == "raster":
            if basemap:
                raise ValueError("The native renderer supports one raster backdrop per map.")
            basemap = str(data)
        else:
            layer_options = dict(layer.options)
            if (
                project.renderer == "layers" and layer.style.get("type") == "singleSymbol"
                and not layer_options.get("column") and not layer_options.get("field_name")
                and "categories" not in layer_options and "colors" not in layer_options
                and "style" not in layer_options
            ):
                # Legacy tool projects saved the global classification rule despite
                # drawing uniform layers. Their effective recorded style remains authoritative.
                layer_options.update(render_method="none", color=layer.style["color"])
            layers.append(
                {
                    "gdf": gpd.read_file(data, layer="features"),
                    "label": layer.label,
                    **layer_options,
                }
            )
    kwargs = {key: value for key, value in project.render_kwargs.items() if key in _RENDER_KEYS}
    output = Path(output_path) if output_path is not None else root / "redrawn.png"
    if project.renderer == "single":
        if len(layers) != 1 or basemap:
            raise ValueError("Single mode needs exactly one vector layer and no raster backdrop.")
        if output.suffix.lower() not in {".html", ".htm"}:
            kwargs["layout"] = project.layout
        return render_map(layers[0]["gdf"], output_path=output, **kwargs)
    kwargs = {key: value for key, value in kwargs.items() if key in {"title", "figsize", "dpi"}}
    if "extent" in project.render_kwargs:
        kwargs["extent"] = project.render_kwargs["extent"]
    return render_layers(
        layers, output_path=output, basemap_raster=basemap, layout=project.layout, **kwargs
    )
