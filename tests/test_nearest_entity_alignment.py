import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig
from geoharness.execution.nearest import nearest_distance, verify_nearest_distances
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool


def inputs(tmp_path):
    # Overlapping ID/index domains also detect silent wrong values without nulls.
    places = gpd.GeoDataFrame({'gid': [2, 0, 1]},
        geometry=[Point(100, 0), Point(200, 0), Point(400, 0)], crs=3857)
    shelters = gpd.GeoDataFrame({'name': ['避难场所']}, geometry=[Point(0, 0)], crs=3857)
    places.to_file(tmp_path / 'places.gpkg')
    shelters.to_file(tmp_path / 'shelters.gpkg')
    return places, shelters


def test_nearest_helper_preserves_shuffled_business_keys_index_and_storage_crs(tmp_path):
    places, shelters = inputs(tmp_path)
    places.index = [71, 8, 45]
    places = places.to_crs(4326)
    actual = nearest_distance(places, shelters, key='gid', field='distance_m',
                              metric_crs='EPSG:3857')
    assert actual.index.tolist() == [71, 8, 45]
    assert actual.gid.tolist() == [2, 0, 1]
    assert actual.crs == places.crs
    assert np.allclose(actual.distance_m, [100, 200, 400])
    assert verify_nearest_distances(actual, shelters, key='gid', field='distance_m',
                                    metric_crs=3857)['status'] == 'passed'
    actual['distance_m'] = [400, None, 100]
    report = verify_nearest_distances(actual, shelters, key='gid', field='distance_m',
                                      metric_crs=3857)
    assert report['mismatches'] == 3 and report['missing'] == 1
    assert report['samples'][0]['key'] == '2'


def test_binding_checks_origin_while_allowing_correct_unit_conversion(tmp_path):
    from geoharness.execution.nearest import NearestBindings

    places, shelters = inputs(tmp_path)
    bindings = NearestBindings()
    joined = bindings.record(gpd.sjoin_nearest(places, shelters, distance_col='distance_m'))
    bindings.check_assignment(places, 'distance_km', joined['distance_m'] / 1000)
    keyed = joined.set_index('gid')
    bindings.check_assignment(places.set_index('gid'), 'distance_km', keyed.distance_m / 1000)
    with pytest.raises(ValueError, match='Nearest entity alignment mismatch'):
        bindings.check_assignment(places, 'distance_km', keyed.distance_m / 1000)


@pytest.mark.parametrize('change', ['geographic', 'empty_reference', 'duplicate_id'])
def test_nearest_rejects_invalid_measurement_contract(tmp_path, change):
    places, shelters = inputs(tmp_path)
    crs = 3857
    if change == 'geographic':
        crs = 4326
    elif change == 'empty_reference':
        shelters = shelters.iloc[:0]
    else:
        places['gid'] = [1, 1, 2]
    with pytest.raises(ValueError):
        nearest_distance(places, shelters, key='gid', field='distance_m', metric_crs=crs)


@pytest.mark.parametrize('call', ['gpd.sjoin_nearest(P, R,', 'P.sjoin_nearest(R,'])
async def test_actual_python_rejects_nearest_series_attached_to_rangeindex(tmp_path, call):
    inputs(tmp_path)
    code = f"""import geopandas as gpd
P=gpd.read_file(INPUTS['places']).reset_index(drop=True)
R=gpd.read_file(INPUTS['shelters'])
sn={call}how='left',distance_col='d_shelter').drop_duplicates('gid').set_index('gid')
P['d_shelter']=sn['d_shelter']
P.to_file(RUN_DIR/'wrong.gpkg')
"""
    result = await GeoPythonTool().execute(GeoPythonInput(code=code, input_refs={
        'places': str(tmp_path / 'places.gpkg'), 'shelters': str(tmp_path / 'shelters.gpkg')}),
        ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()}))
    assert result.is_error, result.output
    assert 'Nearest entity alignment mismatch' in result.metadata['stderr']
    assert not (Path(result.metadata['run_dir']) / 'wrong.gpkg').exists()


async def test_actual_python_correct_key_join_retains_nearest_attributes(tmp_path):
    inputs(tmp_path)
    code = """import geopandas as gpd
P=gpd.read_file(INPUTS['places']).reset_index(drop=True)
R=gpd.read_file(INPUTS['shelters'])
sn=gpd.sjoin_nearest(P, R, how='left', distance_col='distance_m').drop_duplicates('gid')
P=entity_join(P, sn[['gid','name','distance_m']], key='gid')
P.to_file(RUN_DIR/'correct.gpkg')
checked=verify_nearest_distances(P,R,key='gid',field='distance_m',metric_crs=3857)
assert checked['status']=='passed', checked
CONSOLE_RESULT=checked
"""
    result = await GeoPythonTool().execute(GeoPythonInput(code=code, input_refs={
        'places': str(tmp_path / 'places.gpkg'), 'shelters': str(tmp_path / 'shelters.gpkg')}),
        ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()}))
    assert not result.is_error, result.output
    output = gpd.read_file(Path(result.metadata['run_dir']) / 'correct.gpkg')
    assert output.gid.tolist() == [2, 0, 1]
    assert output.distance_m.tolist() == [100, 200, 400]
    json.loads((Path(result.metadata['run_dir']) / 'integrity.json').read_text('utf-8'))
