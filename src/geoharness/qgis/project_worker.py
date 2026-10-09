"""Project operations loaded only by the standalone, native QGIS worker.

QGS XML is inspected before loading to reject external providers and unsafe links.
Projects and styles are written by QGIS itself, never synthesized as XML here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

_VECTOR_SUFFIXES = {".gpkg", ".shp", ".geojson", ".json", ".fgb"}
_RASTER_SUFFIXES = {".tif", ".tiff", ".img", ".asc", ".dem", ".png", ".jpg", ".jpeg"}
_ASSET_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".webp"}
_ASSET_KEYS = {"picturepath", "svgfile", "imagefile", "name", "file", "path"}
_FONT_SUFFIXES = {".ttf", ".ttc", ".otf"}
_FONT_PROPERTIES = {"geoharness/font", "geoharness/label_font"}


def _local_source(source: str, context: Any, *, base: Path | None = None) -> tuple[Path, str]:
    """Accept only file paths and the two non-executable OGR sublayer selectors."""
    if re.search(r"(?i)(://|/vsi|authcfg\s*=|password\s*=|dbname\s*=)", source):
        raise ValueError("QGIS projects accept local files only; remote/credential sources denied")
    path_value, *options = source.split("|")
    for option in options:
        if not re.fullmatch(r"(?:layername=[^|]+|layerid=\d+)", option):
            raise ValueError("Only layername/layerid selectors are supported for local data")
    path = Path(path_value)
    if base is not None and not path.is_absolute():
        path = base / path
    checked = context.read_path(str(path))
    if checked.suffix.lower() not in _VECTOR_SUFFIXES | _RASTER_SUFFIXES:
        raise ValueError(
            "Unsupported local source format (VRT/remote indirection is not supported)"
        )
    return checked, "|".join(options)


def _project_xml(path: Path) -> ET.Element:
    if path.suffix.lower() == ".qgz":
        with zipfile.ZipFile(path) as archive:
            members = [
                item for item in archive.infolist() if item.filename.lower().endswith(".qgs")
            ]
            if len(members) != 1 or members[0].file_size > 32 * 1024 * 1024:
                raise ValueError("QGZ must contain one bounded QGS document")
            content = archive.read(members[0])
    elif path.suffix.lower() == ".qgs":
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("QGS exceeds the supported document size")
        content = path.read_bytes()
    else:
        raise ValueError("project_path must be a local .qgs or .qgz")
    if b"<!ENTITY" in content.upper():
        raise ValueError("XML entities are not supported in project files")
    return ET.fromstring(content)


def _builtin_svg_roots() -> list[Path]:
    """Use QGIS search paths, admitting only the runtime's own SVG directories."""
    from qgis.core import QgsApplication

    official = {
        (Path(value) / "svg").resolve()
        for value in (QgsApplication.prefixPath(), QgsApplication.pkgDataPath())
        if value
    }
    return list(
        dict.fromkeys(
            Path(value).resolve()
            for value in QgsApplication.svgPaths()
            if Path(value).resolve() in official
        )
    )


def _resolve_asset(value: str, project_path: Path, context: Any) -> Path:
    """Project-local resources retain normal read guards; builtins allow SVGs only."""
    if re.search(r"(?i)(://|/vsi)", value) or value.startswith(("\\\\", "//")):
        raise ValueError("Remote layout/symbol resources are not supported")
    asset = Path(value)
    local = asset if asset.is_absolute() else project_path.parent / asset
    try:
        return context.read_path(str(local))
    except (ValueError, PermissionError):
        if asset.suffix.lower() != ".svg":
            raise
        for root in _builtin_svg_roots():
            candidate = (asset if asset.is_absolute() else root / asset).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return candidate
        raise


def _preflight_project(
    path: Path,
    context: Any,
    asset_sources: dict[str, Path] | None = None,
) -> list[Path]:
    """Check linked files before QgsProject.read can open providers or pictures."""
    root = _project_xml(path)
    if root.tag != "qgis":
        raise ValueError("Expected a QGIS project document")
    if any(item.get("embedded") == "1" for item in root.iter()):
        raise ValueError("Embedded external projects are not supported")
    for layer in root.findall("./projectlayers/maplayer"):
        provider = (layer.findtext("provider") or "").strip()
        kind = layer.get("type")
        if (kind, provider) not in {("vector", "ogr"), ("raster", "gdal")}:
            raise ValueError("Only local OGR vector and GDAL raster project layers are supported")
        _local_source(layer.findtext("datasource") or "", context, base=path.parent)
    assets: list[Path] = []
    for font in _project_font_resources(root, path, context).values():
        if font not in assets:
            assets.append(font)
    for item in root.iter():
        if item.tag.lower() in {"pythonmacros", "pythonfunctions", "pythoncode"}:
            if "".join(item.itertext()).strip():
                raise ValueError("Project Python macros/functions are not supported")
        for key, value in item.attrib.items():
            if key == "value":
                semantic_key = item.get("name", "")
            elif key == "v" and item.tag.lower() == "prop":
                semantic_key = item.get("k", "")
            elif key in {"name", "k"} and item.tag.lower() in {"option", "prop"}:
                continue  # These attributes are property keys, not resource values.
            else:
                semantic_key = key
            if semantic_key.lower() not in _ASSET_KEYS or not value:
                continue
            if re.search(r"(?i)(://|/vsi)", value):
                raise ValueError("Remote layout/symbol resources are not supported")
            if Path(value).suffix.lower() in _ASSET_SUFFIXES:
                asset = _resolve_asset(value, path, context)
                if asset_sources is not None:
                    asset_sources[value] = asset
                if asset not in assets:
                    assets.append(asset)
        if item.get("htmlState") == "1" or item.get("htmlMode") == "1":
            content = item.get("labelText", "") + "".join(item.itertext())
            if re.search(r"(?i)<\s*(?:img|iframe|object)|url\s*\(", content):
                raise ValueError("HTML labels with external resources are not supported")
    return assets


def _project_font_resources(root: ET.Element, project_path: Path, context: Any) -> dict[str, Path]:
    """Only declared packaged font resources are admitted, before native project read."""
    resources = {}
    for item in root.iter():
        if item.get("name") not in _FONT_PROPERTIES or "value" not in item.attrib:
            continue
        info = json.loads(item.attrib["value"])
        if not isinstance(info, dict) or not info.get("packaged"):
            continue
        value = info.get("file", "")
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or (
            relative.suffix.lower() not in _FONT_SUFFIXES
        ):
            raise ValueError("Packaged font resources must be project-relative TTF, TTC or OTF")
        font = context.read_path(str(project_path.parent / relative))
        if not font.is_relative_to(project_path.parent.resolve()):
            raise PermissionError("Packaged font must remain inside its project directory")
        expected = info.get("sha256")
        if expected and hashlib.sha256(font.read_bytes()).hexdigest() != expected:
            raise ValueError("Packaged font checksum does not match its saved metadata")
        resources[value] = font
    return resources


