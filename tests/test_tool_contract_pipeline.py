"""Analysis -> persisted facts -> explicit claims -> actual delivery, without a model."""
import json
from pathlib import Path

import geopandas as gpd
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig
from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool


async def test_analysis_contract_facts_and_delivery_chain_preserves_unknowns(tmp_path):
    source = tmp_path/'source.gpkg'
    gpd.GeoDataFrame({'id': ['a', 'b', 'c'], 'distance': [160, 500, 1175],
                     'time': [1, None, 3]},
        geometry=[Point(400000+n*100, 3000000) for n in range(3)], crs=32648).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    result = await GeoPythonTool().execute(GeoPythonInput(
        input_refs={'source': str(source)}, input_requirements={'source': {
            'fields': ['id', 'distance', 'time'], 'key': 'id', 'crs': 'EPSG:32648',
            'valid_geometry': True}}, expected_outputs=[
                'result.gpkg', 'facts.json', 'claims.json', 'method.json'], code="""
import json
import geopandas as gpd
frame = gpd.read_file(INPUTS['source'])
score = score_risk(frame, key='id', missing_policy='unknown', components=[
    {'field':'time','weight':1,'minimum':0,'maximum':10,'direction':'higher'}])
assert score.risk_status.tolist() == ['computed','unknown','computed']
assert score.loc[1, 'time'] != score.loc[1, 'time']
score.to_file('result.gpkg')
facts = artifact_facts('result.gpkg',key='id',numeric_fields=['distance','time'],predicates=[
    {'name':'within500','field':'distance','op':'<=','value':500}])
claims = check_claims(facts,[{'path':['counts','within500','count'],'expected':2}])
assert claims['status'] == 'passed'
open('facts.json','w').write(json.dumps(facts))
open('claims.json','w').write(json.dumps(claims))
open('method.json','w').write(json.dumps(score.attrs['risk_method']))
"""), context)
    assert not result.is_error, result.output
    paths = {Path(file['path']).name: file['path'] for file in result.metadata['files']}
    delivered = await GeoDeliverTool().execute(GeoDeliverInput(
        files=[paths[name] for name in ('result.gpkg', 'facts.json', 'claims.json', 'method.json')],
        name='contract-results'), context)
    assert not delivered.is_error, delivered.output
    folder = Path(delivered.metadata['manifest_path']).parent
    facts = json.loads((folder/'facts.json').read_text(encoding='utf-8'))
    assert facts['numeric']['time']['missing'] == 1
    assert (folder/'method.json').is_file()
    assert (folder/'result.gpkg').is_file()
    assert delivered.metadata['quality_status'] == 'draft'
