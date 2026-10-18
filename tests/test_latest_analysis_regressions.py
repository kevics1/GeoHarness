"""Regression cases from the Oct18 saved run, using synthetic inputs only."""

import json

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point, box

from geoharness.execution.contracts import (
    first_coverage,
    make_leader_lines,
    score_risk,
    write_analysis_report,
)


def test_earliest_coverage_is_not_nearest_stage_and_preserves_uncovered():
    points = gpd.GeoDataFrame({'id': [1, 2, 3, 4]},
        geometry=[Point(1, 1), Point(3, 3), Point(9, 9), Point(0, 0)], crs=32647)
    stages = gpd.GeoDataFrame({'stage': ['late', 'early']},
        geometry=[box(0, 0, 4, 4), box(0, 0, 2, 2)], crs=32647)
    result = first_coverage(points, stages, key='id', stage_key='stage',
                            stage_order=['early', 'late'], boundary='covers')
    assert result.first_stage_hit.iloc[[0, 1, 3]].tolist() == ['early', 'late', 'early']
    assert pd.isna(result.first_stage_hit.iloc[2])
    assert result.coverage_status.tolist() == ['covered', 'covered', 'not_covered', 'covered']
    assert result.geometry.equals(points.geometry)
    assert 'first_stage_hit' not in points
    excluded = first_coverage(points, stages.to_crs(4326), key='id', stage_key='stage',
                              stage_order=['early', 'late'], boundary='contains')
    assert excluded.first_stage_hit.iloc[0] == 'early'
    with pytest.raises(ValueError, match='stage_order'):
        first_coverage(points, stages, key='id', stage_key='stage',
                       stage_order=['late'], boundary='covers')


def test_leaders_join_identity_and_omit_only_zero_length_lines():
    points = gpd.GeoDataFrame({'id': ['a', 'b']},
        geometry=[Point(0, 0), Point(2, 2)], crs=32647)
    labels = gpd.GeoDataFrame({'id': ['b', 'a']},
        geometry=[Point(3, 3), Point(0, 0)], crs=32647)
    lines = make_leader_lines(points, labels, key='id', metric_crs=32647)
    assert lines.id.tolist() == ['b'] and lines.is_valid.all()
    assert lines.attrs['omitted_zero_length_keys'] == ['a']
    assert len(points) == len(labels) == 2
    empty = make_leader_lines(points, points, key='id', metric_crs=32647)
    assert empty.empty and empty.crs.to_epsg() == 32647
    with pytest.raises(ValueError, match='match exactly'):
        make_leader_lines(points, labels.iloc[:1], key='id', metric_crs=32647)


def test_missing_score_policy_is_explicit_and_all_missing_remains_unknown():
    frame = pd.DataFrame({'id': [1, 2, 3], 'distance': [1, 1, None],
                          'population': [1, None, None]})
    components = [{'field': name, 'weight': weight, 'minimum': 0,
                   'maximum': 2, 'direction': 'higher'}
                  for name, weight in [('distance', .75), ('population', .25)]]
    with pytest.raises(ValueError, match='missing values'):
        score_risk(frame, key='id', components=components)
    out = score_risk(frame, key='id', components=components, missing_policy='renormalize')
    assert out.risk_score.iloc[:2].tolist() == [.5, .5]
    assert pd.isna(out.risk_score.iloc[2]) and out.risk_status.iloc[2] == 'unknown'
    assert out.attrs['risk_method']['scientific_correctness'] == 'unverified'


def test_report_recomputes_group_counts_and_rejects_wrong_claim_before_write(tmp_path):
    source = tmp_path / 'result.csv'
    source.write_text('id,type,distance\na,学校,5\nb,学校,15\nc,住宅区,25\n', encoding='utf-8')
    path = tmp_path / 'facts.json'
    kwargs = {'source': source, 'key': 'id', 'numeric_fields': ['distance'],
              'group_by': ['type'], 'method': {'weights': {'distance': 1},
                                               'missing_policy': 'error'}}
    with pytest.raises(ValueError, match='disagree'):
        write_analysis_report(path, **kwargs, claims=[{'path': ['row_count'], 'expected': 4}])
    assert not path.exists()
    with pytest.raises(ValueError, match='weights'):
        write_analysis_report(path, **{**kwargs, 'method': {'weights': {'distance': .8}}})
    assert not path.exists()
    report = write_analysis_report(path, **kwargs,
                                  claims=[{'path': ['row_count'], 'expected': 3}])
    assert report['facts']['groups']['type'] == [
        {'value': '学校', 'count': 2}, {'value': '住宅区', 'count': 1}]
    assert report['analysis_correctness'] == 'unverified'
    assert json.loads(path.read_text(encoding='utf-8')) == report
    assert '实体数：3' in path.with_suffix('.md').read_text(encoding='utf-8')
    with pytest.raises(ValueError, match='end in .json'):
        write_analysis_report(source, **kwargs)


async def test_real_python_helpers_and_delivery_remeasure_same_source(tmp_path):
    from openharness.tools.base import ToolExecutionContext

    from geoharness.config.settings import GeoConfig
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool
    from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool

    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    result = await GeoPythonTool().execute(GeoPythonInput(name='coverage_report', code='''
import geopandas as gpd
from shapely.geometry import Point, box
points=gpd.GeoDataFrame({'id':['a','b']},geometry=[Point(1,1),Point(8,8)],crs=32647)
stages=gpd.GeoDataFrame({'stage':['evening','night']},
    geometry=[box(0,0,2,2),box(0,0,4,4)],crs=32647)
covered=first_coverage(points,stages,key='id',stage_key='stage',
    stage_order=['evening','night'],boundary='covers')
covered.to_file(RUN_DIR/'covered.gpkg',driver='GPKG')
write_analysis_report(RUN_DIR/'facts.json',source=RUN_DIR/'covered.gpkg',key='id',
    group_by=['coverage_status'],method=covered.attrs['coverage_method'])
''', expected_outputs=['covered.gpkg', 'facts.json', 'facts.md']), context)
    assert not result.is_error, result.output
    files = {__import__('pathlib').Path(f['path']).name: f['path']
             for f in result.metadata['files']}
    delivered = await GeoDeliverTool().execute(GeoDeliverInput(
        files=[files[k] for k in ('covered.gpkg', 'facts.json', 'facts.md')],
        name='synthetic-analysis'), context)
    assert not delivered.is_error, delivered.output
    summary = json.loads(__import__('pathlib').Path(
        delivered.metadata['summary_json']).read_text(encoding='utf-8'))
    assert summary['attached_analysis_reports'][0]['status'] == 'persisted_values_checked'
    assert summary['analysis_correctness'] == 'unverified'
    assert 'draft' in delivered.metadata['quality_notice']