def _register_project_fonts(project_path: Path, context: Any) -> None:
    from qgis.PyQt.QtGui import QFontDatabase

    database = QFontDatabase()
    for path in _project_font_resources(_project_xml(project_path), project_path, context).values():
        if database.addApplicationFont(str(path)) < 0:
            raise ValueError(f"QGIS could not register packaged font: {path.name}")


def _snapshot_font(path: Path, context: Any) -> tuple[Path, str]:
    """Snapshot only explicitly selected fonts; a content hash deduplicates layers/layouts."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    relative = f"assets/fonts/{digest}{path.suffix.lower()}"
    target = context.run_dir / relative
    if not target.exists():
        target = Path(context.output_path(None, relative))
        shutil.copy2(path, target)
    elif hashlib.sha256(context.read_path(str(target)).read_bytes()).hexdigest() != digest:
        raise ValueError("Existing packaged font does not match its resource hash")
    return target, digest


def _restore_font_metadata(
    project: Any, source_project: Path, context: Any, *, package: bool
) -> None:
    """Record actual font registration and relocate resource metadata for new packages."""
    from qgis.PyQt.QtGui import QFont, QRawFont

    owners = [*project.mapLayers().values(), *project.layoutManager().printLayouts()]
    resources = _project_font_resources(_project_xml(source_project), source_project, context)
    for owner in owners:
        for key in _FONT_PROPERTIES:
            value = owner.customProperty(key, "")
            if not value:
                continue
            info = json.loads(value)
            if not info.get("packaged"):
                continue
            path = resources[info["file"]]
            raw = QRawFont.fromFont(QFont(info["family"]))
            loaded = raw.isValid() and all(raw.supportsCharacter(char) for char in "地图说明")
            if not loaded:
                raise ValueError("Saved project's packaged Chinese font could not be restored")
            if package:
                path, digest = _snapshot_font(path, context)
                info.update(file=path.relative_to(context.run_dir).as_posix(), sha256=digest)
            info.update(loaded=loaded, registered=True)
            owner.setCustomProperty(key, json.dumps(info))


def _color(value: str) -> Any:
    from qgis.PyQt.QtGui import QColor

    color = QColor(value)
    if not color.isValid():
        raise ValueError(f"Invalid symbol color: {value}")
    return color


def _font(spec: dict[str, Any], context: Any) -> tuple[Any, dict[str, Any]]:
    """Load configured fonts, or the OS's ordinary static CJK font collection."""
    from qgis.PyQt.QtGui import QFont, QFontDatabase, QRawFont

    database = QFontDatabase()
    requested = spec.get("font_family", "Microsoft YaHei")
    configured = spec.get("font_file")
    font_file = ""
    packaged = False
    digest = ""
    if configured:
        path = context.read_path(configured)
        if path.suffix.lower() not in _FONT_SUFFIXES:
            raise ValueError("font_file must be a local TTF, TTC or OTF font")
        index = database.addApplicationFont(str(path))
        families = database.applicationFontFamilies(index) if index >= 0 else []
        if not families:
            raise ValueError(f"QGIS could not load configured font: {path.name}")
        requested = requested if requested in families else families[0]
        path, digest = _snapshot_font(path, context)
        font_file = path.relative_to(context.run_dir).as_posix()
        packaged = True
    else:
        # Native worker bootstrap registers system fonts; these bounded candidates
        # also make the selected file verifiable without recording a host path in code.
        system_root = os.environ.get("WINDIR", os.environ.get("SystemRoot", ""))
        if system_root:
            root = Path(system_root) / "Fonts"
            for filename in ("msyh.ttc", "simhei.ttf", "simsun.ttc"):
                path = root / filename
                if not path.is_file():
                    continue
                index = database.addApplicationFont(str(path))
                families = database.applicationFontFamilies(index) if index >= 0 else []
                if requested in families or requested not in database.families():
                    if families:
                        requested = requested if requested in families else families[0]
                        font_file = str(path)
                        break
    available = database.families()
    if requested not in available:
        requested = next(
            (name for name in ("Microsoft YaHei", "微软雅黑", "SimHei", "Noto Sans CJK SC")
             if name in available),
            requested,
        )
    font = QFont(requested)
    raw = QRawFont.fromFont(font)
    loaded = raw.isValid() and all(raw.supportsCharacter(char) for char in "地图说明")
    if not loaded:
        raise ValueError("QGIS requires a loaded Chinese font; configure font_file/font_family")
    info = {
        "family": font.family(), "file": font_file, "loaded": loaded,
        "packaged": packaged, "registered": True,
    }
    if digest:
        info["sha256"] = digest
    return font, info


def _text_format(font: Any, size: float, color: str = "#222222") -> Any:
    from qgis.core import Qgis, QgsTextFormat

    result = QgsTextFormat()
    result.setFont(font)
    result.setSize(size)
    result.setSizeUnit(Qgis.RenderUnit.Points)
    result.setColor(_color(color))
    return result


def _symbol(layer: Any, style: dict[str, Any], entry: dict[str, Any]) -> Any:
    from qgis.core import Qgis, QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol, QgsSymbol

    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    if symbol is None:
        raise ValueError("Project maps require a spatial vector layer")
    symbol.setColor(_color(entry.get("color", style.get("color", "#4c78a8"))))
    width = entry.get("width_mm", style.get("width_mm", 0.3))
    size = entry.get("size_mm", style.get("size_mm", 3))
    if isinstance(symbol, QgsMarkerSymbol):
        symbol.setSize(size)
        symbol.setSizeUnit(Qgis.RenderUnit.Millimeters)
    elif isinstance(symbol, QgsLineSymbol):
        symbol.setWidth(width)
        symbol.setWidthUnit(Qgis.RenderUnit.Millimeters)
    for symbol_layer in symbol.symbolLayers():
        if isinstance(symbol, (QgsMarkerSymbol, QgsFillSymbol)):
            symbol_layer.setStrokeColor(_color(style.get("outline_color", "#ffffff")))
            symbol_layer.setStrokeWidth(width)
            symbol_layer.setStrokeWidthUnit(Qgis.RenderUnit.Millimeters)
    return symbol


def _symbol_metadata(symbol: Any) -> dict[str, Any]:
    from qgis.core import QgsLineSymbol, QgsMarkerSymbol

    result = {"color": symbol.color().name()}
    if isinstance(symbol, QgsMarkerSymbol):
        result["size_mm"] = symbol.size()
    if isinstance(symbol, QgsLineSymbol):
        result["width_mm"] = symbol.width()
    else:
        stroke_width = getattr(symbol.symbolLayer(0), "strokeWidth", None)
        if callable(stroke_width):
            result["width_mm"] = stroke_width()
    return result


