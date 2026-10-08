"""Project operations loaded only by the standalone, native QGIS worker.

QGS XML is inspected before loading to reject external providers and unsafe links.
Projects and styles are written by QGIS itself, never synthesized as XML here.
"""

from __future__ import annotations

import hashlib
import json
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


def _color(value: str) -> Any:
    from qgis.PyQt.QtGui import QColor

    color = QColor(value)
    if not color.isValid():
        raise ValueError(f"Invalid symbol color: {value}")
    return color


def _style_vector(layer: Any, spec: dict[str, Any]) -> None:
    from qgis.core import (
        QgsCategorizedSymbolRenderer,
        QgsPalLayerSettings,
        QgsRendererCategory,
        QgsSingleSymbolRenderer,
        QgsSymbol,
        QgsTextFormat,
        QgsVectorLayerSimpleLabeling,
    )

    style = spec.get("style") or {}
    renderer = style.get("renderer", "single")
    if renderer == "categorized":
        field = style.get("field", "")
        if layer.fields().indexFromName(field) < 0:
            raise ValueError(f"Categorized field does not exist: {field}")
        categories = []
        for category in style.get("categories", []):
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            symbol.setColor(_color(category["color"]))
            value = category["value"]
            label = category.get("label") or ("NULL" if value is None else str(value))
            categories.append(QgsRendererCategory(value, symbol, label))
        if not categories:
            raise ValueError("Categorized styling requires explicit categories/colors")
        layer.setRenderer(QgsCategorizedSymbolRenderer(field, categories))
    elif renderer == "single":
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        if symbol is None:
            raise ValueError("Project maps require a spatial vector layer")
        symbol.setColor(_color(style.get("color", "#4c78a8")))
        layer.setRenderer(QgsSingleSymbolRenderer(symbol))
    else:
        raise ValueError("Unsupported vector renderer")
    layer.setOpacity(float(style.get("opacity", 1.0)))
    field = spec.get("label_field")
    if field:
        if layer.fields().indexFromName(field) < 0:
            raise ValueError(f"Label field does not exist: {field}")
        settings = QgsPalLayerSettings()
        settings.fieldName = field
        text_format = QgsTextFormat()
        text_format.setSize(10)
        settings.setFormat(text_format)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)


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
        _style_vector(layer, spec)
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


def _create_layout(project: Any, layers: list[Any], extent: Any, payload: dict[str, Any]) -> Any:
    from qgis.core import (
        Qgis,
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemMap,
        QgsLayoutItemScaleBar,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsPrintLayout,
    )
    from qgis.PyQt.QtGui import QFont

    spec = payload.get("layout") or {}
    width, height = spec.get("width_mm", 297), spec.get("height_mm", 210)
    margin = spec.get("margin_mm", 12)
    legend_enabled = spec.get("legend", True)
    legend_width = min(55, (width - 2 * margin) / 4) if legend_enabled else 0
    title = payload.get("title", "")
    title_height = 16 if title else 0
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(payload.get("layout_name") or spec.get("name", "Map"))
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(width, height))
    layout.renderContext().setDpi(payload.get("dpi", 150))
    map_item = QgsLayoutItemMap(layout)
    map_item.setId("main_map")
    layout.addLayoutItem(map_item)
    map_item.attemptMove(QgsLayoutPoint(margin, margin + title_height))
    map_item.attemptResize(
        QgsLayoutSize(
            width - 2 * margin - legend_width - (5 if legend_enabled else 0),
            height - 2 * margin - title_height - 14,
        )
    )
    map_item.setCrs(project.crs())
    map_item.setLayers(list(reversed(layers)))
    map_item.setKeepLayerSet(True)
    map_item.zoomToExtent(extent)
    map_item.setFrameEnabled(True)
    layout.setReferenceMap(map_item)
    if title:
        label = QgsLayoutItemLabel(layout)
        label.setText(title)
        font = QFont()
        font.setPointSize(18)
        label.setFont(font)
        layout.addLayoutItem(label)
        label.attemptMove(QgsLayoutPoint(margin, margin))
        label.attemptResize(QgsLayoutSize(width - 2 * margin, title_height))
    if legend_enabled:
        legend = QgsLayoutItemLegend(layout)
        legend.setTitle("")
        legend.setLinkedMap(map_item)
        layout.addLayoutItem(legend)
        legend.attemptMove(QgsLayoutPoint(width - margin - legend_width, margin + title_height))
        legend.attemptResize(QgsLayoutSize(legend_width, height - 2 * margin - title_height))
    if spec.get("scale_bar", True):
        scale = QgsLayoutItemScaleBar(layout)
        scale.setStyle("Single Box")
        scale.setLinkedMap(map_item)
        scale.setUnits(Qgis.DistanceUnit.Kilometers)
        scale.setUnitsPerSegment(max(0.001, map_item.scale() * 0.02 / 1000))
        scale.setUnitLabel("km")
        scale.setNumberOfSegments(2)
        scale.setNumberOfSegmentsLeft(0)
        scale.applyDefaultSize()
        layout.addLayoutItem(scale)
        scale.attemptMove(QgsLayoutPoint(margin, height - margin - 10))
    project.layoutManager().addLayout(layout)
    return layout


