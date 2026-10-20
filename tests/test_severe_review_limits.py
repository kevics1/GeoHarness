"""Only severe visual defects justify repairs, with durable bounded requests."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.tools.base import ToolExecutionContext
from test_native_scene_workflow import setup

from geoharness.cartography.quality import MapIssue, MapReview, distinguish_pixel_findings
from geoharness.cartography.review_limits import reserve_review_request, review_limit_status
from geoharness.cartography.workflow import reset_map_workflow
from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool
from geoharness.tools.geo_native_map import GeoNativeMapTool, NativeMapInput


class ReviewClient:
    def __init__(self, severity='error'):
        self.severity, self.calls = severity, 0

    async def stream_message(self, request):
        self.calls += 1
        payload = {'status': 'issues', 'summary': '检查完成', 'issues': [{
            'severity': self.severity, 'description': '主图关键图例无法辨认',
            'reading_impact': 'unreadable', 'affected_content': '主图关键图例',
            'location': '右上图例', 'issue_code': 'legend_mismatch'}]}
        yield ApiMessageCompleteEvent(message=ConversationMessage.from_user_text(
            json.dumps(payload)), usage=UsageSnapshot())


def task_context(tmp_path):
    context, scene = setup(tmp_path)
    context.metadata['geo_task_record'] = {'task_id': str(uuid4()), 'workspace': str(tmp_path)}
    scene.pages = scene.pages[:1]
    return context, scene


@pytest.mark.parametrize('severity,status', [('warning', 'passed'), ('info', 'passed'),
                                          ('error', 'issues')])
def test_only_error_blocks_automatic_repair(severity, status):
    review = MapReview(status='issues', summary='结果', issues=[MapIssue(
        severity=severity, description='局部观察', reading_impact='unreadable')])
    distinguish_pixel_findings(review)
    assert review.status == status
    assert review.issues[0].severity == severity


async def test_warning_passes_ledger_and_never_becomes_pending_finding(tmp_path):
    from geoharness.cartography.review_findings import pending_findings

    context, scene = task_context(tmp_path)
    client = ReviewClient('warning')
    context.metadata['map_review_client'] = client
    native = await GeoNativeMapTool().execute(NativeMapInput(action='render', scene=scene), context)
    checked = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=native.metadata['preview_path'], all_pages=True), context)
    assert not checked.is_error, checked.output
    assert checked.metadata['status'] == 'passed', checked.output
    assert checked.metadata['pages'][0]['issues'][0]['severity'] == 'warning'
    assert checked.metadata['quality_status'] != 'issues'
    assert not pending_findings(context.metadata, cwd=tmp_path,
                                map_ids=[native.metadata['map_id']])
    assert client.calls == 1


async def test_three_actual_reviews_then_twenty_attempts_cannot_redraw_or_request(tmp_path):
    from geoharness.cartography.review_findings import pending_findings
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    context, scene = task_context(tmp_path)
    client = ReviewClient()
    context.metadata['map_review_client'] = client
    tool = GeoNativeMapTool()
    native = await tool.execute(NativeMapInput(action='render', scene=scene), context)
    for n in range(3):
        if n:
            native = await tool.execute(NativeMapInput(action='render',
                scene_ref=native.metadata['scene_ref'], edits=[{'target': 'page',
                    'id': 'overview', 'changes': {'title': f'局部修改{n}'}}]), context)
        assert not native.is_error, native.output
        checked = await GeoMapReviewTool().execute(GeoMapReviewInput(
            file_path=native.metadata['preview_path'], all_pages=True), context)
        assert checked.metadata['status'] == 'issues', checked.output
    assert client.calls == 3
    assert checked.metadata['automatic_review_stopped']
    retained = checked.metadata['pages'][0]['report_path']
    from pathlib import Path

    report_bytes = Path(retained).read_bytes()
    # Same-task reconstruction models process restart; no volatile workflow counters.
    fresh = ToolExecutionContext(cwd=tmp_path, metadata={key: context.metadata[key] for key in
        ('geo_task_record', 'geoharness_config', 'map_review_client')})
    for _ in range(20):
        blocked = await tool.execute(NativeMapInput(action='render',
            scene_ref=native.metadata['scene_ref'], edits=[{'target': 'page',
                'id': 'overview', 'changes': {'title': '再改一点'}}]), fresh)
        assert not blocked.metadata['render_executed'], blocked.output
        assert blocked.metadata['automatic_review_stopped']
    # Cache of the actual third report remains reusable without spending a request.
    repeated = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=native.metadata['preview_path'], all_pages=True), fresh)
    assert repeated.metadata['status'] == 'issues', repeated.output
    assert client.calls == 3 and Path(retained).read_bytes() == report_bytes
    assert pending_findings(fresh.metadata, cwd=tmp_path, map_ids=[native.metadata['map_id']])
    delivery = await GeoDeliverTool().execute(GeoDeliverInput(
        project_dir=native.metadata['project_dir']), fresh)
    assert delivery.is_error and 'delivery_not_ready' in delivery.output


async def test_total_limit_durable_atomic_and_cannot_reset_by_map_names(tmp_path):
    context, _ = task_context(tmp_path)
    config = context.metadata['geoharness_config']
    config = replace(config, map_review=config.map_review.model_copy(update={
        'max_review_requests_per_task': 5}))
    context.metadata['geoharness_config'] = config
    root = tmp_path.resolve()
    directory = (root / config.agent.task_state_dir).resolve()
    assert directory.is_relative_to(root), (str(root), str(directory))
    with ThreadPoolExecutor(max_workers=8) as pool:
        reserved = list(pool.map(lambda n: reserve_review_request(context.metadata, config,
                                                                page=n)[0], range(20)))
    assert sum(reserved) == 5
    reset_map_workflow(context.metadata)
    fresh = {key: context.metadata[key] for key in ('geo_task_record', 'geoharness_config')}
    assert review_limit_status(fresh, config)['review_requests_used'] == 5
    assert not reserve_review_request(fresh, config, page=999)[0]


async def test_failed_requests_count_but_cached_failures_do_not(tmp_path):
    from PIL import Image

    from geoharness.cartography.workflow import review_with_budget

    context, _ = task_context(tmp_path)

    class Broken:
        calls = 0

        async def stream_message(self, request):
            self.calls += 1
            raise ValueError('bad response')
            yield  # pragma: no cover

    client = Broken()
    for n in range(4):
        image = tmp_path / f'changed-{n}.png'
        Image.new('RGB', (32, 32), (n * 40, 0, 0)).save(image)
        result, flags = await review_with_budget(image,
            config=context.metadata['geoharness_config'], task='像素检查', evidence={},
            metadata=context.metadata, client=client, pixel_only=True)
        assert result.status == 'unverified'
    assert client.calls == 3
    assert flags['review_budget_exhausted'] and not flags['request_sent']
    assert review_limit_status(context.metadata, context.metadata['geoharness_config'])[
        'review_requests_used'] == 3
    await asyncio.sleep(0)


async def test_limit_blocks_python_drawing_but_keeps_spatial_analysis_available(tmp_path):
    from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool

    context, _ = task_context(tmp_path)
    config = context.metadata['geoharness_config']
    for _ in range(3):
        assert reserve_review_request(context.metadata, config, page=0)[0]
    blocked = await GeoPythonTool().execute(GeoPythonInput(name='renamed-map',
        code="import matplotlib.pyplot as plt\nplt.savefig('attempt.png')"), context)
    assert blocked.metadata['automatic_review_stopped']
    assert blocked.metadata['script_executed'] is False
    analysis = await GeoPythonTool().execute(GeoPythonInput(name='analysis',
        code="CONSOLE_RESULT = {'count': 2 + 3}"), context)
    assert not analysis.is_error, analysis.output


def test_page_passing_last_attempt_does_not_halt_other_pages(tmp_path):
    from geoharness.cartography.review_limits import record_review_outcome

    context, _ = task_context(tmp_path)
    config = context.metadata['geoharness_config']
    for _ in range(3):
        assert reserve_review_request(context.metadata, config, page=0)[0]
    record_review_outcome(context.metadata, page=0, status='passed')
    assert not review_limit_status(context.metadata, config)['automatic_review_stopped']
    assert reserve_review_request(context.metadata, config, page=1)[0]
    # Changing the approved page still cannot buy it a fourth image request.
    allowed, flags = reserve_review_request(context.metadata, config, page=0)
    assert not allowed and flags['automatic_review_stopped']
