"""Third-run failures exercised at actual execution and persisted-data boundaries."""
import json

import geopandas as gpd
import pandas as pd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import box

from geoharness.config.settings import GeoConfig
from geoharness.execution.contracts import artifact_facts, check_claims, extent_geometry
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool


async def test_actual_intermediate_contract_precedes_code_and_exposes_schema(tmp_path):
    source = tmp_path / 'intermediate.csv'
    pd.DataFrame({'id': [1], 'd_fire_m_base': [160]}).to_csv(source, index=False)
    arguments = GeoPythonInput(code="open('executed.txt','w').write('yes')",
        input_refs={'table': str(source)},
        input_requirements={'table': {'fields': ['d_fire_m'], 'key': 'id'}})
    result = await GeoPythonTool().execute(arguments, ToolExecutionContext(
        cwd=tmp_path, metadata={'geoharness_config': GeoConfig()}))
    assert result.is_error
    assert 'd_fire_m_base' in result.metadata['stderr']
    assert not list(tmp_path.rglob('executed.txt'))


async def test_declared_crs_mismatch_is_not_relabelled(tmp_path):
    source = tmp_path / 'input.gpkg'
    gpd.GeoDataFrame({'id': [1]}, geometry=[box(400000, 3000000, 401000, 3001000)],
                     crs=32648).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    tool = GeoPythonTool()
    bad = await tool.execute(GeoPythonInput(code="print('should not run')",
        input_refs={'vector': str(source)}, input_requirements={
            'vector': {'crs': 'EPSG:32647', 'valid_geometry': True}}), context)
    assert bad.is_error and 'actual=EPSG:32648' in bad.metadata['stderr']
    good = await tool.execute(GeoPythonInput(code="print('contract checked')",
        input_refs={'vector': str(source)}, input_requirements={
            'vector': {'crs': 'EPSG:32648', 'metric': True, 'key': 'id',
                       'valid_geometry': True}}), context)
    assert not good.is_error, good.output
    integrity = json.loads((__import__('pathlib').Path(good.metadata['run_dir']) /
                            'integrity.json').read_text(encoding='utf-8'))
    assert integrity['input_checks']['vector']['rows'] == 1
    assert any(item['role'] == 'source_read' and item['crs'] == 'EPSG:32648'
               for item in integrity['provenance'])


async def test_missing_manifest_alias_is_diagnosed_before_child_launch(tmp_path):
    result = await GeoPythonTool().execute(GeoPythonInput(code="print(INPUTS['manifest'])"),
        ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()}))
    assert result.is_error
    assert 'declare input_refs' in result.metadata['stderr']


def test_named_extent_retains_crs_and_validates_order():
    shape = extent_geometry(west=400000, south=3000000, east=401000, north=3001000, crs=32648)
    assert shape.crs.to_epsg() == 32648
    assert shape.area.iloc[0] == 1_000_000
    with pytest.raises(ValueError, match='west < east'):
        extent_geometry(west=400000, south=401000, east=3000000, north=300100, crs=32648)
    with pytest.raises(ValueError, match='Geographic output'):
        extent_geometry(west=400000, south=3000000, east=401000, north=3001000, crs=4326)


def test_invalid_geometry_returns_bounded_row_diagnostics():
    from shapely.geometry import Polygon

    from geoharness.execution.contracts import validate_vector
    from geoharness.tools.geo_python import python_recovery_hints

    frame = gpd.GeoDataFrame(geometry=[Polygon([(0, 0), (1, 1), (1, 0), (0, 1), (0, 0)])],
                             index=['collapsed-part'], crs=32648)
    with pytest.raises(ValueError, match='collapsed-part') as error:
        validate_vector(frame)
    assert 'Self-intersection' in str(error.value)
    assert 'after to_crs()' in python_recovery_hints('', str(error.value))[0]


def test_facts_recompute_threshold_and_area_and_reject_wrong_claims(tmp_path):
    source = tmp_path / 'results.gpkg'
    gpd.GeoDataFrame({'id': ['a', 'b', 'c'], 'd_fire_m': [160, 500, None]}, geometry=[
        box(400000 + n*100, 3000000, 400050 + n*100, 3000050) for n in range(3)],
        crs=32648).to_file(source)
    facts = artifact_facts(source, key='id', numeric_fields=['d_fire_m'],
        predicates=[{'name': 'within500', 'field': 'd_fire_m', 'op': '<=', 'value': 500}],
        metric_crs=32648)
    assert facts['counts']['within500']['count'] == 2
    assert facts['counts']['within500']['missing_excluded'] == 1
    assert facts['area']['sum_feature_area_m2'] == 7500
    report = check_claims(facts, [{'path': ['area', 'sum_feature_area_m2'], 'expected': 3120000},
        {'path': ['counts', 'within500', 'count'], 'expected': 2}])
    assert report['status'] == 'failed'
    assert [c['matched'] for c in report['checks']] == [False, True]
    with pytest.raises(ValueError, match='metre units'):
        artifact_facts(source, key='id', metric_crs=4326)
