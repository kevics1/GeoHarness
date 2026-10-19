"""User-visible rendering never writes an editable project before delivery."""

import hashlib
import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def test_native_create_requests_preview_without_portable_layer_snapshots(
    tmp_path, monkeypatch,
):
    source = tmp_path / 'points.geojson'
    gpd.GeoDataFrame({'id': [7]}, geometry=[Point(1000, 1000)], crs=3857).to_file(source)
    captured = []

    async def runner(action, payload, **kwargs):
        captured.append((action, payload))
        return {'status': 'succeeded', 'result': {}}

    monkeypatch.setattr('geoharness.qgis.runner.execute_qgis', runner)
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig(
        qgis=GeoConfig().qgis.model_copy(update={
            'allow_project_previews': True, 'allow_console_layouts': True}))})
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action='create', layers=[{'file_path': str(source)}]), context)
    # This test stubs the native worker; its empty result cannot be treated as
    # a successful map materialization by any caller.
    assert result.is_error
    assert captured[0][0] == 'project_create'
    assert captured[0][1]['_preview_only'] is True
    assert not list(tmp_path.rglob('*.qgz'))


def test_preflight_reports_actionable_conflict_without_six_inspect_roundtrips():
    from geoharness.qgis.prepared import summary

    packet = summary({'status': 'needs_repair', 'frame_checks': {'symbol_constraints': {
        'feasible': False, 'unresolved': [{'code': 'dense_point_labels',
            'layer_indices': [1, 3], 'coincident_anchor_pairs': 13,
            'recommendation': '排除背景层重复显示的重点点位，保留真实位置'}]}}},
        'qgis-plan:' + 'a' * 64, Path('report.json'))
    assert packet['blocking_issues'][0]['code'] == 'dense_point_labels'
    assert packet['blocking_issues'][0]['layer_indices'] == [1, 3]
    assert packet['blocking_issues'][0]['recommendation']
    assert len(json.dumps(packet, ensure_ascii=False)) < 2400


def test_render_summary_keeps_page_paths_without_repeating_full_label_evidence():
    from geoharness.qgis.prepared import execution_summary

    result = {'status': 'succeeded', 'result': {'project_manifest': 'project.json',
        'preview_path': 'preview.png', 'layout': {'pages': 2,
            'bounds_check': {'checked': True, 'valid': True, 'problems': [],
                             'content_metrics': ['large detail'] * 10000}},
        'native_facts': {'page_previews': [{'page_index': i, 'path': f'page{i}.png',
            'frames': [], 'role': 'roster', 'labels': {'map': ['full label'] * 10000}}
            for i in range(2)]}}}
    compact = execution_summary(result)
    visible = compact['result']
    assert visible['page_count'] == 2
    assert visible['page_previews'][1]['path'] == 'page1.png'
    assert len(json.dumps(compact)) < 2400
    assert len(result['result']['native_facts']['page_previews'][0]['labels']['map']) == 10000


