import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.analysis.result_package import FactRequest, publish_package, verify_package
from geoharness.cartography.delivery_summary import _attached_analysis
from geoharness.cartography.facts import AnalysisBindingError, load_analysis_facts
from geoharness.config.settings import GeoConfig
from geoharness.data.artifacts import sha256_file
from geoharness.task_state import begin_task


def produced(tmp_path, *, vector=False):
    directory = tmp_path / 'run'
    directory.mkdir()
    frame = pd.DataFrame({'id': range(234), 'population': [10]*142 + [None]*92})
    if vector:
        frame = gpd.GeoDataFrame(frame, geometry=[Point(i, 0) for i in range(234)], crs=3857)
        source = directory / 'all.gpkg'
        frame.to_file(source, driver='GPKG')
    else:
        source = directory / 'all.csv'
        frame.to_csv(source, index=False)
    subset = directory / ('subset.gpkg' if vector else 'subset.csv')
    selected = frame.iloc[[*range(8), *range(142, 156)]]
    if vector:
        selected.to_file(subset, driver='GPKG')
    else:
        selected.to_csv(subset, index=False)
    manifest = {'producer': 'geo_python', 'status': 'succeeded', 'exit_code': 0,
        'run_dir': str(directory), 'files': [{'path': str(p), 'sha256': sha256_file(p),
                                             'state': 'succeeded'} for p in (source, subset)]}
    (directory / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    begin_task(context.metadata, cwd=tmp_path, goal='核对全集与重点对象')
    return context, source, subset


def request(scope='全体对象'):
    return FactRequest(key='id', numeric_fields=['population'], scope_name=scope,
                       units={'population': '人'}, method={'operation': 'persisted_count'})


def test_full_and_subset_keep_separate_missing_denominators_and_reuse(tmp_path):
    context, source, subset = produced(tmp_path)
    whole = publish_package(context, source_ref=str(source), request=request())
    selected = publish_package(context, source_ref=str(subset), request=request('重点对象'))
    assert whole['facts']['row_count'] == 234
    assert whole['facts']['numeric']['population']['missing'] == 92
    assert selected['facts']['row_count'] == 22
    assert selected['facts']['numeric']['population']['missing'] == 14
    assert whole['selection']['id'] != selected['selection']['id']
    again = publish_package(context, source_ref=str(source), request=request())
    assert again['reused'] and again['package_path'] == whole['package_path']
    assert {key: value for key, value in again.items() if key != 'reused'} == {
        key: value for key, value in whole.items() if key != 'reused'}
    original = verify_package(Path(whole['package_path']))
    assert original['execution_evidence']['metric_crs'] is None
    assert original['analysis_correctness'] == 'unverified'


def test_nearest_publication_recomputes_geometry_rejects_wrong_ids_and_binds_reference(tmp_path):
    context, source, _ = produced(tmp_path, vector=True)
    frame = gpd.read_file(source)
    reference = tmp_path / 'shelters.gpkg'
    gpd.GeoDataFrame({'id': [7]}, geometry=[Point(1000, 0)], crs=3857).to_file(reference)

    def persist(values):
        frame['distance_m'] = values
        frame.to_file(source)
        manifest = source.parent / 'manifest.json'
        document = json.loads(manifest.read_text('utf-8'))
        next(entry for entry in document['files'] if entry['path'] == str(source))['sha256'] = (
            sha256_file(source))
        manifest.write_text(json.dumps(document), encoding='utf-8')

    facts = FactRequest(key='id', numeric_fields=['distance_m'], units={'distance_m': 'm'},
        method={'operation': 'nearest'}, nearest_checks=[{'field': 'distance_m',
        'reference_ref': str(reference), 'metric_crs': 'EPSG:3857'}])
    persist([1000]*234)
    with pytest.raises(ValueError, match='nearest_distance_mismatch'):
        publish_package(context, source_ref=str(source), request=facts)
    persist([1000-i for i in range(234)])
    result = publish_package(context, source_ref=str(source), request=facts)
    checked = verify_package(Path(result['package_path']))
    assert checked['nearest_checks'][0]['rows'] == 234
    assert checked['nearest_checks'][0]['status'] == 'passed'
    import shutil

    from geoharness.analysis.result_package import delivery_packages

    selected = delivery_packages(context.metadata, [source], {}, lambda value: Path(value))
    delivered = tmp_path / 'delivered'
    delivered.mkdir()
    copied = [str(shutil.copy2(path, delivered / path.name)) for path in selected]
    attached = _attached_analysis({'delivery_dir': str(delivered), 'files': copied})
    assert attached[0]['status'] == 'persisted_values_checked'
    assert attached[0]['execution_evidence']['nearest_distance_status'] == (
        'independently_recomputed')
    # Copied-reference edits cannot silently retain a verification claim.
    gpd.GeoDataFrame({'id': [7]}, geometry=[Point(900, 0)], crs=3857).to_file(
        delivered / reference.name)
    assert _attached_analysis({'delivery_dir': str(delivered), 'files': copied}
                              )[0]['status'] == 'unverified'
    # A reference edit invalidates publication even if output bytes are intact.
    gpd.GeoDataFrame({'id': [7]}, geometry=[Point(1100, 0)], crs=3857).to_file(reference)
    with pytest.raises(ValueError, match='nearest_distance_mismatch'):
        verify_package(Path(result['package_path']))


def test_map_binding_recomputes_package_and_rejects_unrelated_or_changed_cache(tmp_path):
    context, source, subset = produced(tmp_path, vector=True)
    package = publish_package(context, source_ref=str(source), request=request())

    def load(mapped):
        return load_analysis_facts([package['package_path']], cwd=tmp_path,
            read_roots=[tmp_path / '.geoharness/analysis'], mapped_sources=[mapped],
            display_crs='EPSG:3857')

    accepted = load({'file_path': str(source), 'gdf': gpd.read_file(source)})
    assert accepted['status'] == 'verified'
    stats = accepted['analyses'][0]['statistics']
    assert stats['numeric']['population']['missing'] == 92
    with pytest.raises(AnalysisBindingError):
        load({'file_path': str(subset)})
    changed = gpd.read_file(source)
    changed.loc[0, 'population'] = 999
    with pytest.raises(ValueError, match='缓存'):
        load({'file_path': str(source), 'gdf': changed})


def test_changed_fact_source_cannot_reuse_original_package(tmp_path):
    context, source, _ = produced(tmp_path)
    package = publish_package(context, source_ref=str(source), request=request())
    source.write_text('id,population\n0,999\n', encoding='utf-8')
    with pytest.raises(ValueError, match='fingerprint changed'):
        verify_package(Path(package['package_path']))


def test_package_history_is_preserved_without_an_arbitrary_eight_result_stop(tmp_path):
    from geoharness.task_state import TaskRecord, task_context

    context, source, _ = produced(tmp_path)
    paths = [publish_package(context, source_ref=str(source),
        request=request(f'已核实选集说明{i}'))['package_path'] for i in range(9)]
    record = TaskRecord.model_validate(context.metadata['geo_task_record'])
    assert record.analysis_packages == paths
    compact = task_context(context.metadata)
    summary, _ = json.JSONDecoder().raw_decode(compact[compact.index('{'):])
    assert summary['analysis_package_count'] == 9
    assert summary['analysis_packages'] == paths[-8:]


def test_delivered_package_facts_are_recomputed_from_copied_source(tmp_path):
    import shutil

    context, source, _ = produced(tmp_path)
    package = publish_package(context, source_ref=str(source), request=request())
    destination = tmp_path / 'exports'
    destination.mkdir()
    copied = []
    for original in (source, Path(package['package_path'])):
        target = destination / original.name
        shutil.copy2(original, target)
        copied.append(str(target))
    reports = _attached_analysis({'delivery_dir': str(destination), 'files': copied})
    assert reports[0]['status'] == 'persisted_values_checked'
    assert reports[0]['selection']['row_count'] == 234
    assert reports[0]['facts']['numeric']['population']['missing'] == 92
    (destination / source.name).write_text('id,population\n0,999\n', encoding='utf-8')
    assert _attached_analysis({'delivery_dir': str(destination), 'files': copied})[0][
        'status'] == 'unverified'


def test_each_produced_candidate_must_have_its_own_selection_binding(tmp_path):
    from geoharness.analysis.result_package import infer_packages, require_candidate_bindings

    context, source, subset = produced(tmp_path, vector=True)
    package = publish_package(context, source_ref=str(source), request=request())
    mapped = [{'file_path': str(source)}, {'file_path': str(subset)}]
    analysis = load_analysis_facts([package['package_path']], cwd=tmp_path,
        read_roots=[tmp_path / '.geoharness/analysis'], mapped_sources=mapped,
        display_crs='EPSG:3857')
    with pytest.raises(ValueError, match='分别 publish_analysis'):
        require_candidate_bindings(context.metadata, mapped, analysis)
    other = publish_package(context, source_ref=str(subset), request=request('重点对象'))
    unrelated = tmp_path / 'unrelated-broken.json'
    unrelated.write_text('broken-json', encoding='utf-8')
    context.metadata['geo_task_record']['analysis_packages'].append(str(unrelated))
    assert infer_packages(context.metadata, mapped) == [
        package['package_path'], other['package_path']]


def test_delivered_selection_and_units_cannot_be_asserted_without_rechecking(tmp_path):
    import shutil

    context, source, _ = produced(tmp_path)
    package = publish_package(context, source_ref=str(source), request=request())
    destination = tmp_path / 'exports'
    destination.mkdir()
    copied = [destination / source.name, destination / 'facts.json']
    shutil.copy2(source, copied[0])
    report = json.loads(Path(package['package_path']).read_text(encoding='utf-8'))
    report['selection']['row_count'] = 22
    copied[1].write_text(json.dumps(report), encoding='utf-8')
    delivery = {'delivery_dir': str(destination), 'files': [str(p) for p in copied]}
    assert _attached_analysis(delivery)[0]['status'] == 'unverified'
    report['selection']['row_count'] = 234
    report['units']['population'] = '户'
    copied[1].write_text(json.dumps(report), encoding='utf-8')
    assert _attached_analysis(delivery)[0]['status'] == 'unverified'
