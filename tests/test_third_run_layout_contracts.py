"""Actual no-progress, hidden roster identity and per-page labeling regressions."""
import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from PIL import Image
from shapely.geometry import Point, box
from test_map_review_evidence import Reviewer

from geoharness.cartography.quality import review_map
from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.qgis.frame_contracts import joint_paper_proposal
from geoharness.qgis.layout_table import select_roster
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def test_unchanged_dimension_proposal_is_not_a_recursion_candidate():
    spec = {'width_mm': 297, 'height_mm': 210, 'sidebar_width_mm': 60, 'legend': False}
    regions = {'main_map': [12, 12, 208, 150], 'detail_map': [225, 12, 60, 80]}
    assert joint_paper_proposal(spec, regions, 2.04634083, 2.5, (0.5, 0.1)) is None
    assert joint_paper_proposal(spec, regions, 1, 2.5) is not None
    # Hard zoom and coverage remain requirements; the solver never lowers zoom.
    assert spec == {'width_mm': 297, 'height_mm': 210, 'sidebar_width_mm': 60, 'legend': False}


def test_roster_selection_uses_hidden_stable_id_instead_of_nullable_display_rank():
    records = [{'id': 'a', 'number': 1}, {'id': 'b', 'number': 2}, {'id': 'c', 'number': None}]
    assert select_roster(records, {'fields': ['number', 'name'], 'key_field': 'id',
                                   'keys': ['b']}) == [records[1]]


def test_descriptive_review_reason_is_safe_but_unknown_actions_remain_invalid():
    from pydantic import ValidationError

    from geoharness.cartography.quality import MapIssue
    from geoharness.tools.geo_qgis_project import QgisLabelTable

    assert MapIssue(severity='warning', description='crowded',
                    severity_reason='Numbers touch').severity_reason == 'Numbers touch'
    with pytest.raises(ValidationError, match='Extra inputs'):
        MapIssue(severity='warning', description='crowded', execute='anything')
    with pytest.raises(ValidationError, match='undisplayed fields'):
        QgisLabelTable(layer_index=0, fields=['id', 'name'], field_widths={'band': 2})


async def test_native_hidden_roster_key_and_actual_frame_label_overrides(tmp_path):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (OSError, ValueError) as exc:
        pytest.skip(f'QGIS unavailable: {exc}')
    city, source = tmp_path/'city.gpkg', tmp_path/'points.gpkg'
    gpd.GeoDataFrame(geometry=[box(0, 0, 10000, 10000)], crs=3857).to_file(city)
    gpd.GeoDataFrame({'id': ['a', 'b', 'c'], 'number': [1, 2, None],
        'name': ['first', 'second', 'outlier']}, geometry=[Point(200, 200), Point(500, 500),
        Point(9000, 9000)], crs=3857).to_file(source)
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action='create',
        title='Overview and detail', crs='EPSG:3857', dpi=36, layers=[
            {'file_path': str(city), 'layer_key': 'city'},
            {'file_path': str(source), 'layer_key': 'places', 'label_field': 'number'}],
        layout={'legend': False, 'scale_bar': False, 'detail_placement': 'page',
            'main_frame': {'purpose': 'overview; numbers belong to detail', 'label_layers': []},
            'detail_extent': {'xmin': 0, 'ymin': 0, 'xmax': 1000, 'ymax': 1000},
            'detail_frame': {'purpose': 'numbered detail', 'label_layers': ['places']},
            'label_table': {'layer_index': 'places', 'fields': ['number', 'name'],
                'key_field': 'id', 'keys': ['a', 'b'], 'placement': 'pages'}}),
        ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': config}))
    assert not result.is_error, result.output
    manifest = json.loads(Path(result.metadata['project_manifest']).read_text(encoding='utf-8'))
    frames = manifest['native_facts']['frames']
    assert frames['main_map']['label_sources'] == []
    assert frames['detail_map']['label_sources'] == [1]
    assert frames['main_map']['purpose'].startswith('overview')
    assert frames['main_map']['labels'] == []
    assert frames['detail_map']['page_index'] == 1
    roster = manifest['layout']['roster_selection']
    assert roster['membership_check']['valid']
    assert roster['keys'] == ['a', 'b']
    tables = manifest['layout']['label_tables']
    assert [key for table in tables for key in table['source_keys']] == ['a', 'b']


@pytest.mark.parametrize('disabled', [True, False])
async def test_contradicted_label_assertion_cannot_authorize_repair(tmp_path, disabled):
    path = tmp_path/'preview.png'
    Image.new('RGB', (100, 100), 'white').save(path)
    frame = {'rect_mm': [0, 0, 100, 100], 'source_indices': [0], 'page_index': 0,
             'label_sources': [] if disabled else [0], 'labels': [
                 {'layer_index': 0, 'feature_id': 9, 'is_unplaced': False}]}
    response = {'status': 'issues', 'summary': 'claimed missing', 'issues': [{
        'severity': 'warning', 'description': 'missing', 'issue_id': 'missing-9',
        'issue_code': 'label_missing', 'native_assertion': 'label_not_placed',
        'frame_id': 'main_map', 'roi_ref': 'main_map', 'bbox': [0, 0, 1, 1],
        'source_indices': [0], 'feature_ids': [9]}], 'qgis_patch': {
            'issue_ids': ['missing-9'], 'layers': [{'layer_index': 0,
                'label_style': {'size_pt': 10}}]}}
    review = await review_map(path, config=GeoConfig(), task='Check current page',
        client=Reviewer(response), evidence={'backend': 'qgis', 'render_metadata': {
            'layers': [{'kind': 'vector', 'geometry_type': 'Point', 'label_field': 'number'}],
            'native_facts': {'frames': {'main_map': frame}},
            'layout': {'effective_parameters': {'width_mm': 100, 'height_mm': 100}}}})
    assert review.qgis_patch is None
    assert review.diagnostics['disputed_native_assertions'][0]['issue_id'] == 'missing-9'
    assert review.status == 'issues'  # Not promoted to visual acceptance by engineering facts.
