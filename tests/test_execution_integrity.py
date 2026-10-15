import json

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

from geoharness.execution.contracts import EntityTracker, entity_join
from geoharness.execution.runner import execute_python


def sample():
    return gpd.GeoDataFrame({'id': ['a', 'b'], 'score': [2, 1]},
                            geometry=[Point(500000, 3000000), Point(500100, 3000000)],
                            crs=32647)


def test_sorted_attributes_cannot_be_paired_positionally():
    source = sample()
    tracker = EntityTracker()
    tracker.record('source', source)
    bad = gpd.GeoDataFrame(source.drop(columns='geometry').sort_values('score')
                          .reset_index(drop=True), geometry=source.geometry.values, crs=source.crs)
    with pytest.raises(ValueError, match='Entity/geometry mismatch'):
        tracker.check(bad)
    good = source.sort_values('score').reset_index(drop=True)
    assert tracker.check(good)['entity_binding'] == 'passed'
    joined = entity_join(source, pd.DataFrame({'id': ['b', 'a'], 'risk': [10, 20]}), key='id')
    assert joined.risk.tolist() == [20, 10]
    source['rank'] = [1, 2]
    tracker = EntityTracker()
    tracker.record('ranked', source)
    reordered = source.iloc[::-1].copy()
    reordered['rank'] = [1, 2]
    assert tracker.check(reordered)['entity_binding'] == 'passed'


@pytest.mark.asyncio
async def test_real_child_validates_geojson_and_partial_files(tmp_path):
    sample().to_file(tmp_path / 'source.gpkg', driver='GPKG')
    kwargs = dict(name='integrity', cwd=tmp_path, data_dir=None,
                  outputs_dir=tmp_path / 'runs', timeout_seconds=30, max_output_chars=5000)
    good = await execute_python(code="""
import geopandas as gpd
frame = gpd.read_file(SOURCE_CWD / 'source.gpkg')
frame.sort_values('score').to_file('sorted.geojson', driver='GeoJSON')
""", **kwargs)
    assert good['exit_code'] == 0, good['stderr']
    output = next(f for f in good['files'] if f['path'].endswith('sorted.geojson'))
    assert output['crs'] == 'EPSG:4326'
    assert output['validation_state'] == 'passed'
    from geoharness.tools.geo_deliver import _source

    assert _source(__import__('pathlib').Path(good['script_path']), [tmp_path]).is_file()
    bad = await execute_python(code="""
import geopandas as gpd
frame = gpd.read_file(SOURCE_CWD / 'source.gpkg')
open('partial.csv', 'w').write('id\\na\\n')
attrs = frame.drop(columns='geometry').sort_values('score').reset_index(drop=True)
gpd.GeoDataFrame(attrs, geometry=frame.geometry.values, crs=frame.crs).to_file('bad.geojson')
""", **kwargs)
    assert bad['status'] == 'failed'
    assert 'Entity/geometry mismatch' in bad['stderr']
    assert all(f['state'] == 'failed' for f in bad['files'])
    assert json.loads(open(bad['manifest_path'], encoding='utf-8').read())['status'] == 'failed'
    partial = __import__('pathlib').Path(bad['run_dir']) / 'partial.csv'
    consumed = await execute_python(
        code=f'import pandas as pd\npd.read_csv({str(partial)!r})', **kwargs)
    assert consumed['status'] == 'failed'
    assert 'partial outputs are inspection-only' in consumed['stderr']


@pytest.mark.asyncio
async def test_new_geometry_without_source_is_not_self_certified(tmp_path):
    result = await execute_python(code="""
import geopandas as gpd
from shapely.geometry import Point
gpd.GeoDataFrame({'id':['a']}, geometry=[Point(1,1)], crs=4326).to_file('new.geojson')
""", name='new', cwd=tmp_path, data_dir=None, outputs_dir=tmp_path/'runs',
        timeout_seconds=30, max_output_chars=5000)
    assert result['status'] == 'succeeded', result['stderr']
    output = next(f for f in result['files'] if f['path'].endswith('new.geojson'))
    assert output['entity_binding'] == 'unverified'


@pytest.mark.asyncio
async def test_preflight_and_declared_outputs(tmp_path):
    kwargs = dict(name='preflight', cwd=tmp_path, data_dir=None,
                  outputs_dir=tmp_path / 'runs', timeout_seconds=30, max_output_chars=5000)
    bad = await execute_python(code='x = [)', **kwargs)
    assert 'SyntaxError' in bad['stderr']
    missing = await execute_python(code='print(1)', expected_outputs=('result.csv',), **kwargs)
    assert missing['status'] == 'failed'
    assert 'declared outputs missing' in missing['stderr']