def _style_vector(layer: Any, spec: dict[str, Any], context: Any) -> None:
    from qgis.core import (
        Qgis,
        QgsCategorizedSymbolRenderer,
        QgsGraduatedSymbolRenderer,
        QgsPalLayerSettings,
        QgsRendererCategory,
        QgsRendererRange,
        QgsSingleSymbolRenderer,
        QgsTextBufferSettings,
        QgsVectorLayerSimpleLabeling,
    )

    style = spec.get("style") or {}
    renderer = style.get("renderer", "single")
    if renderer in {"categorized", "graduated"}:
        field = style.get("field", "")
        if layer.fields().indexFromName(field) < 0:
            raise ValueError(f"Renderer field does not exist: {field}")
    if renderer == "categorized":
        categories = []
        for category in style.get("categories", []):
            symbol = _symbol(layer, style, category)
            value = category["value"]
            label = category.get("label") or ("NULL" if value is None else str(value))
            categories.append(QgsRendererCategory(value, symbol, label))
        if not categories:
            raise ValueError("Categorized styling requires explicit categories/colors")
        layer.setRenderer(QgsCategorizedSymbolRenderer(field, categories))
    elif renderer == "graduated":
        ranges = [
            QgsRendererRange(
                item["lower"], item["upper"], _symbol(layer, style, item),
                item.get("label") or f"{item['lower']:g} – {item['upper']:g}",
            )
            for item in style.get("ranges", [])
        ]
        if not ranges:
            raise ValueError("Graduated styling requires explicit ranges/colors")
        layer.setRenderer(QgsGraduatedSymbolRenderer(field, ranges))
    elif renderer == "single":
        layer.setRenderer(QgsSingleSymbolRenderer(_symbol(layer, style, {})))
    else:
        raise ValueError("Unsupported vector renderer")
    layer.setOpacity(float(style.get("opacity", 1.0)))
    field = spec.get("label_field")
    if field:
        if layer.fields().indexFromName(field) < 0:
            raise ValueError(f"Label field does not exist: {field}")
        settings = QgsPalLayerSettings()
        settings.fieldName = field
        label_spec = spec.get("label_style") or {}
        font, font_info = _font(label_spec, context)
        text_format = _text_format(
            font, label_spec.get("size_pt", 9), label_spec.get("color", "#222222")
        )
        buffer = QgsTextBufferSettings()
        buffer.setEnabled(label_spec.get("buffer_size_mm", 0.8) > 0)
        buffer.setSize(label_spec.get("buffer_size_mm", 0.8))
        buffer.setSizeUnit(Qgis.RenderUnit.Millimeters)
        buffer.setColor(_color(label_spec.get("buffer_color", "#ffffff")))
        text_format.setBuffer(buffer)
        settings.setFormat(text_format)
        placement = settings.placementSettings()
        placement.setOverlapHandling(Qgis.LabelOverlapHandling.PreventOverlap)
        placement.setAllowDegradedPlacement(False)
        settings.setPlacementSettings(placement)
        settings.displayAll = False
        settings.obstacle = True
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)
        layer.setCustomProperty("geoharness/label_font", json.dumps(font_info))


def _new_layer(spec: dict[str, Any], context: Any) -> Any:
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    path, options = _local_source(spec["file_path"], context)
    kind = spec.get("kind", "vector")
    name = spec.get("name") or path.stem
    if kind == "vector":
        if path.suffix.lower() not in _VECTOR_SUFFIXES:
            raise ValueError("Vector sources require GPKG, SHP, GeoJSON or FlatGeobuf")
        layer_name = spec.get("layer_name")
        if layer_name:
            if "|" in layer_name:
                raise ValueError("layer_name cannot contain OGR URI separators")
            options = f"layername={layer_name}"
        uri = str(path) + ("|" + options if options else "")
        layer = QgsVectorLayer(uri, name, "ogr")
    else:
        if path.suffix.lower() not in _RASTER_SUFFIXES or options:
            raise ValueError("Raster sources require one local raster file")
        layer = QgsRasterLayer(str(path), name, "gdal")
    if not layer.isValid():
        raise ValueError(f"QGIS could not load local layer: {name}")
    if not layer.crs().isValid():
        raise ValueError(f"Layer has no valid declared CRS: {name}")
    if kind == "vector":
        _style_vector(layer, spec, context)
    else:
        style = spec.get("raster_style") or {}
        if style.get("renderer", "default") == "hillshade":
            from qgis.core import QgsHillshadeRenderer

            band = style.get("band", 1)
            if not 1 <= band <= layer.bandCount():
                raise ValueError(f"Hillshade band {band} does not exist: {name}")
            layer.setRenderer(QgsHillshadeRenderer(
                layer.dataProvider(), band, style.get("azimuth", 315), style.get("altitude", 45),
            ))
    return layer


def _snapshot_layer(layer: Any, index: int, project: Any, context: Any) -> dict[str, Any]:
    from qgis.core import QgsMapLayerStyle, QgsRasterLayer, QgsVectorFileWriter, QgsVectorLayer

    saved_style = QgsMapLayerStyle()
    saved_style.readFromLayer(layer)
    if isinstance(layer, QgsVectorLayer):
        target = context.output_path(None, f"data/layer_{index}.gpkg")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = f"layer_{index}"
        options.fileEncoding = "UTF-8"
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer,
            target,
            project.transformContext(),
            options,
        )
        if result[0] != QgsVectorFileWriter.NoError:
            raise ValueError(f"Could not snapshot vector layer {layer.name()}: {result[1]}")
        layer.setDataSource(target + f"|layername=layer_{index}", layer.name(), "ogr", False)
    elif isinstance(layer, QgsRasterLayer):
        from osgeo import gdal

        target = context.output_path(None, f"data/layer_{index}.tif")
        source, _ = _local_source(layer.source(), context)
        dataset = gdal.Translate(target, str(source), format="GTiff")
        if dataset is None:
            raise ValueError(f"Could not snapshot raster layer: {layer.name()}")
        dataset.FlushCache()
        dataset = None
        layer.setDataSource(target, layer.name(), "gdal", False)
    else:
        raise ValueError("Only vector and raster snapshots are supported")
    if not layer.isValid():
        raise ValueError(f"Packaged layer is invalid: {layer.name()}")
    saved_style.writeToLayer(layer)
    style_path = context.output_path(None, f"styles/layer_{index}.qml")
    message, success = layer.saveNamedStyle(style_path)
    if not success:
        raise ValueError(f"Could not save layer style: {message}")
    return {
        **_layer_metadata(layer),
        "file": Path(target).relative_to(context.run_dir).as_posix(),
        "style_file": Path(style_path).relative_to(context.run_dir).as_posix(),
    }


def _extent(project: Any, layers: list[Any], requested: list[float] | None) -> Any:
    from qgis.core import QgsCoordinateTransform, QgsRectangle

    if requested is not None:
        return QgsRectangle(*requested)
    combined = QgsRectangle()
    for layer in layers:
        rect = QgsCoordinateTransform(
            layer.crs(),
            project.crs(),
            project,
        ).transformBoundingBox(layer.extent())
        if rect.isFinite():
            combined.combineExtentWith(rect)
    if combined.isNull() or not combined.isFinite():
        raise ValueError("Layers have no usable extent; provide a positive map_extent")
    if combined.width() == 0 or combined.height() == 0:
        raise ValueError("Point/line extent is degenerate; provide a positive map_extent")
    combined.scale(1.05)
    return combined


