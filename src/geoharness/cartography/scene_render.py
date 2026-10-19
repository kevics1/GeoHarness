"""Resolve and draw a frozen scene with Agg. This module never imports PyQGIS."""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path
from uuid import uuid4

from geoharness.cartography.scene import digest, directory

_DRAW_LOCK = threading.RLock()  # Matplotlib's font/layout state is process-global.


def _sha(path):
    import hashlib

    value = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def _sources(scene, context):
    import geopandas as gpd

    from geoharness.analysis.result_package import (
        _source_inventory,
        infer_packages,
        require_candidate_bindings,
    )
    from geoharness.cartography.facts import infer_analysis_manifests, load_analysis_facts
    from geoharness.data.artifacts import assert_usable_artifact
    from geoharness.data.task_context import resolve_registered_path
    from geoharness.execution.contracts import validate_vector
    from geoharness.qgis.worker_context import WorkerContext
    from geoharness.tools.geo_qgis import qgis_read_roots

    config = context.metadata['geoharness_config']
    roots = qgis_read_roots(config, context.cwd)
    guard = WorkerContext(context.cwd, context.cwd, roots)
    frames, layers, inventory, mapped = {}, [], {}, []
    for source in scene.layers:
        path = guard.read_path(resolve_registered_path(source.source_ref, context.metadata,
            cwd=context.cwd, config=config))
        assert_usable_artifact(path)
        layer_inventory = ({str(path): _sha(path)} if path.suffix.lower() == '.gpkg'
                           else _source_inventory(path))
        inventory.update(layer_inventory)
        packed = source.model_dump(mode='json') | {'file_path': str(path),
                                                  'source_inventory': layer_inventory}
        if source.kind == 'raster':
            import rasterio

            with rasterio.open(path) as raster:
                import numpy as np

                if raster.crs is None:
                    raise ValueError('栅格缺少CRS：' + source.id)
                for dependency in raster.files:
                    locator = guard.read_path(dependency)
                    layer_inventory[str(locator)] = _sha(locator)
                inventory.update(layer_inventory)
                packed['crs'] = str(raster.crs)
                values = raster.read(1, masked=True,
                    out_shape=(min(1024, raster.height), min(1024, raster.width))).compressed()
                values = values[np.isfinite(values)]
                if not len(values):
                    raise ValueError('栅格没有可显示的有限像元：' + source.id)
                packed['raster_display'] = {'band': 1, 'minimum': float(values.min()),
                    'maximum': float(values.max()), 'gradient': 'black_to_white',
                    'scope': 'sampled_display_stretch_only; not_analysis'}
            layers.append(packed)
            continue
        if path.suffix.lower() == '.gpkg':
            import pyogrio

            names = list(pyogrio.list_layers(path)[:, 0])
            if (len(names) > 1 and not source.source_layer
                    or source.source_layer and source.source_layer not in names):
                raise ValueError(f'图层{source.id}需指定真实source_layer；可用：{names}')
        original = gpd.read_file(path, **({'layer': source.source_layer}
                                         if source.source_layer else {}))
        validate_vector(original)
        frame = original
        required = [v for v in [source.key, source.label_field, source.symbol.field] if v]
        if set(required) - set(frame.columns):
            raise ValueError(f'图层{source.id}字段不存在；实际字段：{list(frame.columns)}')
        if source.key and (frame[source.key].isna().any() or not frame[source.key].is_unique):
            raise ValueError('实体key必须唯一非空：' + source.id)
        derivation = None
        if source.select_keys is not None:
            if not source.key:
                raise ValueError('select_keys必须声明唯一实体key')
            frame = frame[frame[source.key].isin(source.select_keys)]
            if set(source.select_keys) - set(frame[source.key]):
                raise ValueError('select_keys包含不存在的实体，不能静默忽略')
            derivation = {'feature_indices': [int(i) for i in range(len(original))
                if original.iloc[i][source.key] in source.select_keys]}
        if source.label_field and not source.key:
            raise ValueError('标注必须声明唯一实体key以绑定文字和真实对象')
        if source.symbol.categories:
            if not source.symbol.field:
                raise ValueError('categories必须声明真实field')
            declared = [v.value for v in source.symbol.categories]
            if len({json.dumps(v, sort_keys=True) for v in declared}) != len(declared):
                raise ValueError('分类值不能重复')
            for value in frame[source.symbol.field]:
                import pandas as pd

                if not any((pd.isna(value) and c is None) or value == c for c in declared):
                    raise ValueError(f'图层{source.id}分类未声明：{value!r}')
        frames[source.id] = frame.to_crs(scene.crs)
        packed.update(crs=str(original.crs), feature_count=len(frame),
            source_feature_count=len(original), data_sha256=digest(layer_inventory))
        layers.append(packed)
        mapped.append({'file_path': str(path), 'gdf': frame,
            **({'derivation': derivation} if derivation else {})})
    refs = list(dict.fromkeys([*scene.analysis_refs, *infer_packages(context.metadata, mapped),
        *infer_analysis_manifests(mapped, cwd=context.cwd, read_roots=roots)]))
    if any(Path(value).suffix.lower() in {'.md', '.txt'} for value in refs):
        raise ValueError('analysis_refs需使用publish_analysis返回的analysis_ref/package_path；'
                         'report_path是给人阅读的Markdown，不能作为JSON分析契约。')
    # Prefer explicit published result packages over their producer manifests.
    facts = load_analysis_facts(refs, cwd=context.cwd, read_roots=roots,
                               mapped_sources=mapped, display_crs=scene.crs)
    require_candidate_bindings(context.metadata, mapped, facts)
    if context.metadata.get('geo_task_record', {}).get('analysis_requirement') == 'required':
        if facts.get('status') != 'verified':
            raise ValueError('制图前必须发布并绑定当前空间分析结果；先geo_task publish_analysis')
    return frames, layers, inventory, facts


