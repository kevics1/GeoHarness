"""The map stage must work with QGIS disabled and defer its editable project."""

import hashlib
import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.cartography.scene import Scene
from geoharness.config.settings import GeoConfig
from geoharness.tools.geo_native_map import GeoNativeMapTool, NativeMapInput


def setup(tmp_path, *, enabled=False):
    config = GeoConfig()
    from dataclasses import replace

    config = replace(config, qgis=config.qgis.model_copy(update={'enabled': enabled}))
    source = tmp_path / 'places.gpkg'
    gpd.GeoDataFrame({'id': [2, 8, 6], 'name': ['西侧', '中部', '东侧'],
        'distance_m': [1., 2., 3.]}, geometry=[Point(0, 0), Point(1000, 500),
        Point(1800, 1300)], crs=3857).to_file(source)
    scene = Scene.model_validate({'title': '原生地图', 'crs': 'EPSG:3857', 'dpi': 72,
        'layers': [{'id': 'places', 'source_ref': str(source), 'key': 'id',
                    'label_field': 'id', 'name': '居民点'}],
        'pages': [{'id': 'overview', 'layers': ['places']},
            {'id': 'roster', 'role': 'roster', 'title': '完整名册', 'table': {
                'layer': 'places', 'fields': ['id', 'name', 'distance_m'],
                'headers': {'distance_m': '距离(m)'}, 'rows_per_page': 2}}]})
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': config})
    return context, scene


async def test_native_map_does_not_start_qgis_and_returns_all_pages(tmp_path, monkeypatch):
    context, scene = setup(tmp_path)

    async def forbidden(*args, **kwargs):
        raise AssertionError('QGIS cannot be used during native mapping')

    monkeypatch.setattr('geoharness.qgis.runner.execute_qgis', forbidden)
    tool = GeoNativeMapTool()
    result = await tool.execute(NativeMapInput(action='render', scene=scene), context)
    assert not result.is_error, result.output
    manifest = json.loads(Path(result.metadata['project_manifest']).read_text('utf-8'))
    assert manifest['backend'] == 'matplotlib'
    assert manifest['project_fidelity'] == 'native_scene_preview'
    assert len(manifest['native_facts']['page_previews']) == 3
    assert not list(tmp_path.rglob('*.qgz')) and not list(tmp_path.rglob('*.qgs'))
    assert Path(result.metadata['output_path']).is_file()
    repeated = await tool.execute(NativeMapInput(action='render',
        scene_ref=result.metadata['scene_ref']), context)
    assert not repeated.is_error, repeated.output
    assert repeated.metadata['reused'] is True


async def test_delivery_before_review_does_not_start_exporter(tmp_path, monkeypatch):
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    context, scene = setup(tmp_path)
    result = await GeoNativeMapTool().execute(NativeMapInput(action='render', scene=scene), context)
    assert not result.is_error, result.output
    called = []

    async def exporter(*args, **kwargs):
        called.append(True)
        raise AssertionError('Unreviewed scene must not generate an editable project')

    monkeypatch.setattr('geoharness.qgis.runner.execute_qgis', exporter)
    delivery = await GeoDeliverTool().execute(GeoDeliverInput(
        project_dir=result.metadata['project_dir']), context)
    assert delivery.is_error and 'delivery_not_ready' in delivery.output, delivery.output
    assert not called


async def test_roster_edit_retains_main_pixels_and_stable_page_id(tmp_path):
    context, scene = setup(tmp_path)
    tool = GeoNativeMapTool()
    first = await tool.execute(NativeMapInput(action='render', scene=scene), context)
    assert not first.is_error, first.output
    changed = await tool.execute(NativeMapInput(action='render',
        scene_ref=first.metadata['scene_ref'], edits=[{'target': 'page', 'id': 'roster',
            'changes': {'title': '修改名册标题'}}]), context)
    assert not changed.is_error, changed.output
    assert hashlib.sha256(Path(first.metadata['preview_path']).read_bytes()).digest() == (
        hashlib.sha256(Path(changed.metadata['preview_path']).read_bytes()).digest())
    assert changed.metadata['native_facts']['page_previews'][0]['page_id'] == 'overview'