def _layout_regions(spec: dict[str, Any], title: str) -> dict[str, list[float]]:
    """Allocate millimetre rectangles from the measured, effective layout parameters."""
    width, height = spec.get("width_mm", 297), spec.get("height_mm", 210)
    margin, gap = spec.get("margin_mm", 12), spec.get("gap_mm", 5)
    inner_width = width - 2 * margin
    title_height = spec.get("title_height_mm", 16) if title else 0
    footer_height = spec.get("footer_height_mm", 18)
    top = margin + title_height + (gap if title else 0)
    footer_y = height - margin - footer_height
    map_height = footer_y - gap - top
    sidebar = spec.get("legend", True) or spec.get("detail_extent") is not None
    sidebar_width = spec.get("sidebar_width_mm", 60) if sidebar else 0
    map_width = inner_width - sidebar_width - (gap if sidebar else 0)
    if map_width < 40 or map_height < 40:
        raise ValueError("Structured layout requires at least 40mm for main map width/height")
    regions = {
        "main_map": [margin, top, map_width, map_height],
        "footer": [margin, footer_y, inner_width, footer_height],
    }
    if title:
        regions["title"] = [margin, margin, inner_width, title_height]
    sidebar_x = width - margin - sidebar_width
    if spec.get("detail_extent") is not None:
        detail_height = (map_height * spec.get("detail_height_ratio", 0.4)
                         if spec.get("legend", True) else map_height)
        regions["detail_map"] = [sidebar_x, top, sidebar_width, detail_height]
        top += detail_height + gap
    if spec.get("legend", True):
        legend_height = footer_y - gap - top
        if legend_height < 20:
            raise ValueError("Structured layout requires at least 20mm for the legend")
        regions["legend"] = [sidebar_x, top, sidebar_width, legend_height]
    return regions


def _place_item(layout: Any, item: Any, name: str, rect: list[float]) -> None:
    from qgis.core import QgsLayoutPoint, QgsLayoutSize

    item.setId(name)
    layout.addLayoutItem(item)
    item.attemptMove(QgsLayoutPoint(rect[0], rect[1]))
    item.attemptResize(QgsLayoutSize(rect[2], rect[3]))


def _create_layout(
    project: Any, layers: list[Any], extent: Any, payload: dict[str, Any], context: Any,
    *, _fit_attempt: int = 0, _adaptations: list[dict[str, Any]] | None = None,
) -> Any:
    from qgis.core import (
        Qgis,
        QgsBasicNumericFormat,
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemMap,
        QgsLayoutItemScaleBar,
        QgsLayoutSize,
        QgsLegendRenderer,
        QgsPrintLayout,
        QgsRectangle,
    )
    from qgis.PyQt.QtCore import Qt

    spec = payload.get("layout") or {}
    width, height = spec.get("width_mm", 297), spec.get("height_mm", 210)
    title = payload.get("title", "")
    regions = _layout_regions(spec, title)
    font, font_info = _font(spec, context)
    body_format = _text_format(font, spec.get("body_font_size_pt", 9))
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(payload.get("layout_name") or spec.get("name", "Map"))
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(width, height))
    layout.renderContext().setDpi(payload.get("dpi", 150))
    layout.setCustomProperty("geoharness/template", "structured-v1")
    layout.setCustomProperty("geoharness/regions", json.dumps(regions))
    layout.setCustomProperty("geoharness/font", json.dumps(font_info))
    layout.setCustomProperty("geoharness/effective_parameters", json.dumps(spec))
    adaptations = list(_adaptations or [])
    layout.setCustomProperty("geoharness/adaptations", json.dumps(adaptations))

    def add_map(name: str, rect: Any) -> Any:
        item = QgsLayoutItemMap(layout)
        _place_item(layout, item, name, regions[name])
        item.setCrs(project.crs())
        item.setLayers(list(reversed(layers)))
        item.setKeepLayerSet(True)
        item.zoomToExtent(rect)
        item.setFrameEnabled(True)
        return item

    map_item = add_map("main_map", extent)
    layout.setReferenceMap(map_item)
    if spec.get("detail_extent") is not None:
        detail = spec["detail_extent"]
        add_map(
            "detail_map",
            QgsRectangle(*(detail[key] for key in ("xmin", "ymin", "xmax", "ymax"))),
        )
    if title:
        label = QgsLayoutItemLabel(layout)
        label.setText(title)
        label.setTextFormat(_text_format(font, spec.get("title_font_size_pt", 18)))
        label.setMargin(0)
        label.setVAlign(Qt.AlignVCenter)
        _place_item(layout, label, "title", regions["title"])
    if spec.get("legend", True):
        legend = QgsLayoutItemLegend(layout)
        legend.setTitle("图例")
        legend.setLinkedMap(map_item)
        legend.setResizeToContents(False)
        legend.setAutoUpdateModel(False)
        root = legend.model().rootGroup()
        root.removeAllChildren()
        for layer in reversed(layers):
            root.addLayer(layer)
        legend.setWrapString("\n")
        legend_format = _text_format(font, spec.get("legend_font_size_pt")
                                    or spec.get("body_font_size_pt", 9))
        for component in (
            Qgis.LegendComponent.Title, Qgis.LegendComponent.Group,
            Qgis.LegendComponent.Subgroup, Qgis.LegendComponent.SymbolLabel,
        ):
            style = legend.style(component)
            style.setTextFormat(legend_format)
            legend.setStyle(component, style)
        _place_item(layout, legend, "legend", regions["legend"])
    footer = regions["footer"]
    text_x = footer[0]
    required_height = footer[3]
    if spec.get("scale_bar", True):
        scale = QgsLayoutItemScaleBar(layout)
        scale.setStyle("Single Box")
        scale.setLinkedMap(map_item)
        scale.setUnits(Qgis.DistanceUnit.Kilometers)
        scale.setNumberOfSegments(2)
        scale.setNumberOfSegmentsLeft(0)
        scale.applyDefaultSize(Qgis.DistanceUnit.Kilometers)
        scale.setTextFormat(body_format)
        target = map_item.scale() * 0.02 / 1000
        magnitude = 10 ** math.floor(math.log10(target))
        distance = max(value for value in (1, 2, 5) if value * magnitude <= target) * magnitude
        scale.setUnitsPerSegment(distance)
        numeric = QgsBasicNumericFormat()
        numeric.setNumberDecimalPlaces(max(0, -math.floor(math.log10(distance))))
        numeric.setShowTrailingZeros(False)
        scale.setNumericFormat(numeric)
        scale.setUnitLabel("km")
        _place_item(layout, scale, "scale_bar", [footer[0], footer[1], 1, 1])
        text_x += scale.rect().width() + spec.get("gap_mm", 5)
        required_height = max(required_height, scale.rect().height() + 0.5)
    if spec.get("footer_text"):
        text_width = footer[0] + footer[2] - text_x
        if text_width < 20:
            raise ValueError("Scale bar leaves less than 20mm for footer_text")
        label = QgsLayoutItemLabel(layout)
        label.setTextFormat(body_format)
        label.setMargin(0)
        label.setVAlign(Qt.AlignTop)
        label.setText(_wrap_label_text(label, spec["footer_text"], text_width))
        required_height = max(required_height, label.sizeForText().height() + 0.5)
        _place_item(layout, label, "footer_text", [text_x, footer[1], text_width, footer[3]])
    if required_height > footer[3]:
        if _fit_attempt >= 3 or required_height > 100:
            raise ValueError("footer_text exceeds its reserved rectangle; "
                             "shorten notes or increase page size")
        resized = {**spec, "footer_height_mm": required_height}
        _layout_regions(resized, title)  # Fail before exporting or packaging any map.
        return _create_layout(project, layers, extent, {**payload, "layout": resized},
                              context, _fit_attempt=_fit_attempt + 1,
                              _adaptations=[*adaptations, {
                                  "parameter": "footer_height_mm", "before": footer[3],
                                  "after": required_height, "reason": "measured footer text",
                              }])
    if spec.get("legend", True) and spec.get("legend_auto_fit", True):
        minimum = QgsLegendRenderer(legend.model(), legend.legendSettings()).minimumSize()
        available = regions["legend"]
        resized = dict(spec)
        if minimum.width() > available[2] + 0.05 and minimum.width() + 0.5 <= 500:
            resized["sidebar_width_mm"] = minimum.width() + 0.5
        if minimum.height() > available[3] + 0.05 and "detail_map" in regions:
            height = regions["main_map"][3]
            detail_height = height - spec.get("gap_mm", 5) - minimum.height() - 0.5
            if detail_height >= max(20, height * 0.1):
                resized["detail_height_ratio"] = detail_height / height
        if resized != spec and _fit_attempt < 3:
            try:
                _layout_regions(resized, title)
            except ValueError:
                pass  # Report measured content and dimensions below; never drop entries.
            else:
                changes = [{"parameter": key, "before": spec.get(key), "after": value,
                            "reason": "measured legend content"}
                           for key, value in resized.items() if spec.get(key) != value]
                return _create_layout(project, layers, extent, {**payload, "layout": resized},
                    context, _fit_attempt=_fit_attempt + 1, _adaptations=[*adaptations, *changes])
    project.layoutManager().addLayout(layout)
    _validate_layout_bounds(layout)
    return layout


