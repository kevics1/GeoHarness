"""Compile a reviewed native scene to editable QGIS objects, only at delivery.

No Pydantic/geopandas imports: this runs inside QGIS's separate interpreter.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path


def _sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def frozen_palette(plan):
    """Older reviewed plans omitted generated locator legend outline colors."""
    colors = dict(plan['colors'])
    for page in plan['pages']:
        for entry in page.get('legend_items', []):
            for color in (entry['color'], entry['symbol']['outline']):
                if color in colors:
                    continue
                if not re.fullmatch(r'#[0-9a-fA-F]{6}', color):
                    raise ValueError('Missing frozen legend RGBA: ' + str(color))
                colors[color] = ','.join([*(str(int(color[i:i+2], 16))
                                           for i in (1, 3, 5)), '255'])
    return colors


def equivalent_attribute(left, right):
    """Permit only a few binary rounding units during provider serialization."""
    if type(left) in (int, float) and type(right) in (int, float):
        if left == right:
            return True
        if type(left) is int or type(right) is int:
            return False
        if isinstance(left, float) or isinstance(right, float):
            a, b = float(left), float(right)
            return (math.isfinite(a) and math.isfinite(b)
                    and abs(a - b) <= 4 * max(math.ulp(a), math.ulp(b)))
        return False
    return type(left) is type(right) and str(left) == str(right)


def validate_snapshot(original, snapshot, fields, layer_id):
    """Match the full feature multiset, retaining strict geometry and text checks."""
    def geometry_key(feature):
        geometry = feature.geometry()
        geometry.convertToMultiType()
        return bytes(geometry.asWkb())

    buckets = {}
    for feature in snapshot.getFeatures():
        buckets.setdefault(geometry_key(feature), []).append([feature[name] for name in fields])
    for feature in original.getFeatures():
        rows = buckets.get(geometry_key(feature), [])
        values = [feature[name] for name in fields]
        found = next((i for i, row in enumerate(rows)
                      if all(equivalent_attribute(a, b) for a, b in zip(values, row))), None)
        if found is None:
            differences = ([{'field': name, 'source': str(a)[:160],
                             'snapshot': str(b)[:160]}
                            for name, a, b in zip(fields, values, rows[0])
                            if not equivalent_attribute(a, b)] if rows else [])
            raise ValueError('Snapshot differs from reviewed source: ' + json.dumps({
                'layer_id': layer_id, 'feature_id': feature.id(),
                'geometry_match': bool(rows), 'attribute_differences': differences[:8],
                'recovery': '内部工程快照差异；不要修改无关业务图层或重复制图审图。'},
                ensure_ascii=False))
        rows.pop(found)
    if any(buckets.values()):
        raise ValueError('Snapshot has extra features: ' + layer_id)


def dispatch(action, payload, context):
    from qgis.core import (
        QgsCategorizedSymbolRenderer,
        QgsContrastEnhancement,
        QgsCoordinateReferenceSystem,
        QgsExpression,
        QgsFillSymbol,
        QgsLayoutItemLabel,
        QgsLayoutItemMap,
        QgsLayoutItemPage,
        QgsLayoutItemPolyline,
        QgsLayoutItemShape,
        QgsLayoutPoint,
        QgsLayoutSize,
        QgsLineSymbol,
        QgsMarkerSymbol,
        QgsPrintLayout,
        QgsProject,
        QgsRasterLayer,
        QgsRectangle,
        QgsRendererCategory,
        QgsSingleBandGrayRenderer,
        QgsVectorFileWriter,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import QDate, QDateTime, QPointF, Qt
    from qgis.PyQt.QtGui import QFont, QFontDatabase, QPolygonF

    if action != 'native_scene_export':
        raise ValueError('Unknown final scene export action')
    plan_path = context.read_path(payload['plan_path'])
    if _sha(plan_path) != payload['plan_sha256']:
        raise ValueError('Frozen native scene changed before project export')
    plan = json.loads(plan_path.read_text('utf-8'))
    plan['colors'] = frozen_palette(plan)
    if plan_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('Native scene plan too large')
    for locator, expected in plan['source_inventory'].items():
        if _sha(context.read_path(locator)) != expected:
            raise ValueError('Source changed after image review')
    font_path = context.read_path(plan['font']['path'])
    if _sha(font_path) != plan['font']['sha256']:
        raise ValueError('Font changed after native review')
    if QFontDatabase.addApplicationFont(str(font_path)) < 0:
        raise ValueError('Reviewed font is unavailable in QGIS')
    scene = plan['scene']
    project = QgsProject.instance()
    project.setCrs(QgsCoordinateReferenceSystem(scene['crs']))
    layers, snapshots, checks = {}, [], {}
    data_dir = context.run_dir / 'data'
    data_dir.mkdir()

    def symbol(spec, kind, color=None):
        args = {'color': plan['colors'][color or spec['color']],
            'outline_color': plan['colors'][spec['outline']],
            'outline_width': str(spec['width_mm']), 'outline_width_unit': 'MM'}
        if kind == 'point':
            args.update(name='circle', size=str(spec['size_mm']), size_unit='MM')
            result = QgsMarkerSymbol.createSimple(args)
        elif kind == 'line':
            result = QgsLineSymbol.createSimple({
                'line_color': plan['colors'][color or spec['color']],
                'line_width': str(spec['width_mm']), 'line_width_unit': 'MM'})
        else:
            result = QgsFillSymbol.createSimple(args)
        expected = tuple(int(v) for v in plan['colors'][color or spec['color']].split(','))
        if result.color().getRgb() != expected:
            raise ValueError('QGIS symbol color differs from frozen RGBA')
        return result

    def fingerprint(layer, fields):
        rows = []
        for feature in layer.getFeatures():
            geometry = bytes(feature.geometry().asWkb()).hex()
            values = [str(feature[name]) for name in fields]
            rows.append(hashlib.sha256(json.dumps([geometry, values],
                ensure_ascii=False).encode()).hexdigest())
        return hashlib.sha256(''.join(sorted(rows)).encode()).hexdigest()

    def renderer_identity(layer):
        renderer = layer.renderer()
        if isinstance(renderer, QgsCategorizedSymbolRenderer):
            return {'field': renderer.classAttribute(), 'categories': [
                [str(c.value()), c.label(), c.symbol().symbolLayer(0).properties()]
                for c in renderer.categories()]}
        return renderer.symbol().symbolLayer(0).properties()

    for index, source in enumerate(plan['layers']):
        original = context.read_path(source['file_path'])
        if source['kind'] == 'raster':
            from osgeo import gdal

            destination = data_dir / (str(index) + '.tif')
            snapshot = gdal.Translate(str(destination), str(original), format='GTiff',
                                      creationOptions=['COMPRESS=LZW'])
            if snapshot is None:
                raise ValueError('Could not snapshot raster dependencies')
            snapshot = None
            layer = QgsRasterLayer(str(destination), source['name'] or source['id'])
            display = source['raster_display']
            renderer = QgsSingleBandGrayRenderer(layer.dataProvider(), display['band'])
            enhancement = QgsContrastEnhancement(layer.dataProvider().dataType(display['band']))
            enhancement.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
            enhancement.setMinimumValue(display['minimum'])
            enhancement.setMaximumValue(display['maximum'])
            renderer.setContrastEnhancement(enhancement)
            layer.setRenderer(renderer)
        else:
            uri = str(original) + ('|layername=' + source['source_layer']
                                  if source['source_layer'] else '')
            layer = QgsVectorLayer(uri, source['name'] or source['id'], 'ogr')
            if not layer.isValid():
                raise ValueError('Scene source layer invalid: ' + source['id'])
            if source['select_keys'] is not None:
                expr = QgsExpression.quotedColumnRef(source['key']) + ' IN (' + ','.join(
                    QgsExpression.quotedValue(v) for v in source['select_keys']) + ')'
                if not source['select_keys']:
                    expr = '1=0'
                if not layer.setSubsetString(expr):
                    raise ValueError('Could not apply frozen entity selection')
            fields = [f.name() for f in layer.fields()]
            original_layer = layer
            destination = data_dir / f'{index}.gpkg'
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName, options.layerName = 'GPKG', 'features'
            error = QgsVectorFileWriter.writeAsVectorFormatV3(layer, str(destination),
                project.transformContext(), options)
            if error[0] != QgsVectorFileWriter.NoError:
                raise ValueError('Could not snapshot frozen source: ' + str(error))
            layer = QgsVectorLayer(str(destination) + '|layername=features', layer.name(), 'ogr')
            if layer.featureCount() != source['feature_count']:
                raise ValueError('Final project entity selection differs from reviewed scene')
            validate_snapshot(original_layer, layer, fields, source['id'])
            # Once exported, reopening must match the snapshot exactly; the
            # narrow numeric tolerance is only for crossing the file providers.
            expected_features = fingerprint(layer, fields)
            kind = {0: 'point', 1: 'line', 2: 'polygon'}.get(int(layer.geometryType()))
            if kind is None:
                raise ValueError('Unsupported geometry for editable scene')
            style = source['symbol']
            if style['categories']:
                field = layer.fields().field(style['field'])

                def category_native(value):
                    if value is None or not isinstance(value, str):
                        return value
                    if field.typeName().lower() in {'datetime', 'timestamp'}:
                        parsed = QDateTime.fromString(value, Qt.ISODate)
                        if not parsed.isValid():
                            raise ValueError('Invalid frozen datetime category: ' + value)
                        return parsed
                    if field.typeName().lower() == 'date':
                        return QDate.fromString(value[:10], Qt.ISODate)
                    return value

                renderer = QgsCategorizedSymbolRenderer(style['field'], [QgsRendererCategory(
                    category_native(c['value']), symbol(style, kind, c['color']), c['label'])
                    for c in style['categories']])
                layer.setRenderer(renderer)
            else:
                layer.renderer().setSymbol(symbol(style, kind))
            layer.setOpacity(style['opacity'])
        if not layer.isValid():
            raise ValueError('Final scene layer invalid: ' + source['id'])
        layer.setOpacity(source['symbol']['opacity'])
        layer.setCustomProperty('geoharness/scene_layer_id', source['id'])
        checks[source['id']] = ({'fields': fields, 'features': expected_features,
            'renderer': renderer_identity(layer)} if source['kind'] == 'vector' else {})
        project.addMapLayer(layer)
        layers[source['id']] = layer
        snapshots.append({'id': source['id'], 'kind': source['kind'],
            'file': destination.relative_to(context.run_dir).as_posix(),
            'name': layer.name(), 'crs': layer.crs().authid(),
            'feature_count': source.get('feature_count'), 'symbol': source['symbol']})
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName('Reviewed native map')
    first = layout.pageCollection().page(0)
    first.setPageSize(QgsLayoutSize(scene['width_mm'], scene['height_mm']))
    for _ in plan['pages'][1:]:
        page = QgsLayoutItemPage(layout)
        page.setPageSize(QgsLayoutSize(scene['width_mm'], scene['height_mm']))
        layout.pageCollection().addPage(page)
    font = QFont(plan['font']['family'])
    for index, page in enumerate(plan['pages']):
        frame = page.get('frame')
        if frame:
            item = QgsLayoutItemMap(layout)
            layout.addLayoutItem(item)
            x, y, width, height = frame['rect_mm']
            item.attemptMove(QgsLayoutPoint(x, y), page=index)
            item.attemptResize(QgsLayoutSize(width, height))
            item.setCrs(QgsCoordinateReferenceSystem(frame['crs']))
            item.setLayers([layers[key] for key in reversed(frame['layers'])])
            item.setKeepLayerSet(True)
            item.zoomToExtent(QgsRectangle(*frame['extent']))
            item.setId(page['page_id'])
        for i, text in enumerate(page['texts']):
            item = QgsLayoutItemLabel(layout)
            item.setText(text['text'])
            font.setPointSizeF(text['size_pt'])
            item.setFont(font)
            item.setMargin(0)
            layout.addLayoutItem(item)
            item.attemptMove(QgsLayoutPoint(text['x_mm'], text['y_mm']), page=index)
            item.attemptResize(QgsLayoutSize(text['width_mm'] + .5, text['height_mm'] + .5))
            item.setId(f"{page['page_id']}:text:{i}")
        for entry in page.get('legend_items', []):
            item = QgsLayoutItemShape(layout)
            kind = entry['kind']
            item.setShapeType(QgsLayoutItemShape.Ellipse if kind == 'point'
                              else QgsLayoutItemShape.Rectangle)
            fill = plan['colors'][entry['color']]
            item.setSymbol(QgsFillSymbol.createSimple({'color': fill,
                'outline_color': plan['colors'][entry['symbol']['outline']],
                'outline_width': '.15'}))
            item.setItemOpacity(entry['symbol']['opacity'])
            x, y, w, h = entry['rect_mm']
            if kind == 'point':
                w = h = entry['symbol']['size_mm']
            elif kind == 'line':
                h = max(.1, entry['symbol']['width_mm'])
            layout.addLayoutItem(item)
            item.attemptMove(QgsLayoutPoint(x, y), page=index)
            item.attemptResize(QgsLayoutSize(w, h))
        for segment in page.get('lines', []):
            points = QPolygonF([QPointF(*point) for point in segment['points_mm']])
            line = QgsLayoutItemPolyline(points, layout)
            line.setSymbol(QgsLineSymbol.createSimple({'line_color': '#000000',
                'line_width': str(segment['width_mm']), 'line_width_unit': 'MM'}))
            layout.addLayoutItem(line)
            # Absolute paper coordinates are already encoded in its nodes;
            # translate only by this page's top-left position.
            line.attemptMove(QgsLayoutPoint(line.positionWithUnits().x(),
                line.positionWithUnits().y()), page=index)
    project.layoutManager().addLayout(layout)
    from qgis.core import Qgis

    project.setFilePathStorage(Qgis.FilePathType.Relative)
    destination = context.run_dir / 'map.qgz'
    project.setFileName(str(destination))
    if not project.write():
        raise ValueError('Final QGIS project write failed')
    project.clear()
    reopened = QgsProject()
    try:
        if not reopened.read(str(destination)) or not all(v.isValid()
                for v in reopened.mapLayers().values()):
            raise ValueError('Final QGIS project could not reopen with valid data')
        saved = reopened.layoutManager().printLayouts()
        if len(saved) != 1 or saved[0].pageCollection().pageCount() != len(plan['pages']):
            raise ValueError('Final QGIS layout pages differ from native scene')
        actual = {layer.customProperty('geoharness/scene_layer_id'): layer
                  for layer in reopened.mapLayers().values()}
        if set(actual) != set(layers):
            raise ValueError('Final scene layer membership changed after reopening')
        for source in plan['layers']:
            layer, check = actual[source['id']], checks[source['id']]
            if abs(layer.opacity() - source['symbol']['opacity']) > 1e-6:
                raise ValueError('Final layer opacity differs from native scene')
            if check and (fingerprint(layer, check['fields']) != check['features']
                          or renderer_identity(layer) != check['renderer']):
                raise ValueError('Final geometry, attributes or styles changed after reopening')
        for index, page in enumerate(plan['pages']):
            frame = page.get('frame')
            if frame:
                item = saved[0].itemById(page['page_id'])
                if item is None or item.page() != index:
                    raise ValueError('Final map frame missing or on wrong page')
                rect = frame['rect_mm']
                actual_rect = [item.pagePos().x(), item.pagePos().y(),
                               item.sizeWithUnits().width(), item.sizeWithUnits().height()]
                extent = item.extent()
                actual_extent = [extent.xMinimum(), extent.yMinimum(),
                                 extent.xMaximum(), extent.yMaximum()]
                if (any(abs(a-b) > .001 for a, b in zip(actual_rect, rect))
                        or any(abs(a-b) > max(1e-5, abs(b)*1e-8)
                               for a, b in zip(actual_extent, frame['extent']))
                        or [v.customProperty('geoharness/scene_layer_id')
                            for v in item.layers()] != list(reversed(frame['layers']))):
                    raise ValueError('Final frame geometry, extent or layer order differs')
            for i, text in enumerate(page['texts']):
                label = saved[0].itemById(f"{page['page_id']}:text:{i}")
                if (label is None or label.text() != text['text'] or label.page() != index
                        or abs(label.pagePos().x() - text['x_mm']) > .001
                        or abs(label.pagePos().y() - text['y_mm']) > .001):
                    raise ValueError('Final editable text differs from frozen scene')
    finally:
        reopened.clear()
    if any(_sha(context.read_path(path)) != value for path, value in (
            plan['source_inventory'].items())):
        raise ValueError('Source changed during final export')
    report = {'status': 'passed',
        'scope': 'source_hashes_geometry_attributes_styles_page_frames_editable_text',
        'pixel_equivalence': 'not_claimed', 'scene_plan_sha256': payload['plan_sha256'],
        'project_file': str(destination), 'layers': snapshots, 'pages': len(plan['pages'])}
    (context.run_dir / 'scene-export-check.json').write_text(json.dumps(report,
        ensure_ascii=False), 'utf-8')
    return report