async def test_native_multipage_preview_delivery_and_repeat_share_one_project(tmp_path):
    from geoharness.cartography.jobs import map_job_receipt, record_map_review
    from geoharness.qgis.discovery import discover_qgis
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    config = GeoConfig(qgis=GeoConfig().qgis.model_copy(
        update={'allow_project_previews': True, 'allow_console_layouts': True}))
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f'QGIS unavailable: {exc}')
    source = tmp_path / 'points.gpkg'
    gpd.GeoDataFrame({'id': [71, 42], 'name': ['西侧居民点', '东侧居民点']},
        geometry=[Point(1000, 1000), Point(2000, 2000)], crs=3857).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': config})
    tool = GeoQgisProjectTool()
    spec = await tool.execute(GeoQgisProjectInput(action='spec', dpi=36,
        title='冻结版本验证', crs='EPSG:3857', layers=[{'file_path': str(source),
        'name': '居民点', 'layer_key': 'places'}], layout={'label_table': {
            'layer_index': 'places', 'fields': ['id', 'name'], 'placement': 'pages'}}), context)
    assert not spec.is_error, spec.output
    built = await tool.execute(GeoQgisProjectInput(action='build',
        spec_ref=spec.metadata['spec_ref']), context)
    assert not built.is_error, built.output
    root = Path(built.metadata['project_dir'])
    manifest = json.loads((root / 'project.json').read_text(encoding='utf-8'))
    pages = manifest['native_facts']['page_previews']
    assert len(pages) == 2
    assert manifest['project_fidelity'] == 'preview_only'
    assert manifest['creation_spec']['source_inventory'][str(source)] == hashlib.sha256(
        source.read_bytes()).hexdigest()
    assert not list(tmp_path.rglob('*.qgz'))
    assert not list(root.rglob('*.gpkg'))
    before = map_job_receipt(context.metadata, cwd=tmp_path, config=config,
                            artifact_path=Path(built.metadata['preview_path']))
    # Simulated review receipts exercise binding transfer, without calling AI or
    # claiming this fixture received a real visual/business review.
    for page in pages:
        image = root / page['path']
        record_map_review(before['job_id'], context.metadata, cwd=tmp_path, config=config,
            image_path=image, review={'status': 'passed', 'image_sha256': hashlib.sha256(
                image.read_bytes()).hexdigest(), 'issues': [], 'diagnostics': {
                'page_visual_status': 'passed', 'review_binding': {
                    'version': before['selected_version'], 'page_index': page['page_index'],
                    'primary_image_sha256': before['image_sha256'],
                    'manifest_sha256': hashlib.sha256((root / 'project.json').read_bytes()
                                                     ).hexdigest()}}})
    delivered = await GeoDeliverTool().execute(GeoDeliverInput(project_dir=str(root)), context)
    assert not delivered.is_error, delivered.output
    assert Path(delivered.metadata['qgis_project']).is_file()
    after = map_job_receipt(context.metadata, cwd=tmp_path, config=config,
                           artifact_path=Path(built.metadata['preview_path']))
    assert before['selected_version'] == after['selected_version']
    assert len(before['versions']) == len(after['versions']) == 1
    assert after['stages']['packaged']['status'] == 'passed'
    assert after['stages']['visual_checked']['status'] == 'passed'
    assert len(after['stages']['visual_checked']['page_reviews']) == 2
    # Count native generation, excluding the deliberate copy in each export folder.
    generated = [p for p in tmp_path.rglob('*.qgz') if 'exports' not in p.parts]
    assert len(generated) == 1
    repeated = await GeoDeliverTool().execute(GeoDeliverInput(project_dir=str(root)), context)
    assert not repeated.is_error, repeated.output
    assert [p for p in tmp_path.rglob('*.qgz') if 'exports' not in p.parts] == generated
    assert Path(repeated.metadata['qgis_project']).read_bytes() == generated[0].read_bytes()
    # Delivery by the exact immutable spec must also reuse across a process
    # restart, where the in-memory render cache no longer exists.
    fresh = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': config})
    by_spec = await GeoDeliverTool().execute(GeoDeliverInput(
        spec_ref=built.metadata['latest_spec_ref']), fresh)
    assert not by_spec.is_error, by_spec.output
    assert [p for p in tmp_path.rglob('*.qgz') if 'exports' not in p.parts] == generated
    assert Path(by_spec.metadata['qgis_project']).read_bytes() == generated[0].read_bytes()
    # A completed self-contained version is reusable even if its original
    # business input subsequently changes; it never substitutes those new data.
    changed = gpd.read_file(source)
    changed['name'] = ['changed', 'changed']
    changed.to_file(source)
    reused = await GeoDeliverTool().execute(GeoDeliverInput(project_dir=str(root)), context)
    assert not reused.is_error, reused.output
    assert [p for p in tmp_path.rglob('*.qgz') if 'exports' not in p.parts] == generated
    files_only = await GeoDeliverTool().execute(GeoDeliverInput(
        files=[built.metadata['preview_path']]), context)
    assert not files_only.is_error, files_only.output
    assert Path(files_only.metadata['qgis_project']).read_bytes() == generated[0].read_bytes()
    restarted = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': config})
    restored = await GeoDeliverTool().execute(GeoDeliverInput(project_dir=str(root)), restarted)
    assert not restored.is_error, restored.output
    assert Path(restored.metadata['qgis_project']).read_bytes() == generated[0].read_bytes()
    assert [p for p in tmp_path.rglob('*.qgz') if 'exports' not in p.parts] == generated
    # Validate the relocated editable package with the real native reader.
    inspected = await tool.execute(GeoQgisProjectInput(action='inspect',
        project_path=repeated.metadata['qgis_project']), context)
    assert not inspected.is_error, inspected.output


