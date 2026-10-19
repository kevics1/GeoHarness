"""Synthetic native QGIS round trip for roster titles, page roles and RGB legend policy."""

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from openharness.tools.base import ToolExecutionContext
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def test_rgb_legend_default_and_roster_title_survive_native_export(tmp_path):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f'QGIS unavailable: {exc}')
    points = tmp_path / 'points.gpkg'
    raster = tmp_path / 'rgb.tif'
    gpd.GeoDataFrame({'id': range(1, 33), 'name': [f'合成对象{i}' for i in range(32)],
        'distance': range(32)}, geometry=[Point(200 + i * 10, 500) for i in range(32)],
        crs=3857).to_file(points, driver='GPKG')
    with rasterio.open(raster, 'w', driver='GTiff', width=10, height=10,
        count=3, dtype='uint8', crs='EPSG:3857', transform=from_origin(0, 1000, 100, 100)) as dst:
        dst.write(np.full((3, 10, 10), 120, dtype='uint8'))
        dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue)
    tool = GeoQgisProjectTool()
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': config})
    created = await tool.execute(GeoQgisProjectInput(action='create', crs='EPSG:3857',
        title='合成分页测试', dpi=72, map_extent=[0, 0, 1000, 1000], layers=[
            {'file_path': str(raster), 'kind': 'raster', 'name': '影像'},
            {'file_path': str(points), 'name': '对象'},
            {'file_path': str(points), 'name': '辅助编号', 'show_in_legend': False}],
        layout={'label_table': {'layer_index': 1, 'fields': ['id', 'name', 'distance'],
            'headers': {'distance': '距离(m)'}, 'placement': 'pages', 'page_columns': 1,
            'title': '合成对象名单', 'note': '编号对应合成地图；距离单位为米。'}}), context,
        _materialize=True)
    assert not created.is_error, created.output
    manifest = json.loads(Path(created.metadata['project_manifest']).read_text(encoding='utf-8'))
    pages = manifest['native_facts']['page_previews']
    assert pages[0]['role'] == 'map' and pages[1]['role'] == 'roster'
    tables = manifest['layout']['label_tables']
    assert sum(t['visible_rows'] for t in tables) == 32
    assert all(t['page_title'] == '合成对象名单' for t in tables)
    assert manifest['layout']['bounds_check']['valid']
    legends = manifest['layout']['legends']
    indices = {layer['layer_index'] for v in legends.values() for layer in v['layers']}
    assert 0 not in indices and 2 not in indices and 1 in indices
    exported = await tool.execute(GeoQgisProjectInput(action='export', dpi=72,
        project_path=created.metadata['project_file']), context)
    assert not exported.is_error, exported.output
    saved = json.loads(Path(exported.metadata['project_manifest']).read_text(encoding='utf-8'))
    assert saved['native_facts']['page_previews'][1]['role'] == 'roster'
    assert sum(t['visible_rows'] for t in saved['layout']['label_tables']) == 32
    assert all(t['page_title'] == '合成对象名单' for t in saved['layout']['label_tables'])