def _layer_metadata(layer: Any) -> dict[str, Any]:
    from qgis.core import QgsCategorizedSymbolRenderer, QgsVectorLayer

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
                    "color": category.symbol().color().name(),
                }
                for category in renderer.categories()
            ]
    else:
        metadata["kind"] = "raster"
    return metadata


def _layout_metadata(layout: Any) -> dict[str, Any]:
    from qgis.core import QgsLayoutItemMap

    maps = []
    for item in layout.items():
        if isinstance(item, QgsLayoutItemMap):
            extent = item.extent()
            maps.append(
                {
                    "id": item.id(),
                    "crs": item.crs().authid(),
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
    return {"name": layout.name(), "pages": layout.pageCollection().pageCount(), "map_frames": maps}


def _export_layout(layout: Any, payload: dict[str, Any], context: Any) -> tuple[Path, Path]:
    from qgis.core import QgsLayoutExporter

    exporter = QgsLayoutExporter(layout)
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
    project_path = Path(context.output_path(None, "map." + payload.get("project_format", "qgz")))
    project.setPresetHomePath("")
    project.setFilePathStorage(Qgis.FilePathType.Relative)
    project.setFileName(str(project_path))
    if not project.write():
        raise ValueError(f"QGIS could not save the packaged project: {project.error()}")
    preview, output = _export_layout(layout, payload, context)
    digest = hashlib.sha256(preview.read_bytes()).hexdigest()
    readme = Path(context.output_path(None, "README.md"))
    readme.write_text(
        "# Editable QGIS map\n\nOpen `" + project_path.name + "` in QGIS. Packaged data and "
        "styles are local snapshots; source files were not changed. Edit the saved print "
        "layout, save it, then use geo_qgis_project export to create a new package.\n\n"
        "`preview.png` is the first page exported from that same layout. This is an "
        "unverified draft; successful QGIS export does not certify visual or analytical quality.\n",
        encoding="utf-8",
    )
    manifest_path = Path(context.output_path(None, "project.json"))
    files = [project_path, preview, output, readme, manifest_path]
    for directory in (
        context.run_dir / "data",
        context.run_dir / "styles",
        context.run_dir / "assets",
    ):
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    manifest = {
        "backend": "qgis",
        "project_file": project_path.name,
        "preview": "preview.png",
        "preview_sha256": digest,
        "quality_status": "draft",
        "visual_review_status": "unverified",
        "layers": layer_info,
        "layout": _layout_metadata(layout),
        "files": sorted({path.relative_to(context.run_dir).as_posix() for path in files}),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "backend": "qgis",
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
    preprocessor_id = None
    try:
        if action == "project_create":
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
            layout = _create_layout(project, layers, extent, payload)
            return _save_package(project, layout, layers, payload, context)
        if action not in {"project_inspect", "project_export"}:
            raise ValueError(f"Unsupported QGIS project action: {action}")
        source_project = context.read_path(payload["project_path"])
        asset_sources: dict[str, Path] = {}
        assets = _preflight_project(source_project, context, asset_sources)
        if action == "project_export" and assets:
            replacements = {}
            for index, asset in enumerate(assets):
                target = Path(context.output_path(None, f"assets/{index}{asset.suffix.lower()}"))
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
        if action == "project_export" and assets:
            _relocate_project_assets(project, relocate_asset)
        layers = _project_layers(project)
        if not layers or not all(layer.isValid() for layer in layers):
            raise ValueError("Project has invalid or missing local layers")
        layouts = project.layoutManager().printLayouts()
        if action == "project_inspect":
            return {
                "backend": "qgis",
                "project_file": str(source_project),
                "crs": project.crs().authid(),
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