@pytest.mark.parametrize('change_source', [False, True])
async def test_user_authored_export_without_data_contract_cannot_materialize(
    tmp_path, change_source,
):
    from geoharness.qgis.discovery import discover_qgis
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool
    from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool

    config = GeoConfig(qgis=GeoConfig().qgis.model_copy(
        update={'allow_project_previews': True, 'allow_console_layouts': True}))
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f'QGIS unavailable: {exc}')
    source = tmp_path / 'places.gpkg'
    gpd.GeoDataFrame({'id': [1, 2]}, geometry=[Point(0, 0), Point(1000, 1000)],
                     crs=3857).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': config})
    # No GeoHarness custom font/layout properties: this represents a user file.
    created = await GeoPythonTool().execute(GeoPythonInput(backend='qgis',
        input_refs={'places': str(source)}, code="""from qgis.core import (
QgsVectorLayer, QgsPrintLayout, QgsLayoutItemMap, QgsLayoutItemLabel,
QgsLayoutPoint, QgsLayoutSize, QgsCoordinateReferenceSystem)
layer=QgsVectorLayer(str(INPUTS['places']), '居民点', 'ogr')
Project.addMapLayer(layer)
Project.setCrs(QgsCoordinateReferenceSystem('EPSG:3857'))
layout=QgsPrintLayout(Project)
layout.initializeDefaults()
layout.setName('User layout')
frame=QgsLayoutItemMap(layout)
layout.addLayoutItem(frame)
frame.attemptMove(QgsLayoutPoint(10,25))
frame.attemptResize(QgsLayoutSize(160,140))
frame.setLayers([layer])
frame.zoomToExtent(layer.extent())
label=QgsLayoutItemLabel(layout)
label.setText('原生用户工程')
layout.addLayoutItem(label)
label.attemptMove(QgsLayoutPoint(10,10))
label.adjustSizeToText()
Project.layoutManager().addLayout(layout)
assert Project.write(str(RUN_DIR/'user.qgz'))
"""), context)
    assert not created.is_error, created.output
    original = Path(created.metadata['run_dir']) / 'user.qgz'
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    tool = GeoQgisProjectTool()
    exported = await tool.execute(GeoQgisProjectInput(action='export',
        project_path=str(original), layout_name='User layout', dpi=36), context)
    assert not exported.is_error, exported.output
    preview = Path(exported.metadata['project_dir'])
    manifest = json.loads((preview / 'project.json').read_text('utf-8'))
    assert manifest['project_fidelity'] == 'preview_only'
    assert not list(preview.rglob('*.qgz'))
    # Compatibility export still requires a bound review before finalization.
    # This is synthetic evidence, not a real visual/business acceptance.
    from geoharness.cartography.jobs import map_job_receipt, record_map_review

    receipt = map_job_receipt(context.metadata, cwd=tmp_path, config=config,
        artifact_path=Path(exported.metadata['preview_path']))
    for page in manifest['native_facts']['page_previews']:
        image = preview / page['path']
        record_map_review(receipt['job_id'], context.metadata, cwd=tmp_path, config=config,
            image_path=image, review={'status': 'passed', 'image_sha256': hashlib.sha256(
                image.read_bytes()).hexdigest(), 'issues': [], 'diagnostics': {
                'page_visual_status': 'passed', 'review_binding': {
                    'version': receipt['selected_version'], 'page_index': page['page_index'],
                    'primary_image_sha256': receipt['image_sha256'],
                    'manifest_sha256': hashlib.sha256((preview / 'project.json').read_bytes()
                                                     ).hexdigest()}}})
    if change_source:
        changed = gpd.read_file(source)
        changed['id'] = [8, 9]
        changed.to_file(source)
    delivered = await GeoDeliverTool().execute(GeoDeliverInput(project_dir=str(preview)), context)
    # A visually accepted imported project still has no verified data contract.
    # It must not generate a second project, even when its sources also changed.
    assert delivered.is_error and 'delivery_not_ready' in delivered.output
    assert len(list(tmp_path.rglob('*.qgz'))) == 1
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('change', ['', 'source', 'second_page', 'preview_manifest'])
def test_materialization_requires_same_frozen_sources_and_every_page(tmp_path, change):
    from geoharness.cartography.jobs import (
        ensure_map_job,
        map_job_receipt,
        record_map_materialization,
        record_map_version,
    )

    config, metadata = GeoConfig(), {}
    identity = ensure_map_job(metadata, cwd=tmp_path, config=config)

    def package(name, preview):
        root = tmp_path / name
        root.mkdir()
        (root / 'preview.png').write_bytes(b'page 0')
        (root / 'page1.png').write_bytes(b'page 1')
        files = ['preview.png', 'page1.png', 'project.json']
        if not preview:
            (root / 'map.qgz').write_bytes(b'editable package')
            files.append('map.qgz')
        document = {'backend': 'qgis', 'project_fidelity': (
            'preview_only' if preview else 'saved_layout'), 'preview': 'preview.png',
            'preview_sha256': hashlib.sha256(b'page 0').hexdigest(), 'files': files,
            'layout_plan': {'source_identities': [{'data_sha256': 'original'}],
                            'input_digest': 'frozen-input'},
            'native_facts': {'page_previews': [
                {'page_index': 0, 'path': 'preview.png', 'frames': ['main']},
                {'page_index': 1, 'path': 'page1.png', 'frames': []}]}}
        if not preview:
            document['project_file'] = 'map.qgz'
        (root / 'project.json').write_text(json.dumps(document), encoding='utf-8')
        return root, document

    preview, _ = package('preview', True)
    record_map_version(identity, metadata, cwd=tmp_path, config=config,
                       image_path=preview / 'preview.png', project_dir=preview,
                       preflight=None, layout=None)
    final, document = package('final', False)
    if change == 'source':
        document['layout_plan']['source_identities'][0]['data_sha256'] = 'changed'
        (final / 'project.json').write_text(json.dumps(document), encoding='utf-8')
    elif change == 'second_page':
        (final / 'page1.png').write_bytes(b'different second page')
    elif change == 'preview_manifest':
        (preview / 'project.json').write_text('{}', encoding='utf-8')
    kwargs = {'cwd': tmp_path, 'config': config, 'image_path': preview / 'preview.png',
              'project_dir': final, 'selected_version': 1}
    if change:
        with pytest.raises(ValueError):
            record_map_materialization(identity, metadata, **kwargs)
    else:
        result = record_map_materialization(identity, metadata, **kwargs)
        assert result['stages']['packaged']['status'] == 'passed'
        assert result['selected_version'] == result['latest_version'] == 1
    receipt = map_job_receipt(metadata, cwd=tmp_path, config=config, map_id=identity)
    assert len(receipt['versions']) == 1