def _wrap_label_text(label: Any, text: str, width: float) -> str:
    """Measure the actual QgsLayoutItemLabel font, including CJK and explicit lines."""
    if len(text) > 20000:
        raise ValueError("footer_text exceeds the bounded layout text limit")
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
        while paragraph:
            low, high = 0, len(paragraph)
            while low < high:
                middle = (low + high + 1) // 2
                label.setText(paragraph[:middle])
                if label.sizeForText().width() <= width - 0.5:
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                raise ValueError("footer_text font is too large for the available width")
            boundary = paragraph.rfind(" ", 0, low)
            if low < len(paragraph) and boundary > low // 2:
                low = boundary + 1
            lines.append(paragraph[:low])
            paragraph = paragraph[low:]
    return "\n".join(lines)


def _layer_metadata(layer: Any) -> dict[str, Any]:
    from qgis.core import (
        QgsCategorizedSymbolRenderer,
        QgsGraduatedSymbolRenderer,
        QgsHillshadeRenderer,
        QgsVectorLayer,
    )

    metadata = {
        "id": layer.id(),
        "name": layer.name(),
        "provider": layer.providerType(),
        "crs": layer.crs().authid() or layer.crs().toWkt(),
        "valid": layer.isValid(),
    }
    if isinstance(layer, QgsVectorLayer):
        renderer = layer.renderer()
        metadata.update(
            kind="vector",
            feature_count=layer.featureCount(),
            fields=[field.name() for field in layer.fields()],
            renderer=renderer.type() if renderer is not None else None,
            labels_enabled=layer.labelsEnabled(),
        )
        if isinstance(renderer, QgsCategorizedSymbolRenderer):
            metadata["category_field"] = renderer.classAttribute()
            metadata["categories"] = [
                {
                    "value": category.value(),
                    "label": category.label(),
                    **_symbol_metadata(category.symbol()),
                }
                for category in renderer.categories()
            ]
        elif isinstance(renderer, QgsGraduatedSymbolRenderer):
            metadata["range_field"] = renderer.classAttribute()
            metadata["ranges"] = [
                {
                    "lower": item.lowerValue(), "upper": item.upperValue(),
                    "label": item.label(), **_symbol_metadata(item.symbol()),
                }
                for item in renderer.ranges()
            ]
        elif renderer is not None and renderer.type() == "singleSymbol":
            metadata["symbol"] = _symbol_metadata(renderer.symbol())
        if layer.labelsEnabled() and layer.labeling() is not None:
            settings = layer.labeling().settings()
            text_format = settings.format()
            metadata["label_style"] = {
                "field": settings.fieldName,
                "font": json.loads(layer.customProperty("geoharness/label_font", "{}")),
                "size_pt": text_format.size(),
                "buffer_enabled": text_format.buffer().enabled(),
                "buffer_size_mm": text_format.buffer().size(),
                "overlap_handling": settings.placementSettings().overlapHandling().name,
            }
    else:
        metadata["kind"] = "raster"
        renderer = layer.renderer()
        metadata["renderer"] = renderer.type() if renderer is not None else None
        metadata["band_count"] = layer.bandCount()
        if isinstance(renderer, QgsHillshadeRenderer):
            metadata["raster_style"] = {
                "renderer": "hillshade", "band": renderer.inputBand(),
                "azimuth": renderer.azimuth(), "altitude": renderer.altitude(),
            }
    return metadata


def _rectangle_problems(
    rectangles: dict[str, list[float]], width: float, height: float
) -> list[str]:
    problems = []
    tolerance = 0.05
    for name, (x, y, w, h) in rectangles.items():
        if not all(math.isfinite(value) for value in (x, y, w, h)) or w <= 0 or h <= 0:
            problems.append(f"{name}: invalid rectangle")
        elif x < -tolerance or y < -tolerance or x + w > width + tolerance or (
            y + h > height + tolerance
        ):
            problems.append(f"{name}: rectangle exceeds page bounds")
    entries = list(rectangles.items())
    for index, (name, (x, y, w, h)) in enumerate(entries):
        for other, (ox, oy, ow, oh) in entries[index + 1:]:
            if min(x + w, ox + ow) - max(x, ox) > tolerance and (
                min(y + h, oy + oh) - max(y, oy) > tolerance
            ):
                problems.append(f"{name}/{other}: rectangles overlap")
    return problems


