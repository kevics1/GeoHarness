import json
from pathlib import Path

import geopandas as gpd
import pytest
from test_result_packages import produced

from geoharness.analysis.result_package import FactRequest, publish_package, verify_package
from geoharness.cartography.facts import AnalysisBindingError, load_analysis_facts
from geoharness.data.artifacts import sha256_file


def multi(tmp_path):
    context, source, _ = produced(tmp_path, vector=True)
    frame = gpd.read_file(source)
    frame.to_file(source, layer='all_people', driver='GPKG')
    frame.iloc[:5].to_file(source, layer='selected', driver='GPKG')
    manifest = source.parent / 'manifest.json'
    doc = json.loads(manifest.read_text('utf-8'))
    next(f for f in doc['files'] if f['path'] == str(source))['sha256'] = sha256_file(source)
    manifest.write_text(json.dumps(doc), 'utf-8')
    return context, source, frame


def test_multilayer_publication_freezes_layer_and_rejects_wrong_map_view(tmp_path):
    context, source, frame = multi(tmp_path)
    request = dict(key='id', numeric_fields=['population'], method={'operation': 'count'})
    with pytest.raises(ValueError, match='source_layer'):
        publish_package(context, source_ref=str(source), request=FactRequest(**request))
    all_rows = publish_package(context, source_ref=str(source), request=FactRequest(
        **request, source_layer='all_people'))
    subset = publish_package(context, source_ref=str(source), request=FactRequest(
        **request, source_layer='selected'))
    assert all_rows['facts']['row_count'] == 234 and subset['facts']['row_count'] == 5
    assert all_rows['selection']['id'] != subset['selection']['id']
    assert verify_package(Path(subset['package_path']))['facts']['source_layer'] == 'selected'
    args = dict(cwd=tmp_path, read_roots=[tmp_path / '.geoharness/analysis'],
                display_crs='EPSG:3857')
    with pytest.raises(AnalysisBindingError):
        load_analysis_facts([all_rows['analysis_ref']], mapped_sources=[{
            'file_path': str(source), 'source_layer': 'selected', 'gdf': frame.iloc[:5]}], **args)
    facts = load_analysis_facts([subset['analysis_ref']], mapped_sources=[{
        'file_path': str(source), 'source_layer': 'selected', 'gdf': frame.iloc[:5]}], **args)
    assert facts['status'] == 'verified'


def test_unknown_layer_cannot_publish(tmp_path):
    context, source, _ = multi(tmp_path)
    with pytest.raises(ValueError, match='source_layer'):
        publish_package(context, source_ref=str(source), request=FactRequest(
            key='id', source_layer='wrong', method={'operation': 'count'}))


def test_single_layer_name_can_be_omitted_by_map_without_losing_binding(tmp_path):
    context, source, _ = produced(tmp_path, vector=True)
    import pyogrio

    name = str(pyogrio.list_layers(source)[0, 0])
    package = publish_package(context, source_ref=str(source), request=FactRequest(
        key='id', source_layer=name, method={'operation': 'count'}))
    facts = load_analysis_facts([package['analysis_ref']], cwd=tmp_path,
        read_roots=[tmp_path / '.geoharness/analysis'], mapped_sources=[{
            'file_path': str(source)}], display_crs='EPSG:3857')
    assert facts['status'] == 'verified'
    with pytest.raises(AnalysisBindingError):
        load_analysis_facts([package['analysis_ref']], cwd=tmp_path,
            read_roots=[tmp_path / '.geoharness/analysis'], mapped_sources=[{
                'file_path': str(source), 'source_layer': 'wrong'}], display_crs='EPSG:3857')


def test_multilayer_nearest_check_uses_explicit_reference_layer(tmp_path):
    from shapely.geometry import Point

    context, source, frame = multi(tmp_path)
    selected = frame.iloc[:5].copy()
    selected['distance_m'] = 1000 - selected['id']
    selected.to_file(source, layer='selected', driver='GPKG')
    manifest = source.parent / 'manifest.json'
    doc = json.loads(manifest.read_text('utf-8'))
    next(f for f in doc['files'] if f['path'] == str(source))['sha256'] = sha256_file(source)
    manifest.write_text(json.dumps(doc), 'utf-8')
    reference = tmp_path / 'shelters.gpkg'
    gpd.GeoDataFrame({'id': [1]}, geometry=[Point(1000, 0)], crs=3857).to_file(
        reference, layer='actual')
    gpd.GeoDataFrame({'id': [2]}, geometry=[Point(9000, 0)], crs=3857).to_file(
        reference, layer='unrelated')
    request = FactRequest(key='id', source_layer='selected', numeric_fields=['distance_m'],
        units={'distance_m': 'm'}, method={'operation': 'nearest'}, nearest_checks=[{
            'field': 'distance_m', 'reference_ref': str(reference), 'reference_layer': 'actual',
            'metric_crs': 'EPSG:3857'}])
    package = publish_package(context, source_ref=str(source), request=request)
    assert verify_package(Path(package['analysis_ref']))['nearest_checks'][0]['rows'] == 5