async def test_source_change_invalidates_native_render_cache(tmp_path):
    context, scene = setup(tmp_path)
    tool = GeoNativeMapTool()
    first = await tool.execute(NativeMapInput(action='render', scene=scene), context)
    assert not first.is_error, first.output
    from geoharness.task_controller import _inventory

    inputs_before = _inventory({'scene_ref': first.metadata['scene_ref']}, context)
    source = Path(scene.layers[0].source_ref)
    frame = gpd.read_file(source)
    frame['name'] = ['变更', '中部', '东侧']
    frame.to_file(source)
    second = await tool.execute(NativeMapInput(action='render',
        scene_ref=first.metadata['scene_ref']), context)
    assert not second.is_error, second.output
    assert _inventory({'scene_ref': first.metadata['scene_ref']}, context) != inputs_before
    assert second.metadata['reused'] is False
    assert second.metadata['project_dir'] != first.metadata['project_dir']


@pytest.mark.parametrize('changes', [{'key': 'missing'}, {'select_keys': [999]}])
async def test_bad_entity_contract_reports_actual_fields(tmp_path, changes):
    context, scene = setup(tmp_path)
    raw = scene.model_dump()
    raw['layers'][0].update(changes)
    result = await GeoNativeMapTool().execute(NativeMapInput(action='render',
        scene=Scene.model_validate(raw)), context)
    assert result.is_error


@pytest.mark.parametrize('with_raster', [False, True])
async def test_reviewed_native_scene_generates_editable_project_once(tmp_path, with_raster):
    from geoharness.cartography.jobs import map_job_receipt, record_map_review
    from geoharness.qgis.discovery import discover_qgis
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    context, scene = setup(tmp_path, enabled=True)
    scene.layers[0].symbol.color = 'none'
    if with_raster:
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        source = tmp_path / 'elevation.tif'
        with rasterio.open(source, 'w', driver='GTiff', width=4, height=4, count=1,
            dtype='float32', crs=3857, transform=from_origin(-100, 1800, 500, 500)) as dest:
            dest.write(np.arange(16, dtype='float32').reshape(4, 4), 1)
        raw = scene.model_dump()
        raw['layers'].insert(0, {'id': 'dem', 'kind': 'raster', 'source_ref': str(source)})
        raw['pages'][0]['layers'].insert(0, 'dem')
        scene = Scene.model_validate(raw)
    try:
        discover_qgis(context.metadata['geoharness_config'].qgis, cwd=tmp_path)
    except (OSError, ValueError) as exc:
        pytest.skip(str(exc))
    rendered = await GeoNativeMapTool().execute(NativeMapInput(action='render', scene=scene),
                                               context)
    assert not rendered.is_error, rendered.output
    root = Path(rendered.metadata['project_dir'])
    config = context.metadata['geoharness_config']
    receipt = map_job_receipt(context.metadata, cwd=tmp_path, config=config,
                              artifact_path=Path(rendered.metadata['preview_path']))
    # Synthetic review only tests state transport; no AI/business-quality claim.
    for page in rendered.metadata['native_facts']['page_previews']:
        path = Path(page['path'])
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        record_map_review(receipt['job_id'], context.metadata, cwd=tmp_path, config=config,
            image_path=path, review={'status': 'passed', 'image_sha256': sha, 'issues': [],
                'diagnostics': {'page_visual_status': 'passed', 'review_binding': {
                    'version': receipt['selected_version'], 'page_index': page['page_index'],
                    'primary_image_sha256': receipt['image_sha256'], 'image_sha256': sha,
                    'manifest_sha256': hashlib.sha256((root / 'project.json').read_bytes()
                                                     ).hexdigest()}}})
    before = len(list(tmp_path.rglob('*.qgz')))
    assert before == 0
    args = GeoDeliverInput(project_dir=str(root))
    first = await GeoDeliverTool().execute(args, context)
    assert not first.is_error, first.output
    assert Path(first.metadata['qgis_project']).is_file()
    final_root = Path(first.metadata['qgis_project']).parent
    doc = json.loads((final_root / 'project.json').read_text('utf-8'))
    assert doc['project_fidelity'] == 'scene_bound'
    assert doc['render_backend'] == 'matplotlib'
    assert doc['project_validation']['pixel_equivalence'] == 'not_claimed'
    from geoharness.tools.geo_map_review import _verified_render_evidence
    old_roster = root / 'page-2.png'
    receipt = map_job_receipt(context.metadata, cwd=tmp_path, config=config,
                              map_id=receipt['job_id'])
    proof = _verified_render_evidence(old_roster,
        hashlib.sha256(old_roster.read_bytes()).hexdigest(), receipt, [tmp_path])
    assert proof['review_binding']['version'] == receipt['selected_version']
    assert proof['review_page_index'] == 1
    assert len(receipt['versions']) == 1

    fresh = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': config})
    second = await GeoDeliverTool().execute(args, fresh)
    assert not second.is_error, second.output
    generated = [p for p in tmp_path.rglob('*.qgz') if 'project' not in p.parts]
    assert len(generated) == 1
    assert Path(first.metadata['qgis_project']).read_bytes() == (
        Path(second.metadata['qgis_project']).read_bytes())