def _layout_bounds(layout: Any) -> dict[str, Any]:
    from qgis.core import (
        Qgis,
        QgsLayoutItem,
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemPage,
        QgsLegendRenderer,
    )

    if layout.customProperty("geoharness/template", "") != "structured-v1":
        return {"checked": False, "valid": None, "problems": []}
    if layout.units() != Qgis.LayoutUnit.Millimeters:
        return {"checked": False, "valid": None, "problems": [],
                "reason": "Bounds check requires a millimetre scene; saved layout preserved"}
    page = layout.renderContext().measurementConverter().convert(
        layout.pageCollection().page(0).pageSize(), Qgis.LayoutUnit.Millimeters)
    regions = json.loads(layout.customProperty("geoharness/regions", "{}"))
    rectangles = {}
    user_items = {}
    problems = []
    content_metrics = {}
    for item in layout.items():
        if not isinstance(item, QgsLayoutItem) or isinstance(item, QgsLayoutItemPage):
            continue
        name = item.id() or item.uuid()
        rect = item.mapRectToScene(item.rect())
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        if name not in {*regions, "scale_bar", "footer_text"}:
            # User annotations, north arrows and inset decorations legitimately
            # overlap maps. Preserve them; pixel review assesses their placement.
            user_items[name] = [x, y, w, h]
            continue
        rectangles[name] = [x, y, w, h]
        owner = regions.get("footer") if name in {"scale_bar", "footer_text"} else regions.get(name)
        if owner is not None:
            ox, oy, width, height = owner
            if x < ox - 0.05 or y < oy - 0.05 or (
                x + w > ox + width + 0.05 or y + h > oy + height + 0.05
            ):
                problems.append(f"{name}: item exceeds its reserved rectangle")
        if isinstance(item, QgsLayoutItemLegend):
            minimum = QgsLegendRenderer(item.model(), item.legendSettings()).minimumSize()
            content_metrics[name] = {
                "required_mm": [minimum.width(), minimum.height()],
                "available_mm": [w, h],
            }
            if minimum.width() > w + 0.05 or (
                minimum.height() > h + 0.05
            ):
                problems.append(f"{name}: legend content exceeds its reserved rectangle")
        elif isinstance(item, QgsLayoutItemLabel):
            minimum = layout.renderContext().measurementConverter().convert(
                item.sizeForText(), Qgis.LayoutUnit.Millimeters)
            if minimum.width() > w + 0.05 or (
                minimum.height() > h + 0.05
            ):
                problems.append(f"{name}: label text exceeds its reserved rectangle")
    problems.extend(_rectangle_problems(rectangles, page.width(), page.height()))
    return {
        "checked": True, "valid": not problems, "problems": problems,
        "items": rectangles, "user_items": user_items, "content_metrics": content_metrics,
    }


def _validate_layout_bounds(layout: Any) -> None:
    result = _layout_bounds(layout)
    if result["problems"]:
        error = ValueError("Structured layout validation failed: " + "; ".join(result["problems"])
                           + "; use the measured required_mm/available_mm, effective parameters "
                           "and loaded font evidence to adjust page/sidebar dimensions")
        error.diagnostics = _layout_metadata(layout)
        raise error


def _layout_metadata(layout: Any) -> dict[str, Any]:
    from qgis.core import (
        Qgis,
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemMap,
        QgsLayoutItemScaleBar,
    )

    maps = []
    scale_bars = []
    page = layout.pageCollection().page(0)
    converter = layout.renderContext().measurementConverter()
    page_size = converter.convert(page.pageSize(), Qgis.LayoutUnit.Millimeters) if page else None
    effective = ({"width_mm": page_size.width(), "height_mm": page_size.height()}
                 if page_size else {})

    def record_font(key: str, text_format: Any) -> None:
        effective[key] = {"value": text_format.size(), "unit": text_format.sizeUnit().name}
        if text_format.sizeUnit() == Qgis.RenderUnit.Points:
            effective[key + "_pt"] = text_format.size()
        else:
            effective.pop(key + "_pt", None)
    for item in layout.items():
        if isinstance(item, QgsLayoutItemMap):
            extent = item.extent()
            maps.append(
                {
                    "id": item.id(),
                    "crs": item.crs().authid(),
                    "crs_valid": item.crs().isValid(),
                    "scale": item.scale(),
                    "extent": [
                        extent.xMinimum(),
                        extent.yMinimum(),
                        extent.xMaximum(),
                        extent.yMaximum(),
                    ],
                    "layers": [layer.id() for layer in item.layers()],
                }
            )
            main = layout.itemById("main_map")
            if item.id() == "detail_map" and main and main.rect().height() > 0:
                effective["detail_height_ratio"] = (
                    item.rect().height() / main.rect().height())
        elif isinstance(item, QgsLayoutItemLegend):
            effective["sidebar_width_mm"] = converter.convert(
                item.sizeWithUnits(), Qgis.LayoutUnit.Millimeters).width()
            record_font("legend_font_size", item.style(
                Qgis.LegendComponent.SymbolLabel).textFormat())
        elif isinstance(item, QgsLayoutItemLabel):
            key = {"title": "title_font_size_pt", "footer_text": "body_font_size_pt"}.get(
                item.id())
            if key:
                record_font(key.removesuffix("_pt"), item.textFormat())
        elif isinstance(item, QgsLayoutItemScaleBar):
            if "body_font_size" not in effective:
                record_font("body_font_size", item.textFormat())
            scale_bars.append({
                "id": item.id(), "units": item.units().name,
                "units_per_segment": item.unitsPerSegment(), "segments": item.numberOfSegments(),
            })
    return {
        "name": layout.name(), "pages": layout.pageCollection().pageCount(), "map_frames": maps,
        "map_extents": {item["id"]: item["extent"] for item in maps},
        "scale_bars": scale_bars,
        "template": layout.customProperty("geoharness/template", ""),
        "regions": json.loads(layout.customProperty("geoharness/regions", "{}")),
        "font": json.loads(layout.customProperty("geoharness/font", "{}")),
        "effective_parameters": effective,
        "creation_parameters": json.loads(layout.customProperty(
            "geoharness/effective_parameters", "{}")),
        "adaptations": json.loads(layout.customProperty("geoharness/adaptations", "[]")),
        "bounds_check": _layout_bounds(layout),
        "footer_text": (layout.itemById("footer_text").text()
                        if isinstance(layout.itemById("footer_text"), QgsLayoutItemLabel) else ""),
    }


