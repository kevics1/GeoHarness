import json

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import LineString, Point, box

from geoharness.config.settings import GeoConfig, QgisConfig
from geoharness.execution.contracts import (
    check_leader_lines,
    fit_frame_extent,
    make_leader_lines,
)
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def endpoints():
    points = gpd.GeoDataFrame({'id': [12, 13]},
        geometry=[Point(100, 100), Point(200, 200)], crs=3857)
    anchors = gpd.GeoDataFrame({'id': [13, 12]},
        geometry=[Point(220, 200), Point(100, 120)], crs=3857)
    return points, anchors


def test_moving_anchors_invalidates_old_leaders_and_regeneration_fixes_them():
    points, anchors = endpoints()
    lines = make_leader_lines(points, anchors, key='id', metric_crs=3857)
    assert check_leader_lines(points, anchors, lines,
                             key='id', metric_crs=3857)['status'] == 'passed'
    anchors.loc[anchors.id == 12, 'geometry'] = Point(120, 100)  # 90-degree stale callout.
    bad = check_leader_lines(points, anchors, lines, key='id', metric_crs=3857)
    assert bad['status'] == 'failed' and bad['issues'][0]['key'] == 12
    json.dumps(bad, allow_nan=False)
    fixed = make_leader_lines(points, anchors, key='id', metric_crs=3857)
    assert fixed.attrs['leader_check']['status'] == 'passed'


def test_trimmed_symbol_edge_gaps_must_be_explicit_and_geometry_still_matches():
    points, anchors = endpoints()
    lines = gpd.GeoDataFrame({'id': [12, 13]}, geometry=[
        LineString([(100, 102), (100, 117)]), LineString([(202, 200), (217, 200)])], crs=3857)
    assert check_leader_lines(points, anchors, lines,
                             key='id', metric_crs=3857)['status'] == 'failed'
    assert check_leader_lines(points, anchors, lines, key='id', metric_crs=3857,
        source_gap_m=2, target_gap_m=3)['status'] == 'passed'


def test_fit_uses_actual_frame_aspect_and_never_crops_required_subjects():
    frame = gpd.GeoDataFrame(geometry=[box(0, 0, 100, 400)], crs=3857)
    fit = fit_frame_extent(frame, metric_crs=3857, width_mm=200, height_mm=100)
    x1, y1, x2, y2 = fit['map_extent']
    assert (x2 - x1) / (y2 - y1) == pytest.approx(2)
    assert box(x1, y1, x2, y2).covers(frame.geometry.iloc[0])
    with pytest.raises(ValueError, match='explicit spatial scale'):
        fit_frame_extent(frame.centroid.to_frame('geometry'), metric_crs=3857,
                         width_mm=100, height_mm=100)


async def test_qgis_preflight_rejects_stale_leader_before_native_render(tmp_path):
    points, anchors = endpoints()
    lines = make_leader_lines(points, anchors, key='id', metric_crs=3857)
    anchors.loc[anchors.id == 12, 'geometry'] = Point(120, 100)
    layers = []
    for name, frame in [('points', points), ('anchors', anchors), ('leaders', lines)]:
        path = tmp_path / f'{name}.gpkg'
        frame.to_file(path)
        layers.append({'file_path': str(path), 'layer_key': name})
    layers[2]['leader_binding'] = {'source_layer': 'points', 'labels_layer': 'anchors',
                                   'key_field': 'id', 'metric_crs': 'EPSG:3857'}
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig(
        qgis=QgisConfig(enabled=False))})
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action='preflight',
        title='callout check', crs='EPSG:3857', layers=layers), context)
    assert result.is_error and 'current source/anchors' in result.output
    assert result.metadata['render_executed'] is False
