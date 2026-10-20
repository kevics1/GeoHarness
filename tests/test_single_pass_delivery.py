"""One review round, one repair and truthful editable delivery, without a live model."""
import json
from dataclasses import replace

from openharness.hooks.executor import HookExecutionContext
from openharness.hooks.loader import HookRegistry
from test_incremental_review_schedule import Reviewer
from test_native_scene_workflow import setup

from geoharness.cartography.review_policy import state
from geoharness.hooks.executor import GeoHookExecutor
from geoharness.task_state import begin_task
from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool
from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool
from geoharness.tools.geo_native_map import GeoNativeMapTool, NativeMapInput


async def test_single_round_repair_and_qgis_delivery(tmp_path):
    context, scene = setup(tmp_path, enabled=True)
    begin_task(context.metadata, cwd=tmp_path, goal='单轮策略工程冒烟')
    context = replace(context, hook_executor=GeoHookExecutor(HookRegistry(),
        HookExecutionContext(cwd=tmp_path, api_client=None, default_model='test')))
    reviewer = Reviewer()
    reviewer.status = 'issues'
    context.metadata['map_review_client'] = reviewer
    tool = GeoNativeMapTool()
    first = await tool.execute(NativeMapInput(action='render', scene=scene), context)
    assert not first.is_error, first.output
    again = await tool.execute(NativeMapInput(action='render',
        scene_ref=first.metadata['scene_ref']), context)
    assert not again.is_error and not json.loads(again.output)['render_executed']
    review_tool = GeoMapReviewTool()
    checked = await review_tool.execute(GeoMapReviewInput(
        file_path=first.metadata['preview_path']), context)
    assert not checked.is_error, checked.output
    calls = reviewer.calls
    assert calls == 3, checked.output
    fixed = await tool.execute(NativeMapInput(action='render',
        scene_ref=first.metadata['scene_ref'], edits=[{'target': 'page', 'id': 'overview',
        'changes': {'title': '合并修改'}}]), context)
    assert not fixed.is_error, fixed.output
    repeated = await review_tool.execute(GeoMapReviewInput(
        file_path=fixed.metadata['preview_path']), context)
    assert not repeated.is_error and reviewer.calls == calls
    blocked = await tool.execute(NativeMapInput(action='render',
        scene_ref=fixed.metadata['scene_ref']), context)
    assert not blocked.is_error and not json.loads(blocked.output)['render_executed']
    restarted = {'geo_task_record': dict(context.metadata['geo_task_record']),
                 'geoharness_config': context.metadata['geoharness_config']}
    assert state(restarted)['repair_rendered']
    delivery = await GeoDeliverTool().execute(GeoDeliverInput(
        project_dir=fixed.metadata['project_dir']), context)
    assert not delivery.is_error, delivery.output
    assert delivery.metadata['qgis_project'].endswith('.qgz'), delivery.output
    assert delivery.metadata['review_workflow'] == 'single_pass'
    assert delivery.metadata['post_repair_visual_review'] == 'not_performed'
    assert delivery.metadata['quality_status'] != 'passed', delivery.output