def _validate_project_coordinates(
    project: Any, layouts: list[Any], *, strict_layout: bool = False
) -> None:
    """A readable project with valid layers can still have projection disabled."""
    if not project.crs().isValid():
        raise ValueError(
            "Project CRS is invalid; verify projection is enabled in the saved project"
        )
    for layout in layouts:
        if strict_layout:
            _validate_layout_bounds(layout)
        for frame in _layout_metadata(layout)["map_frames"]:
            if not frame["crs_valid"]:
                raise ValueError(f"Layout map {frame['id']} has an invalid CRS")
            xmin, ymin, xmax, ymax = frame["extent"]
            if not all(math.isfinite(value) for value in frame["extent"]):
                raise ValueError(f"Layout map {frame['id']} has a nonfinite extent")
            if xmin >= xmax or ymin >= ymax:
                raise ValueError(f"Layout map {frame['id']} has a zero or reversed extent")
            if not math.isfinite(frame["scale"]) or frame["scale"] <= 0:
                raise ValueError(f"Layout map {frame['id']} has an invalid scale")


def _export_layout(layout: Any, payload: dict[str, Any], context: Any) -> tuple[Path, Path]:
    from qgis.core import QgsLayoutExporter

    exporter = QgsLayoutExporter(layout)
    if payload.get("strict_layout", False):
        _validate_layout_bounds(layout)
    preview = Path(context.output_path(None, "preview.png"))
    image_settings = QgsLayoutExporter.ImageExportSettings()
    image_settings.dpi = payload.get("dpi", 150)
    image_settings.pages = [0]
    status = exporter.exportToImage(str(preview), image_settings)
    if status != QgsLayoutExporter.Success:
        raise ValueError(f"QGIS PNG export failed: {exporter.errorMessage()}")
    if not preview.is_file() and preview.with_stem("preview_1").is_file():
        preview.with_stem("preview_1").rename(preview)
    if not preview.is_file():
        raise ValueError("QGIS did not write the layout preview")
    if payload.get("strict_layout", False):
        _validate_layout_bounds(layout)
    output_format = payload.get("output_format", "png")
    if output_format == "png":
        requested = payload.get("output_path")
        output = preview
        if requested:
            candidate = Path(requested)
            candidate = candidate if candidate.is_absolute() else context.run_dir / candidate
            if candidate.resolve() != preview:
                output = Path(context.output_path(requested, "preview.png"))
        if output.suffix.lower() != ".png":
            raise ValueError("PNG output_path must end with .png")
        if output != preview:
            shutil.copy2(preview, output)
    elif output_format == "pdf":
        output = Path(context.output_path(payload.get("output_path") or None, "map.pdf"))
        if output.suffix.lower() != ".pdf":
            raise ValueError("PDF output_path must end with .pdf")
        pdf_settings = QgsLayoutExporter.PdfExportSettings()
        pdf_settings.dpi = payload.get("dpi", 150)
        status = exporter.exportToPdf(str(output), pdf_settings)
        if status != QgsLayoutExporter.Success or not output.is_file():
            raise ValueError(f"QGIS PDF export failed: {exporter.errorMessage()}")
    else:
        raise ValueError("Only PNG/PDF layout export is supported")
    return preview, output


