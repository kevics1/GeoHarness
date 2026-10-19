import json

from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from test_native_scene_workflow import setup

from geoharness.cartography.review_findings import pending_findings, review_findings
from geoharness.task_state import begin_task
from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool
from geoharness.tools.geo_native_map import GeoNativeMapTool, NativeMapInput


class Reviewer:
    def __init__(self):
        self.calls = 0
        self.target = ''
        self.assessment = 'unassessable'
        self.retain_issue = True

    async def stream_message(self, request):
        self.calls += 1
        payload = {'status': 'issues' if self.retain_issue else 'passed', 'summary': '模拟像素复核',
            'issues': [{'severity': 'warning', 'description': '北侧编号重叠',
                        'issue_code': 'label_overlap', 'location': '北侧'}]
                      if self.retain_issue else []}
        if self.target:
            assert self.target in str(request.messages)
            payload['finding_assessments'] = [{'issue_id': self.target,
                'state': self.assessment, 'reason': '当前图片北侧编号已分离且均可读'}]
        yield ApiMessageCompleteEvent(message=ConversationMessage.from_user_text(
            json.dumps(payload)), usage=UsageSnapshot())


async def test_new_pixels_explicit_assessment_closes_finding_only_after_committed_report(tmp_path):
    context, scene = setup(tmp_path)
    begin_task(context.metadata, cwd=tmp_path, goal='验证问题闭环')
    native, review = GeoNativeMapTool(), GeoMapReviewTool()
    first = await native.execute(NativeMapInput(action='render', scene=scene), context)
    assert not first.is_error, first.output
    reviewer = Reviewer()
    context.metadata['map_review_client'] = reviewer
    initial = await review.execute(GeoMapReviewInput(
        file_path=first.metadata['preview_path']), context)
    assert not initial.is_error, initial.output
    identity = initial.metadata['map_id']
    reviewer.target = pending_findings(context.metadata, cwd=tmp_path, map_ids=[identity])[0][
        'issue_id']
    # Repeating the same issue pixels never sends a new request to gamble on a pass.
    await review.execute(GeoMapReviewInput(file_path=first.metadata['preview_path']), context)
    assert reviewer.calls == 1
    for iteration, (assessment, retained, expected) in enumerate([
            ('resolved', True, False), ('unassessable', False, False),
            ('resolved', False, True)]):
        changed = await native.execute(NativeMapInput(action='render',
            scene_ref=first.metadata['scene_ref'], edits=[{'target': 'page', 'id': 'overview',
                'changes': {'title': f'修改后的总览{iteration}'}}]), context)
        assert not changed.is_error, changed.output
        reviewer.assessment, reviewer.retain_issue = assessment, retained
        checked = await review.execute(GeoMapReviewInput(
            file_path=changed.metadata['preview_path']), context)
        assert not checked.is_error, checked.output
        items = review_findings(context, map_id=identity)['findings']
        assert (items[0]['state'] == 'fix_verified') == expected
        if expected:
            assert checked.metadata['targeted_recheck']['resolved'] == [reviewer.target]
    assert reviewer.calls == 4