@pytest.mark.parametrize('status', ['passed', 'issues'])
async def test_unchanged_page_preserves_review_and_changed_roster_needs_review(tmp_path, status):
    from geoharness.cartography.jobs import map_job_receipt, record_map_review

    context, scene = setup(tmp_path)
    tool = GeoNativeMapTool()
    first = await tool.execute(NativeMapInput(action='render', scene=scene), context)
    config = context.metadata['geoharness_config']
    image = Path(first.metadata['preview_path'])
    sha = hashlib.sha256(image.read_bytes()).hexdigest()
    receipt = map_job_receipt(context.metadata, cwd=tmp_path, config=config, artifact_path=image)
    record_map_review(receipt['job_id'], context.metadata, cwd=tmp_path, config=config,
        image_path=image, review={'status': status, 'image_sha256': sha, 'issues': [],
            'diagnostics': {'page_visual_status': status}})
    changed = await tool.execute(NativeMapInput(action='render',
        scene_ref=first.metadata['scene_ref'], edits=[{'target': 'page', 'id': 'roster',
            'changes': {'title': '仅修改名册'}}]), context)
    receipt = map_job_receipt(context.metadata, cwd=tmp_path, config=config,
        artifact_path=Path(changed.metadata['preview_path']))
    pages = receipt['stages']['visual_checked']['page_reviews']
    assert pages[0]['status'] == status
    assert pages[1]['status'] == 'unverified'
    assert len(receipt['versions']) == 2


async def test_raster_and_numeric_roster_share_frozen_display_contract(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    context, scene = setup(tmp_path)
    raster = tmp_path / 'dem.tif'
    with rasterio.open(raster, 'w', driver='GTiff', width=4, height=4, count=1,
        dtype='float32', crs=3857, transform=from_origin(-100, 1800, 500, 500)) as dest:
        dest.write(np.arange(16, dtype='float32').reshape(4, 4), 1)
    raw = scene.model_dump()
    raw['layers'].insert(0, {'id': 'dem', 'kind': 'raster', 'source_ref': str(raster)})
    raw['pages'][0]['layers'].insert(0, 'dem')
    raw['pages'][1]['table']['decimals'] = {'distance_m': 0}
    result = await GeoNativeMapTool().execute(NativeMapInput(action='render', scene=raw), context)
    assert not result.is_error, result.output
    plan = json.loads((Path(result.metadata['project_dir']) / 'scene-plan.json').read_text('utf-8'))
    assert plan['layers'][0]['raster_display']['maximum'] == 15
    assert plan['pages'][1]['table']['rows'][0][2] == '1'


async def test_service_failure_binds_secondary_page_and_stops_batch(tmp_path):
    from geoharness.cartography.jobs import map_job_receipt
    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    context, scene = setup(tmp_path)
    class FailingService:
        def __init__(self):
            self.calls = 0
        async def stream_message(self, request):
            self.calls += 1
            raise TimeoutError('controlled transient service failure')
            yield
    client = FailingService()
    context.metadata['map_review_client'] = client
    rendered = await GeoNativeMapTool().execute(NativeMapInput(action='render', scene=scene),
                                               context)
    page = Path(rendered.metadata['project_dir']) / 'page-2.png'
    single = await GeoMapReviewTool().execute(GeoMapReviewInput(file_path=str(page)), context)
    assert not single.is_error, single.output
    assert single.metadata['registration_status'] == 'committed'
    assert single.metadata['review_binding']['page_index'] == 1
    assert single.metadata['review_status'] == 'unverified'
    batch = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=rendered.metadata['preview_path'], all_pages=True), context)
    assert not batch.is_error, batch.output
    assert batch.metadata['completed_page_count'] == 1
    assert client.calls == 1  # shared service cooldown, no per-page blind retries
    receipt = map_job_receipt(context.metadata, cwd=tmp_path,
        config=context.metadata['geoharness_config'], map_id=batch.metadata['map_id'])
    assert len(receipt['versions']) == 1
    assert receipt['stages']['visual_checked']['status'] == 'unverified'