def _save_package(
    project: Any,
    layout: Any,
    layers: list[Any],
    payload: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    from qgis.core import Qgis

    layer_info = [
        _snapshot_layer(layer, index, project, context) for index, layer in enumerate(layers)
    ]
    facts = payload.get("analysis_facts")
    if facts is None:
        # Preserve the original provenance on user-edited exports as historical evidence.
        # This does not certify edited layers or refresh their old statistics.
        recorded, _ = project.readEntry("geoharness", "analysis_facts", "")
        if recorded:
            try:
                facts = json.loads(recorded)
            except (TypeError, ValueError):
                facts = None
    if isinstance(facts, dict):
        facts_path = Path(context.output_path(None, "analysis/verified-facts.json"))
        facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
        project.writeEntry("geoharness", "analysis_facts", json.dumps(facts, ensure_ascii=False))
    if payload.get("map_id"):
        project.writeEntry("geoharness", "map_job_id", payload["map_id"])
    project_path = Path(context.output_path(None, "map." + payload.get("project_format", "qgz")))
    project.setPresetHomePath("")
    project.setFilePathStorage(Qgis.FilePathType.Relative)
    project.setFileName(str(project_path))
    if not project.write():
        raise ValueError(f"QGIS could not save the packaged project: {project.error()}")
    from qgis.core import QgsProject

    reopened = QgsProject()
    try:
        _register_project_fonts(project_path, context)
        if not reopened.read(str(project_path)):
            raise ValueError(f"QGIS could not reopen its packaged project: {reopened.error()}")
        if not all(layer.isValid() for layer in reopened.mapLayers().values()):
            raise ValueError("Packaged project contains invalid data references")
        _restore_font_metadata(reopened, project_path, context, package=False)
        _validate_project_coordinates(
            reopened, reopened.layoutManager().printLayouts(),
            strict_layout=payload.get("strict_layout", False),
        )
        saved_layout = next(
            (item for item in reopened.layoutManager().printLayouts()
             if item.name() == layout.name()),
            None,
        )
        if saved_layout is None:
            raise ValueError("Packaged project did not preserve the selected print layout")
        # Native save/read normalizes floating coordinates and scale-bar metrics.
        # Export the saved layout so preview pixels correspond to the delivered QGS.
        preview, output = _export_layout(saved_layout, payload, context)
        layout_info = _layout_metadata(saved_layout)
    finally:
        reopened.clear()
    digest = hashlib.sha256(preview.read_bytes()).hexdigest()
    readme = Path(context.output_path(None, "README.md"))
    readme.write_text(
        "# Editable QGIS map\n\nOpen `" + project_path.name + "` in QGIS. Packaged data and "
        "styles are local snapshots; source files were not changed. Edit the saved print "
        "layout, save it, then use geo_qgis_project export to create a new package.\n\n"
        "`preview.png` is the first page exported from that same layout. This is an "
        "unverified draft; successful QGIS export does not certify visual or analytical "
        "quality.\n\n"
        "Explicit custom fonts, when present, are copied into `assets/fonts/` and registered "
        "by GeoHarness before re-export. QGIS desktop does not automatically register font "
        "files from GeoHarness custom properties. If a custom font is absent on another "
        "computer, select an installed font in QGIS or install the supplied font yourself. "
        "GeoHarness does not install fonts into the operating system.\n",
        encoding="utf-8",
    )
    manifest_path = Path(context.output_path(None, "project.json"))
    files = [project_path, preview, output, readme, manifest_path]
    for directory in (
        context.run_dir / "data",
        context.run_dir / "styles",
        context.run_dir / "assets",
        context.run_dir / "analysis",
    ):
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    manifest = {
        "backend": "qgis",
        "project_fidelity": "saved_layout",
        "project_file": project_path.name,
        "preview": "preview.png",
        "preview_sha256": digest,
        "quality_status": "draft",
        "visual_review_status": "unverified",
        "layers": layer_info,
        "layout": layout_info,
        "map_id": payload.get("map_id", ""),
        "analysis": facts,
        "files": sorted({path.relative_to(context.run_dir).as_posix() for path in files}),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "backend": "qgis",
        "project_fidelity": "saved_layout",
        "project_dir": str(context.run_dir),
        "project_file": str(project_path),
        "project_manifest": str(manifest_path),
        "output_path": str(output),
        "preview_path": str(preview),
        "image_sha256": digest,
        "quality_status": "draft",
        "visual_review_status": "unverified",
        "primary_map": str(preview),
        "layers": layer_info,
        "layout": manifest["layout"],
        "analysis": facts,
    }


def _project_layers(project: Any) -> list[Any]:
    """Include non-tree dependencies without changing saved drawing order or visibility."""
    layers = list(reversed(project.layerTreeRoot().layerOrder()))
    tree_ids = {layer.id() for layer in layers}
    layers.extend(layer for layer in project.mapLayers().values() if layer.id() not in tree_ids)
    return layers


def _relocate_symbol_assets(symbol: Any, relocate: Any) -> None:
    """Set native resource properties, including nested fill/marker symbols."""
    if symbol is None:
        return
    for symbol_layer in symbol.symbolLayers():
        for getter, setter in (("path", "setPath"), ("svgFilePath", "setSvgFilePath")):
            read = getattr(symbol_layer, getter, None)
            write = getattr(symbol_layer, setter, None)
            if callable(read) and callable(write):
                original = read()
                replacement = relocate(original)
                if replacement != original:
                    write(replacement)
        _relocate_symbol_assets(symbol_layer.subSymbol(), relocate)


def _relocate_project_assets(project: Any, relocate: Any) -> None:
    """Builtin SVG resolution can bypass QgsPathResolver; update live QGIS objects."""
    from qgis.core import (
        QgsLayoutItemMap,
        QgsLayoutItemPicture,
        QgsMapLayerStyle,
        QgsRenderContext,
        QgsVectorLayer,
    )

    def relocate_layer(layer: Any) -> None:
        if isinstance(layer, QgsVectorLayer) and layer.renderer() is not None:
            for symbol in layer.renderer().symbols(QgsRenderContext()):
                _relocate_symbol_assets(symbol, relocate)

    for layer in project.mapLayers().values():
        relocate_layer(layer)
    for layout in project.layoutManager().printLayouts():
        for item in layout.items():
            if isinstance(item, QgsLayoutItemPicture):
                original = item.picturePath()
                replacement = relocate(original)
                if replacement != original:
                    item.setPicturePath(replacement)
            elif isinstance(item, QgsLayoutItemMap):
                overrides = item.layerStyleOverrides()
                for layer_id, xml in overrides.items():
                    layer = project.mapLayer(layer_id)
                    if layer is None:
                        continue
                    current = QgsMapLayerStyle()
                    current.readFromLayer(layer)
                    try:
                        QgsMapLayerStyle(xml).writeToLayer(layer)
                        relocate_layer(layer)
                        packaged = QgsMapLayerStyle()
                        packaged.readFromLayer(layer)
                        overrides[layer_id] = packaged.xmlData()
                    finally:
                        current.writeToLayer(layer)
                if overrides:
                    item.setLayerStyleOverrides(overrides)


def dispatch(action: str, payload: dict[str, Any], context: Any) -> dict[str, Any]:
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsPathResolver,
        QgsProject,
        QgsReferencedRectangle,
    )

    project = QgsProject.instance()
    project.clear()
    payload = {**payload, "strict_layout": action in {"project_create", "project_preflight"}}
    preprocessor_id = None
    try:
        if action in {"project_create", "project_preflight"}:
            layers = [_new_layer(spec, context) for spec in payload.get("layers", [])]
            if not layers:
                raise ValueError("create requires at least one local vector/raster layer")
            crs = (
                QgsCoordinateReferenceSystem(payload["crs"])
                if payload.get("crs")
                else layers[0].crs()
            )
            if not crs.isValid():
                raise ValueError("Invalid project CRS")
            project.setCrs(crs)
            project.setTitle(payload.get("title", ""))
            for layer in layers:
                project.addMapLayer(layer)
            project.layerTreeRoot().setHasCustomLayerOrder(True)
            project.layerTreeRoot().setCustomLayerOrder(list(reversed(layers)))
            extent = _extent(project, layers, payload.get("map_extent"))
            project.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(extent, crs))
            layout = _create_layout(project, layers, extent, payload, context)
            if action == "project_preflight":
                return {"layout": _layout_metadata(layout), "render_executed": False}
            return _save_package(project, layout, layers, payload, context)
        if action not in {"project_inspect", "project_export"}:
            raise ValueError(f"Unsupported QGIS project action: {action}")
        source_project = context.read_path(payload["project_path"])
        asset_sources: dict[str, Path] = {}
        assets = _preflight_project(source_project, context, asset_sources)
        _register_project_fonts(source_project, context)
        if action == "project_export" and assets:
            replacements = {}
            for index, asset in enumerate(assets):
                if asset.suffix.lower() in _FONT_SUFFIXES:
                    target, _ = _snapshot_font(asset, context)
                else:
                    target = Path(
                        context.output_path(None, f"assets/{index}{asset.suffix.lower()}")
                    )
                    shutil.copy2(asset, target)
                replacements[str(asset)] = str(target)
            aliases = {name: replacements[str(asset)] for name, asset in asset_sources.items()}

            def relocate_asset(value: str) -> str:
                if value in aliases:
                    return aliases[value]
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = source_project.parent / candidate
                return replacements.get(str(candidate.resolve()), value)

            preprocessor_id = QgsPathResolver.setPathPreprocessor(relocate_asset)
        if not project.read(str(source_project)):
            raise ValueError(f"QGIS could not read project: {project.error()}")
        _restore_font_metadata(
            project, source_project, context, package=action == "project_export"
        )
        if action == "project_export" and assets:
            _relocate_project_assets(project, relocate_asset)
        layers = _project_layers(project)
        if not layers or not all(layer.isValid() for layer in layers):
            raise ValueError("Project has invalid or missing local layers")
        layouts = project.layoutManager().printLayouts()
        _validate_project_coordinates(project, layouts)
        if action == "project_inspect":
            return {
                "backend": "qgis",
                "project_file": str(source_project),
                "crs": project.crs().authid(),
                "crs_valid": project.crs().isValid(),
                "layers": [_layer_metadata(layer) for layer in layers],
                "layouts": [_layout_metadata(layout) for layout in layouts],
                "quality_status": "unverified",
                "visual_review_status": "unverified",
            }
        requested = payload.get("layout_name")
        if requested:
            layout = next((item for item in layouts if item.name() == requested), None)
            if layout is None:
                raise ValueError(f"Saved print layout was not found: {requested}")
        elif len(layouts) == 1:
            layout = layouts[0]
        else:
            raise ValueError("Export requires one saved layout or an explicit layout_name")
        return _save_package(project, layout, layers, payload, context)
    finally:
        if preprocessor_id is not None:
            QgsPathResolver.removePathPreprocessor(preprocessor_id)
        project.clear()
