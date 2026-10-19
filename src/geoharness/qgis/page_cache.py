"""Conservative per-page PNG reuse for system-created, fully bound QGIS layouts.

Saved projects and PDFs are still produced from the current complete layout.
Unknown or dynamic dependencies disable reuse instead of certifying old pixels.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(node):
    # QGIS gives symbol layers fresh UUIDs when recreating identical 2D styles.
    # Item/layer references have already been replaced with stable identities.
    attrs = {}
    for key, value in node.attrib.items():
        if key in {'uuid', 'templateUuid', 'template_uuid', 'groupUuid'}:
            continue
        if (key == 'id' and node.tag == 'layer'
                and re.fullmatch(r'\{[0-9a-fA-F-]{36}\}', value)):
            continue
        if value.lstrip().startswith('<'):
            try:
                # Callout symbols are stored as XML inside an XML attribute.
                value = _canonical(ET.fromstring(value))
            except ET.ParseError:
                pass
        attrs[key] = value
    children = [_canonical(child) for child in node
             # Only system-created 2D map items use this cache. Default profile
             # colors are random and are never rendered by these items.
                if not (node.tag == 'qgis' and child.tag == 'elevation')]
    if node.tag == 'LayerStyles':
        # Style lookup is keyed by layer ID; QMap UUID sort order is random.
        # LayerSet (actual map drawing order) remains ordered and is hashed.
        children.sort(key=lambda value: json.dumps(value, sort_keys=True))
    return [node.tag, sorted(attrs.items()), (node.text or '').strip(), children]


def page_key(layout, page, payload, context, tables):
    if not payload.get('_layout_sources') or not payload.get('_creation_spec'):
        return None
    from qgis.core import QgsLayoutItem, QgsReadWriteContext
    from qgis.PyQt.QtXml import QDomDocument

    from .project_worker import _font_identities, _project_layers, _runtime_identity

    project = layout.project()
    layers = _project_layers(project)
    items = [item for item in layout.items() if isinstance(item, QgsLayoutItem)]
    items.sort(key=lambda item: (item.type(), item.id(), item.page(),
                                 item.scenePos().x(), item.scenePos().y(), item.zValue()))
    replacements = {item.uuid(): f'item:{index}' for index, item in enumerate(items)}
    for index, layer in enumerate(layers):
        replacements[layer.id()] = f'layer:{index}'
        replacements[layer.source()] = f'source:{index}'
    # The layout name does not affect page pixels. Each relevant item and shared
    # layer style is serialized by QGIS itself; no guessed subset of style keys.
    document = QDomDocument('page')
    root = document.createElement('page')
    document.appendChild(root)
    rw = QgsReadWriteContext()
    for item in items:
        if item.page() == page:
            item.writeXml(root, document, rw)
    for table in layout.multiFrames():
        if any(frame.page() == page for frame in table.frames()):
            table.writeXml(root, document, rw)
            if hasattr(table, 'uuid'):
                replacements[table.uuid()] = 'table:' + str(table.customProperty(
                    'geoharness/table_id', ''))
    for layer in layers:
        node = document.createElement('layer')
        layer.writeLayerXml(node, document, rw)
        root.appendChild(node)
    text = document.toString()
    # Time/randomness, external resources or unbound expressions are not stable.
    if re.search(r'\[%|\b(?:now|rand|randf|uuid|current_date)\s*\(', text, re.I):
        return None
    for old, replacement in sorted(replacements.items(), key=lambda p: -len(p[0])):
        if old:
            text = text.replace(old, replacement)
    text = text.replace(str(context.run_dir), '<current-package>')
    element = ET.fromstring(text)

    facts = {'schema': 3, 'page': page, 'dpi': payload.get('dpi', 150),
             'runtime': _runtime_identity(), 'xml': _canonical(element),
             'sources': payload['_layout_sources'],
             # Exporting one page can temporarily load a map style override.
             # Keep the validated shared style contract as a second dependency,
             # so page-export order cannot hide a changed shared symbol/label.
             'layer_parameters': [
                 {key: value for key, value in layer.items() if key != 'file_path'}
                 for layer in payload.get('layers', [])],
             'fonts': _font_identities(layout, layers, context),
             'tables': [table for table in tables if table['page'] == page]}
    return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def recall_page(key, target, payload, context):
    if key is None:
        return None
    for entry in payload.get('_page_reuse', []):
        if entry.get('content_key') != key:
            continue
        try:
            path = context.read_path(entry['path'])
            if path.suffix.lower() != '.png' or _sha(path) != entry['sha256']:
                continue
            shutil.copy2(path, target)
            if _sha(target) != entry['sha256']:
                target.unlink()
                continue
            return entry.get('labels', {})
        except (OSError, KeyError, ValueError, PermissionError):
            continue
    return None


def remember_pages(metadata, result, map_id, *, limit=128):
    if result.is_error or result.metadata.get('receipt_status') == 'failed':
        return
    cache = metadata.setdefault('geo_page_reuse', {}).setdefault(map_id, {})
    native = result.metadata.get('native_facts') or {}
    for entry in native.get('page_previews', []):
        if not entry.get('content_key'):
            continue
        path = Path(entry['path'])
        if path.is_file():
            cache[entry['content_key']] = {'content_key': entry['content_key'],
                'path': str(path), 'sha256': _sha(path), 'labels': entry.get('labels', {})}
    for key in list(cache)[:-limit]:
        cache.pop(key)
    for key in list(metadata['geo_page_reuse'])[:-8]:
        metadata['geo_page_reuse'].pop(key)
