"""Exercise real multi-page rendering and count actual replacement reviewer requests."""

import json

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.tools.base import ToolExecutionContext
from test_native_scene_workflow import setup

from geoharness.cartography.scene import Page
from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool
from geoharness.tools.geo_native_map import GeoNativeMapTool, NativeMapInput


class Reviewer:
    def __init__(self):
        self.calls = 0
        self.status = 'passed'

    async def stream_message(self, request):
        self.calls += 1
        yield ApiMessageCompleteEvent(message=ConversationMessage.from_user_text(json.dumps({
            'status': self.status, 'summary': '模拟图片已检查', 'issues': [
                {'severity': 'warning', 'issue_code': 'label_overlap',
                 'description': '模拟标签重叠', 'location': '图中上部'}
            ] if self.status == 'issues' else []})), usage=UsageSnapshot())


async def rendered_four_pages(tmp_path):
    context, scene = setup(tmp_path)
    scene.pages.insert(1, Page(id='detail', role='detail', title='重点详图', layers=['places']))
    tool = GeoNativeMapTool()
    result = await tool.execute(NativeMapInput(action='render', scene=scene), context)
    assert not result.is_error, result.output
    reviewer = Reviewer()
    context.metadata['map_review_client'] = reviewer
    return context, result, reviewer


async def test_first_four_then_two_changed_pages_and_no_repeated_notes(tmp_path):
    context, first, reviewer = await rendered_four_pages(tmp_path)
    tool = GeoMapReviewTool()
    result = await tool.execute(GeoMapReviewInput(file_path=first.metadata['preview_path'],
        checks=['检查全部页面，包括名册'], all_pages=True), context)
    assert not result.is_error, result.output
    assert reviewer.calls == 4
    changed = await GeoNativeMapTool().execute(NativeMapInput(action='render',
        scene_ref=first.metadata['scene_ref'], edits=[
            {'target': 'page', 'id': 'overview', 'changes': {'title': '总览标题更新'}},
            {'target': 'page', 'id': 'detail', 'changes': {'title': '详图标题更新'}}]), context)
    assert not changed.is_error, changed.output
    result = await tool.execute(GeoMapReviewInput(file_path=changed.metadata['preview_path'],
        task='最终复核', checks=['再确认改好的两页，名册保持不变'], all_pages=True), context)
    assert not result.is_error, result.output
    body = json.loads(result.output)
    assert reviewer.calls == 6
    assert len(body['reviewed_pages']) == 2 and len(body['reused_pages']) == 2
    for wording in ['再检查一遍', '确认最终修改', '准备交付']:
        result = await tool.execute(GeoMapReviewInput(file_path=changed.metadata['preview_path'],
            task=wording, checks=[wording], all_pages=True), context)
        assert not result.is_error, result.output
    assert reviewer.calls == 6


async def test_explicit_page_requirement_changes_only_its_page_and_survives_restart(tmp_path):
    context, first, reviewer = await rendered_four_pages(tmp_path)
    tool = GeoMapReviewTool()
    await tool.execute(GeoMapReviewInput(file_path=first.metadata['preview_path'],
        all_pages=True), context)
    result = await tool.execute(GeoMapReviewInput(file_path=first.metadata['preview_path'],
        all_pages=True, page_checks={'roster-1': ['检查距离列小数是否清晰']}), context)
    assert not result.is_error, result.output
    assert reviewer.calls == 5
    fresh = ToolExecutionContext(cwd=tmp_path, metadata={
        'geoharness_config': context.metadata['geoharness_config'], 'map_review_client': reviewer})
    again = await tool.execute(GeoMapReviewInput(file_path=first.metadata['preview_path'],
        all_pages=True), fresh)
    assert not again.is_error, again.output
    assert reviewer.calls == 5


async def test_unknown_page_requirement_rejected_before_request(tmp_path):
    context, first, reviewer = await rendered_four_pages(tmp_path)
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=first.metadata['preview_path'], all_pages=True,
        page_checks={'does-not-exist': ['检查']}), context)
    assert result.is_error and reviewer.calls == 0


async def test_issues_are_reused_and_explicit_requirement_detail_is_preserved(tmp_path):
    context, first, reviewer = await rendered_four_pages(tmp_path)
    reviewer.status = 'issues'
    tool = GeoMapReviewTool()
    await tool.execute(GeoMapReviewInput(file_path=first.metadata['preview_path'],
        all_pages=True, page_checks={'roster-1': ['检查距离列是否清晰']}), context)
    reused = await tool.execute(GeoMapReviewInput(file_path=first.metadata['preview_path'],
        all_pages=True), context)
    assert json.loads(reused.output)['status'] == 'issues'
    assert reviewer.calls == 4
    # Both requirements contain an alias such as readability, but are different duties.
    await tool.execute(GeoMapReviewInput(file_path=first.metadata['preview_path'],
        all_pages=True, page_checks={'roster-1': ['检查高程列是否清晰']}), context)
    assert reviewer.calls == 5
    config = context.metadata['geoharness_config']
    from dataclasses import replace

    context.metadata['geoharness_config'] = replace(config,
        map_review=config.map_review.model_copy(update={
            'max_tokens': config.map_review.max_tokens + 1}))
    await tool.execute(GeoMapReviewInput(file_path=first.metadata['preview_path'],
        all_pages=True), context)
    assert reviewer.calls == 9