def _bounds(page, frames, scene):
    import numpy as np

    if page.extent:
        return list(page.extent)
    selected = page.focus_layers or page.layers or list(frames)
    extents = [frames[key].total_bounds for key in selected if key in frames and len(frames[key])]
    if not extents:
        raise ValueError('地图范围无非空矢量，请指定真实extent')
    array = np.array(extents)
    low, high = array[:, :2].min(axis=0), array[:, 2:].max(axis=0)
    from pyproj import CRS

    minimum = .0001 if CRS.from_user_input(scene.crs).is_geographic else 1
    span = np.maximum(high - low, minimum)
    return [*(low - span * .05), *(high + span * .05)]


def _colors(layer, frame):
    import pandas as pd

    categories = layer['symbol']['categories']
    if not categories:
        return [layer['symbol']['color']] * len(frame)
    return [next(c['color'] for c in categories if (pd.isna(v) and c['value'] is None)
        or v == c['value']) for v in frame[layer['symbol']['field']]]


def _rgba(color, opacity=1):
    from matplotlib.colors import to_rgba

    r, g, b, a = to_rgba(color)
    return r, g, b, a * opacity


def _page_text(fig, text, x, y, size, font, scene, resolved, *, width=None):
    if not text:
        return
    artist = fig.text(x / scene.width_mm, 1 - y / scene.height_mm, text,
        fontsize=size, fontfamily=font, ha='left', va='top')
    bbox = artist.get_window_extent(fig.canvas.get_renderer())
    scale = 25.4 / scene.dpi
    box = [bbox.x0 * scale, scene.height_mm - bbox.y1 * scale,
           bbox.width * scale, bbox.height * scale]
    resolved['texts'].append({'text': text, 'x_mm': box[0], 'y_mm': box[1],
        'width_mm': box[2], 'height_mm': box[3], 'size_pt': size, 'font': font})
    if box[0] < 0 or box[1] < 0 or box[0] + box[2] > scene.width_mm or (
            box[1] + box[3] > scene.height_mm) or width and box[2] > width:
        resolved['problems'].append({'code': 'text_overflow', 'text': text[:100], 'bbox_mm': box})