async def test_multilayer_gpkg_requires_and_honors_actual_layer_name(tmp_path):
    context, scene = setup(tmp_path)
    source = Path(scene.layers[0].source_ref)
    other = gpd.GeoDataFrame({'other': [99]}, geometry=[Point(0, 0)], crs=3857)
    other.to_file(source, layer='unrelated', driver='GPKG', mode='a')
    missing = await GeoNativeMapTool().execute(
        NativeMapInput(action='render', scene=scene), context)
    assert missing.is_error and 'source_layer' in missing.output
    raw = scene.model_dump()
    raw['layers'][0]['source_layer'] = 'places'
    rendered = await GeoNativeMapTool().execute(NativeMapInput(action='render', scene=raw), context)
    assert not rendered.is_error, rendered.output
    assert len(rendered.metadata['native_facts']['page_previews']) == 3


async def test_processing_result_publish_native_render_and_image_tool_chain(tmp_path, monkeypatch):
    from test_map_receipt_workflow import PassingReviewer

    from geoharness.analysis.result_package import FactRequest, publish_package
    from geoharness.qgis.discovery import discover_qgis
    from geoharness.task_state import begin_task
    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool
    from geoharness.tools.geo_qgis import GeoQgisInput, GeoQgisTool

    context, scene = setup(tmp_path, enabled=True)
    try:
        discover_qgis(context.metadata['geoharness_config'].qgis, cwd=tmp_path)
    except (OSError, ValueError) as exc:
        pytest.skip(str(exc))
    begin_task(context.metadata, cwd=tmp_path, goal='标准Processing分析后的原生地图')
    tool = GeoQgisTool()
    help_result = await tool.execute(GeoQgisInput(action='help', algorithm_id='native:buffer'),
                                     context)
    assert not help_result.is_error, help_result.output
    processed = await tool.execute(GeoQgisInput(action='run', algorithm_id='native:buffer',
        parameters={'INPUT': scene.layers[0].source_ref, 'DISTANCE': 50,
            'SEGMENTS': 5, 'END_CAP_STYLE': 0, 'JOIN_STYLE': 0, 'MITER_LIMIT': 2,
            'DISSOLVE': False, 'OUTPUT': 'buffer.gpkg'}), context)
    assert not processed.is_error, processed.output
    output = processed.metadata['result']['outputs']['OUTPUT']
    package = publish_package(context, source_ref=output, request=FactRequest(key='id',
        numeric_fields=['distance_m'], method={'operation': 'native:buffer', 'distance_m': 50}))
    raw = scene.model_dump()
    raw['layers'][0]['source_ref'] = output
    raw['analysis_refs'] = [package['analysis_ref']]
    async def forbidden(*args, **kwargs):
        raise AssertionError('Cartography/review cannot call QGIS')
    monkeypatch.setattr('geoharness.qgis.runner.execute_qgis', forbidden)
    rendered = await GeoNativeMapTool().execute(NativeMapInput(action='render', scene=raw), context)
    assert not rendered.is_error, rendered.output
    reviewer = PassingReviewer()
    context.metadata['map_review_client'] = reviewer
    reviewed = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=rendered.metadata['preview_path'], all_pages=True), context)
    assert not reviewed.is_error, reviewed.output
    assert len(reviewer.requests) == 3  # simulated provider, real tool page binding
    from geoharness.cartography.jobs import map_job_receipt

    receipt = map_job_receipt(context.metadata, cwd=tmp_path,
        config=context.metadata['geoharness_config'], map_id=reviewed.metadata['map_id'])
    assert receipt['stages']['visual_checked']['status'] == 'passed'
    assert not list(tmp_path.rglob('*.qgz'))


async def test_legacy_delivery_spec_without_review_cannot_start_qgis_preview(tmp_path, monkeypatch):
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

    context, scene = setup(tmp_path, enabled=True)
    spec = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action='spec',
        layers=[{'file_path': scene.layers[0].source_ref}]), context)
    assert not spec.is_error, spec.output
    async def forbidden(*args, **kwargs):
        raise AssertionError('Legacy spec cannot generate an unreviewed QGIS preview')
    monkeypatch.setattr('geoharness.qgis.runner.execute_qgis', forbidden)
    delivered = await GeoDeliverTool().execute(GeoDeliverInput(
        spec_ref=spec.metadata['spec_ref']), context)
    assert delivered.is_error and 'delivery_not_ready' in delivered.output
    assert not list(tmp_path.rglob('*.qgz'))