def _draw_map(fig, page, frames, layers, scene, font, resolved):
    from matplotlib.lines import Line2D
    from matplotlib.patches import Ellipse, Rectangle

    box = [9, 25, scene.width_mm - 69, scene.height_mm - 45]
    if box[2] <= 30 or box[3] <= 30:
        raise ValueError('纸张太小，无法布置图框和图例')
    ax = fig.add_axes([box[0] / scene.width_mm,
        1 - (box[1] + box[3]) / scene.height_mm,
        box[2] / scene.width_mm, box[3] / scene.height_mm])
    extent = _bounds(page, frames, scene)
    ids = page.layers or [item['id'] for item in layers]
    ordered_layers = [next(layer for layer in layers if layer['id'] == key) for key in ids]
    legend_items = []
    for draw_index, layer in enumerate(ordered_layers, start=1):
        if layer['kind'] == 'raster':
            import rasterio
            from rasterio.warp import Resampling, calculate_default_transform, reproject

            with rasterio.open(layer['file_path']) as src:
                import numpy as np

                transform, width, height = calculate_default_transform(src.crs, scene.crs,
                    src.width, src.height, *src.bounds)
                limit = max(1, math.ceil(max(scene.width_mm, scene.height_mm) * scene.dpi / 25.4))
                if max(width, height) > limit:
                    ratio = max(width, height) / limit
                    from affine import Affine

                    transform = transform * Affine.scale(ratio)
                    width = max(1, math.ceil(width / ratio))
                    height = max(1, math.ceil(height / ratio))
                data = np.full((height, width), np.nan, dtype='float32')
                reproject(rasterio.band(src, 1), data, src_transform=src.transform,
                    src_crs=src.crs, dst_transform=transform, dst_crs=scene.crs,
                    dst_nodata=np.nan, resampling=Resampling.nearest)
                bounds = rasterio.transform.array_bounds(height, width, transform)
                display = layer['raster_display']
                ax.imshow(data, extent=[bounds[0], bounds[2], bounds[1], bounds[3]],
                    origin='upper', cmap='gray', vmin=display['minimum'], vmax=display['maximum'],
                    alpha=layer['symbol']['opacity'], zorder=draw_index)
            continue
        frame, symbol = frames[layer['id']], layer['symbol']
        if not len(frame):
            continue
        kind = frame.geometry.geom_type.iloc[0]
        color = _colors(layer, frame)
        # Collection alpha overrides transparent RGBA entries (including 'none').
        # Apply opacity to each real color without turning hollow polygons black.
        color = [_rgba(c, symbol['opacity']) for c in color]
        kwargs = {'ax': ax, 'color': color, 'zorder': draw_index,
            'linewidth': symbol['width_mm'] * 72 / 25.4, 'aspect': None}
        if 'Point' in kind:
            kwargs.update(markersize=(symbol['size_mm'] * 72 / 25.4) ** 2,
                          edgecolor=_rgba(symbol['outline'], symbol['opacity']))
        elif 'Polygon' in kind:
            kwargs['edgecolor'] = _rgba(symbol['outline'], symbol['opacity'])
        frame.plot(**kwargs)
        legend = symbol['categories'] or [{'label': layer['name'] or layer['id'],
                                           'color': symbol['color']}]
        for entry in legend:
            legend_items.append({'label': entry['label'], 'color': entry['color'],
                'kind': 'point' if 'Point' in kind else 'line' if 'Line' in kind else 'polygon',
                'symbol': symbol})
    ax.set_xlim(extent[0], extent[2])
    ax.set_ylim(extent[1], extent[3])
    ax.set_aspect('equal', adjustable='box')
    ax.set_xticks([])
    ax.set_yticks([])
    fig.canvas.draw()
    actual = ax.get_position()
    resolved['frame'] = {'id': page.id, 'crs': scene.crs, 'extent': extent, 'layers': ids,
        'rect_mm': [actual.x0 * scene.width_mm, (1 - actual.y1) * scene.height_mm,
                    actual.width * scene.width_mm, actual.height * scene.height_mm]}
    resolved['legend_items'] = []
    for i, entry in enumerate(legend_items):
        x, y = scene.width_mm - 55, 26 + i * 6
        if y > scene.height_mm - 23:
            resolved['problems'].append({'code': 'legend_overflow'})
        _page_text(fig, entry['label'], x + 7, y, 8, font, scene, resolved, width=47)
        entry.update(rect_mm=[x, y, 5, 3])
        resolved['legend_items'].append(entry)
        if entry['kind'] == 'point':
            artist = Ellipse(((x + 2.5) / scene.width_mm, 1 - (y + 1.5) / scene.height_mm),
                width=entry['symbol']['size_mm'] / scene.width_mm,
                height=entry['symbol']['size_mm'] / scene.height_mm,
                transform=fig.transFigure,
                facecolor=_rgba(entry['color'], entry['symbol']['opacity']),
                edgecolor=_rgba(entry['symbol']['outline'], entry['symbol']['opacity']))
        elif entry['kind'] == 'line':
            artist = Line2D([x / scene.width_mm, (x + 5) / scene.width_mm],
                [1 - (y + 1.5) / scene.height_mm] * 2, transform=fig.transFigure,
                color=_rgba(entry['color'], entry['symbol']['opacity']),
                linewidth=entry['symbol']['width_mm'] * 72 / 25.4)
        else:
            artist = Rectangle((x / scene.width_mm, 1 - (y + 3) / scene.height_mm),
                5 / scene.width_mm, 3 / scene.height_mm, transform=fig.transFigure,
                facecolor=_rgba(entry['color'], entry['symbol']['opacity']),
                edgecolor=_rgba(entry['symbol']['outline'], entry['symbol']['opacity']))
        fig.add_artist(artist)
    # Freeze positions after collision avoidance. Exporter uses these layout labels,
    # not a second engine's automatic label placement.
    occupied = []
    for layer in ordered_layers:
        if not layer['label_field'] or layer['id'] not in frames:
            continue
        frame = frames[layer['id']]
        for _, row in frame.iterrows():
            point = row.geometry.representative_point()
            if not (extent[0] <= point.x <= extent[2] and extent[1] <= point.y <= extent[3]):
                continue
            text = str(row[layer['label_field']])
            chosen = None
            for dx, dy in [(4, 4), (4, -12), (-12, 4), (-12, -12),
                           (12, 10), (-20, 10), (12, -20), (-20, -20)]:
                item = ax.annotate(text, (point.x, point.y), xytext=(dx, dy),
                    textcoords='offset points', fontsize=layer['label_size_pt'],
                    fontfamily=font, ha='left', va='bottom', zorder=len(ordered_layers) + 2,
                    bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': .8, 'pad': .2})
                b = item.get_window_extent(fig.canvas.get_renderer()).expanded(1.12, 1.15)
                if ax.bbox.contains(b.x0, b.y0) and ax.bbox.contains(b.x1, b.y1) and not any(
                        b.overlaps(previous) for previous in occupied):
                    chosen = (item, b)
                    break
                item.remove()
            if chosen is None:
                resolved['problems'].append({'code': 'label_collision', 'layer': layer['id'],
                    'entity_key': str(row[layer['key']]), 'text': text,
                    'next_action': '拆分局部详图或扩大图框，保留真实坐标'})
                continue
            item, b = chosen
            occupied.append(b)
            anchor = ax.transData.transform((point.x, point.y))
            scale = 25.4 / scene.dpi
            label = {'text': text, 'layer': layer['id'], 'entity_key': str(row[layer['key']]),
                'x_mm': b.x0 * scale, 'y_mm': scene.height_mm - b.y1 * scale,
                'width_mm': b.width * scale, 'height_mm': b.height * scale,
                'size_pt': layer['label_size_pt'], 'font': font,
                'anchor_mm': [anchor[0] * scale, scene.height_mm - anchor[1] * scale]}
            resolved['texts'].append(label)
    from pyproj import CRS

    crs = CRS.from_user_input(scene.crs)
    span = extent[2] - extent[0]
    if crs.is_projected:
        metres = span * crs.axis_info[0].unit_conversion_factor
    else:
        _, _, metres = crs.get_geod().inv(extent[0], (extent[1] + extent[3]) / 2,
                                         extent[2], (extent[1] + extent[3]) / 2)
    target = metres / 5
    magnitude = 10 ** math.floor(math.log10(target))
    length = max(v * magnitude for v in (1, 2, 5) if v * magnitude <= target)
    width = length / metres * resolved['frame']['rect_mm'][2]
    x, y = 9, scene.height_mm - 18
    resolved['lines'] = [{'points_mm': [[x, y], [x + width, y]], 'width_mm': .6}]
    fig.add_artist(Line2D([x / scene.width_mm, (x + width) / scene.width_mm],
        [1 - y / scene.height_mm] * 2, transform=fig.transFigure, color='black',
        linewidth=.6 * 72 / 25.4))
    _page_text(fig, '0', x, y + 1, 7, font, scene, resolved)
    unit = f'{length / 1000:g} km' if length >= 1000 else f'{length:g} m'
    _page_text(fig, unit, x + width, y + 1, 7, font, scene, resolved)
    return extent


def _font(scene):
    from matplotlib import font_manager

    from geoharness.cartography.renderer import _CJK_FONTS, _configure_matplotlib

    _configure_matplotlib()
    available = {entry.name for entry in font_manager.fontManager.ttflist}
    font = scene.font_family or next((name for name in _CJK_FONTS if name in available), None)
    if not font:
        raise ValueError('没有可用中文字体；配置scene.font_family后制图')
    font_path = Path(font_manager.findfont(font, fallback_to_default=False))
    return font, font_path


def render_scene(scene, scene_ref, context):
    with _DRAW_LOCK:
        return _render(scene, scene_ref, context)


def _render(scene, scene_ref, context):
    import shutil

    import matplotlib.pyplot as plt
    from pypdf import PdfWriter


    frames, layers, inventory, facts = _sources(scene, context)
    implementation = _sha(__file__)
    font, font_path = _font(scene)
    key = digest({'scene': scene.model_dump(mode='json'), 'inventory': inventory,
                  'analysis': facts, 'renderer': implementation, 'font_sha256': _sha(font_path)})
    folder = directory(context) / ('render-' + key)
    manifest_path = folder / 'project.json'
    if manifest_path.is_file():
        doc = json.loads(manifest_path.read_text('utf-8'))
        if all((folder / p).is_file() and _sha(folder / p) == value
               for p, value in doc.get('file_hashes', {}).items()) and doc.get('file_hashes'):
            return _result(folder, doc, reused=True)
        raise ValueError('已有原生预览发生变化，保留证据；修改计划后重新渲染')
    folder.mkdir(parents=True, exist_ok=False)
    resolved_pages, previews, extents = [], [], []
    files = []
    try:
        with PdfWriter() as pdf:
            for page in scene.pages:
                table = None
                if page.table:
                    table = frames[page.table.layer]
                    if set(page.table.fields) - set(table.columns):
                        raise ValueError('名册字段不存在；实际字段：' + str(list(table.columns)))
                    if page.table.sort_by:
                        table = table.sort_values(page.table.sort_by, kind='stable')
                chunks = max(1, math.ceil(len(table) / page.table.rows_per_page)) if (
                    table is not None) else 1
                for chunk in range(chunks):
                    index = len(previews)
                    identity = page.id + (f'-{chunk + 1}' if chunks > 1 else '')
                    filename = 'preview.png' if index == 0 else f'page-{index + 1}.png'
                    selected_ids = ([page.table.layer] if page.table else page.layers or
                                    [layer['id'] for layer in layers])
                    selected_layers = [layer for layer in layers if layer['id'] in selected_ids]
                    page_key = digest({'page': page.model_dump(mode='json'), 'chunk': chunk,
                        'layers': selected_layers, 'sources': {k: v for k, v in inventory.items()
                            if any(k == layer['file_path'] or Path(k).stem == Path(
                                layer['file_path']).stem for layer in selected_layers)},
                        'crs': scene.crs, 'width': scene.width_mm, 'height': scene.height_mm,
                        'title': page.title or scene.title, 'dpi': scene.dpi,
                        'formats': scene.extra_formats,
                        'font_sha': _sha(font_path), 'implementation': implementation})
                    cache = directory(context) / 'page-cache' / page_key
                    record = cache / 'page.json'
                    if record.is_file() and not record.is_symlink() and (
                            record.stat().st_size < 4 * 1024 * 1024):
                        saved = json.loads(record.read_text('utf-8'))
                        if (saved.get('key') == page_key and all(not (cache / name).is_symlink()
                                and (cache / name).is_file() and _sha(cache / name) == expected
                                for name, expected in saved['files'].items())):
                            resolved = saved['resolved']
                            resolved['page_index'] = index
                            shutil.copy2(cache / 'page.png', folder / filename)
                            if 'svg' in scene.extra_formats:
                                name = filename.replace('.png', '.svg')
                                shutil.copy2(cache / 'page.svg', folder / name)
                                files.append(name)
                            pdf.append(cache / 'page.pdf')
                            resolved_pages.append(resolved)
                            if resolved.get('frame'):
                                extents.append(resolved['frame']['extent'])
                            previews.append({'page_id': identity, 'page_index': index,
                                'path': filename, 'role': page.role, 'content_key': page_key,
                                'frames': [page.id] if resolved['frame'] else [], 'reused': True})
                            files.append(filename)
                            continue
                    fig = plt.figure(figsize=(scene.width_mm / 25.4, scene.height_mm / 25.4),
                                     dpi=scene.dpi, facecolor='white')
                    try:
                        resolved = {'page_id': identity, 'page_index': index, 'role': page.role,
                            'texts': [], 'problems': [], 'frame': None, 'table': None}
                        _page_text(fig, page.title or scene.title, 9, 8, 13, font, scene,
                                   resolved, width=scene.width_mm - 18)
                        if table is not None:
                            import pandas as pd

                            start = chunk * page.table.rows_per_page
                            data = table.iloc[start:start + page.table.rows_per_page]
                            from numbers import Real

                            def cell(value, field):
                                if pd.isna(value):
                                    return '无统计'
                                if isinstance(value, Real) and not isinstance(value, bool):
                                    n = page.table.decimals.get(field, 2)
                                    result = f'{value:.{n}f}'
                                    return result.rstrip('0').rstrip('.') if n else result
                                return str(value)

                            rows = [[cell(row[f], f)
                                     for f in page.table.fields] for _, row in data.iterrows()]
                            headers = [page.table.headers.get(f, f) for f in page.table.fields]
                            all_rows = [headers, *rows]
                            w = (scene.width_mm - 18) / len(headers)
                            h = min(7, (scene.height_mm - 42) / len(all_rows))
                            for ri, values in enumerate(all_rows):
                                for ci, value in enumerate(values):
                                    _page_text(fig, value, 10 + ci * w, 27 + ri * h, 8,
                                               font, scene, resolved, width=w - 2)
                            resolved['table'] = {'layer': page.table.layer,
                                'fields': page.table.fields, 'rows': rows, 'headers': headers,
                                'row_start': start, 'row_count': len(rows), 'total': len(table),
                                'rect_mm': [9, 25, scene.width_mm - 18, len(all_rows) * h]}
                        else:
                            extents.append(_draw_map(fig, page, frames, layers, scene,
                                                     font, resolved))
                        footer = page.footer
                        _page_text(fig, footer, 65, scene.height_mm - 14, 7, font, scene,
                                   resolved, width=scene.width_mm - 74)
                        fig.savefig(folder / filename, dpi=scene.dpi,
                                    metadata={'Software': 'GeoHarness native scene'})
                        cache.mkdir(parents=True, exist_ok=True)
                        if 'svg' in scene.extra_formats:
                            name = filename.replace('.png', '.svg')
                            fig.savefig(folder / name, format='svg', metadata={'Date': None})
                            shutil.copy2(folder / name, cache / 'page.svg')
                            files.append(name)
                        fig.savefig(cache / 'page.pdf', format='pdf',
                                    metadata={'CreationDate': None, 'ModDate': None})
                        pdf.append(cache / 'page.pdf')
                    finally:
                        plt.close(fig)
                    resolved_pages.append(resolved)
                    previews.append({'page_id': identity, 'page_index': index,
                        'path': filename, 'role': page.role,
                        'frames': [page.id] if resolved['frame'] else [],
                        'content_key': page_key, 'reused': False})
                    files.append(filename)
                    shutil.copy2(folder / filename, cache / 'page.png')
                    from openharness.utils.fs import atomic_write_text

                    cached = ['page.png', 'page.pdf', *(['page.svg']
                              if 'svg' in scene.extra_formats else [])]
                    atomic_write_text(record, json.dumps({'key': page_key, 'resolved': resolved,
                        'files': {name: _sha(cache / name) for name in cached}},
                        ensure_ascii=False))
            pdf.write(folder / 'map.pdf')
        # Sources may change during a long draw; never register mismatched pixels.
        if any(_sha(path) != value for path, value in inventory.items()):
            raise ValueError('制图期间来源发生变化，请重新验证分析结果')
        if 'html' in scene.extra_formats:
            import folium

            first = next((p for p in resolved_pages if p['frame']), None)
            if first is None:
                raise ValueError('交互HTML需要至少一个地图页')
            from pyproj import Transformer

            bounds = Transformer.from_crs(scene.crs, 4326, always_xy=True).transform_bounds(
                *first['frame']['extent'])
            interactive = folium.Map(tiles=None)
            for layer in layers:
                if layer['id'] not in first['frame']['layers'] or layer['kind'] != 'vector':
                    continue
                frame = frames[layer['id']].to_crs(4326).copy()
                frame['_display_color'] = _colors(layer, frame)
                style = layer['symbol']
                folium.GeoJson(json.loads(frame.to_json()), name=layer['name'] or layer['id'],
                    marker=folium.CircleMarker(radius=style['size_mm'] * 2),
                    style_function=lambda feature, s=style: {
                        'color': s['outline'], 'weight': s['width_mm'] * 3,
                        'fillColor': feature['properties']['_display_color'],
                        'fillOpacity': s['opacity']}).add_to(interactive)
            interactive.fit_bounds([[bounds[1], bounds[0]], [bounds[3], bounds[2]]])
            folium.LayerControl().add_to(interactive)
            interactive.save(folder / 'map.html')
            files.append('map.html')
        problems = [p for page in resolved_pages for p in page['problems']]
        plan = {'schema_version': 1, 'scene_ref': scene_ref, 'scene': scene.model_dump(mode='json'),
            'layers': layers, 'pages': resolved_pages, 'source_inventory': inventory,
            'font': {'family': font, 'path': str(font_path), 'sha256': _sha(font_path)}}
        colors = {value for layer in layers for value in [layer['symbol']['color'],
            layer['symbol']['outline'], *(c['color'] for c in layer['symbol']['categories'])]}
        plan['colors'] = {color: ','.join(str(round(v * 255)) for v in _rgba(color))
                          for color in colors}
        (folder / 'scene-plan.json').write_text(json.dumps(plan, ensure_ascii=False), 'utf-8')
        files += ['map.pdf', 'scene-plan.json']
        doc = {'backend': 'matplotlib', 'project_fidelity': 'native_scene_preview',
            'project_file': '', 'preview': 'preview.png',
            'preview_sha256': _sha(folder / 'preview.png'),
            'scene_ref': scene_ref, 'scene_plan_sha256': _sha(folder / 'scene-plan.json'),
            'source_inventory': inventory, 'analysis': facts, 'crs': scene.crs,
            'layers': layers, 'extent': extents[0] if extents else [0, 0, 1, 1],
            'native_facts': {'page_previews': previews}, 'quality_status': 'draft',
            'layout': {'pages': len(previews), 'bounds_check': {'checked': True,
                'valid': not problems, 'problems': problems,
                'scope': 'measured_text_bounds_and_label_boxes; not_all_symbol_shapes'}},
            'file_hashes': {name: _sha(folder / name) for name in files},
            'files': [*files, 'project.json']}
        manifest_path.write_text(json.dumps(doc, ensure_ascii=False), 'utf-8')
        return _result(folder, doc)
    except BaseException:
        (folder / 'render-failed.json').write_text(json.dumps({'status': 'failed',
            'attempt': uuid4().hex}), 'utf-8')
        raise


def _result(folder, doc, reused=False):
    return {'project_dir': str(folder), 'project_manifest': str(folder / 'project.json'),
        'preview_path': str(folder / doc['preview']), 'output_path': str(folder / 'map.pdf'),
        'backend': 'matplotlib', 'project_fidelity': doc['project_fidelity'],
        'scene_ref': doc['scene_ref'], 'scene_plan_sha256': doc['scene_plan_sha256'],
        'analysis': doc['analysis'], 'layout': doc['layout'], 'extent': doc['extent'],
        'native_facts': {'page_previews': [{**p, 'path': str(folder / p['path'])}
                                        for p in doc['native_facts']['page_previews']]},
        'render_executed': not reused, 'reused': reused}