async def test_page_layer_order_controls_actual_pixels(tmp_path):
    import numpy as np
    from PIL import Image
    from shapely.geometry import box

    context, _ = setup(tmp_path)
    source = tmp_path / 'square.geojson'
    gpd.GeoDataFrame(geometry=[box(0, 0, 100, 100)], crs=3857).to_file(source)
    scene = {'crs': 'EPSG:3857', 'dpi': 72, 'layers': [
        {'id': 'red', 'source_ref': str(source), 'symbol': {'color': '#ff0000'}},
        {'id': 'blue', 'source_ref': str(source), 'symbol': {'color': '#0000ff'}}],
        'pages': [{'id': 'main', 'layers': ['blue', 'red']}]}
    result = await GeoNativeMapTool().execute(NativeMapInput(action='render', scene=scene), context)
    assert not result.is_error, result.output
    plan = json.loads((Path(result.metadata['project_dir']) / 'scene-plan.json').read_text('utf-8'))
    x, y, w, h = plan['pages'][0]['frame']['rect_mm']
    pixels = np.array(Image.open(result.metadata['preview_path']).convert('RGB'))
    actual = pixels[round((y + h / 2) * 72 / 25.4), round((x + w / 2) * 72 / 25.4)]
    assert actual.tolist() == [255, 0, 0]


async def test_vrt_dependency_change_invalidates_page_and_controller_cache(tmp_path):
    import numpy as np
    import rasterio
    from rasterio.shutil import copy as raster_copy
    from rasterio.transform import from_origin

    from geoharness.task_controller import _inventory

    context, scene = setup(tmp_path)
    source, vrt = tmp_path / 'pixels.tif', tmp_path / 'display.vrt'
    options = dict(driver='GTiff', width=4, height=4, count=1, dtype='float32',
        crs=3857, transform=from_origin(-100, 1800, 500, 500))
    values = np.arange(16, dtype='float32').reshape(4, 4)
    with rasterio.open(source, 'w', **options) as dest:
        dest.write(values, 1)
    raster_copy(source, vrt, driver='VRT')
    raw = scene.model_dump()
    raw['layers'].insert(0, {'id': 'dem', 'kind': 'raster', 'source_ref': str(vrt)})
    raw['pages'][0]['layers'].insert(0, 'dem')
    tool = GeoNativeMapTool()
    first = await tool.execute(NativeMapInput(action='render', scene=raw), context)
    assert not first.is_error, first.output
    arguments = {'scene_ref': first.metadata['scene_ref']}
    before = _inventory(arguments, context)
    vrt_hash = hashlib.sha256(vrt.read_bytes()).hexdigest()
    with rasterio.open(source, 'w', **options) as dest:
        dest.write(values[::-1], 1)
    assert hashlib.sha256(vrt.read_bytes()).hexdigest() == vrt_hash
    assert _inventory(arguments, context) != before
    second = await tool.execute(NativeMapInput(action='render', **arguments), context)
    assert not second.is_error, second.output
    assert second.metadata['reused'] is False
    assert second.metadata['native_facts']['page_previews'][0]['reused'] is False
    assert Path(first.metadata['preview_path']).read_bytes() != (
        Path(second.metadata['preview_path']).read_bytes())


async def test_render_cache_can_register_after_failed_receipt_write(tmp_path, monkeypatch):
    from geoharness.cartography import jobs

    context, scene = setup(tmp_path)
    tool = GeoNativeMapTool()
    spec = await tool.execute(NativeMapInput(action='spec', scene=scene), context)
    original = jobs.record_map_version
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(True)
        raise OSError('controlled receipt write failure')

    monkeypatch.setattr(jobs, 'record_map_version', interrupted)
    arguments = NativeMapInput(action='render', scene_ref=spec.metadata['scene_ref'])
    first = await tool.execute(arguments, context)
    assert first.is_error and calls
    monkeypatch.setattr(jobs, 'record_map_version', original)
    recovered = await tool.execute(arguments, context)
    assert not recovered.is_error, recovered.output
    assert recovered.metadata['reused'] is True
    assert recovered.metadata['map_receipt']['artifact_registered'] is True
    assert len(recovered.metadata['map_receipt']['versions']) == 1
