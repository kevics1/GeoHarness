import hashlib
import json

import pytest
from openharness.tools.base import ToolResult
from test_managed_inputs import setup_context

from geoharness.cartography.review_findings import observe_findings, review_findings
from geoharness.task_store import begin_operation, finish_operation


def record(context, tmp_path, digest, issues, status='issues'):
    return observe_findings(context.metadata, cwd=tmp_path, map_id='map', page_index=0,
        image_sha256=digest, pixel_sha256='pixels-' + digest,
        review={'status': status, 'issues': issues})


def test_finding_identity_persists_and_absence_is_not_a_verified_fix(tmp_path):
    context, _ = setup_context(tmp_path)
    issue = {'severity': 'warning', 'issue_code': 'label_overlap',
             'location': '主图北侧', 'description': '标注挤在一起'}
    first = record(context, tmp_path, 'a', [issue])
    identity = first['current'][0]['issue_id']
    changed = record(context, tmp_path, 'b', [{**issue, 'description': '编号仍然紧密'}])
    assert changed['current'][0]['issue_id'] == identity
    record(context, tmp_path, 'c', [], status='passed')
    fresh = type(context)(cwd=tmp_path, metadata=context.metadata.copy())
    saved = review_findings(fresh, issue_id=identity)['findings'][0]
    assert saved['state'] == 'needs_recheck'
    assert saved['seen_images'] == ['a', 'b']


def test_issue_disposition_requires_committed_evidence_and_new_targeted_pixels(tmp_path):
    context, _ = setup_context(tmp_path)
    issue = {'severity': 'error', 'issue_code': 'label_missing', 'location': '主图',
             'description': '居民点不在范围内'}
    first = record(context, tmp_path, 'a', [issue])
    identity = first['current'][0]['issue_id']
    with pytest.raises(ValueError, match='成功执行'):
        review_findings(context, issue_id=identity, disposition='dismissed', reason='看起来正常')
    image = tmp_path / 'review.png'
    from PIL import Image

    from geoharness.cartography.workflow import _pixel_digest

    Image.new('RGB', (10, 10), 'red').save(image)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()

    def proof(checks):
        handle = begin_operation(context.metadata, cwd=tmp_path, tool='geo_map_review',
                                 arguments={'file_path': str(image), 'checks': checks})
        return finish_operation(handle, context.metadata, result=ToolResult(
            output=json.dumps({'status': 'passed'}), metadata={'map_id': 'map',
                'review_page_index': 0, 'image_path': str(image), 'image_sha256': digest,
                'pixel_sha256': _pixel_digest(image),
                'review_checks': checks, 'review_status': 'passed'}))['operation_id']

    unscoped = proof([])
    with pytest.raises(ValueError, match='新像素'):
        review_findings(context, issue_id=identity, disposition='fix_verified', reason='修改了范围',
                        evidence_operation_id=unscoped)
    targeted = proof([f'核验 {identity}：居民点是否全部落在主图范围内'])
    verified = review_findings(context, issue_id=identity, disposition='fix_verified',
                               reason='修正范围后同页新图片定向审查通过',
                               evidence_operation_id=targeted)
    assert verified['findings'][0]['state'] == 'fix_verified'
    # A later actual recurrence reopens it rather than inheriting an old waiver.
    again = record(context, tmp_path, 'd', [issue])
    assert again['current'][0]['state'] == 'open'


def test_png_metadata_change_cannot_prove_a_pixel_fix(tmp_path):
    from PIL import Image, PngImagePlugin

    from geoharness.cartography.workflow import _pixel_digest

    context, _ = setup_context(tmp_path)
    image = tmp_path / 'review.png'
    Image.new('RGB', (10, 10), 'red').save(image)
    pixels = _pixel_digest(image)
    original = hashlib.sha256(image.read_bytes()).hexdigest()
    observed = observe_findings(context.metadata, cwd=tmp_path, map_id='map', page_index=0,
        image_sha256=original, pixel_sha256=pixels, review={'status': 'issues', 'issues': [{
            'severity': 'error', 'issue_code': 'label_overlap', 'description': '文字重叠'}]})
    identity = observed['current'][0]['issue_id']
    info = PngImagePlugin.PngInfo()
    info.add_text('version', 'new-container-only')
    Image.new('RGB', (10, 10), 'red').save(image, pnginfo=info)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    assert digest != original and _pixel_digest(image) == pixels
    checks = [f'核验{identity}是否修好']
    handle = begin_operation(context.metadata, cwd=tmp_path, tool='geo_map_review', arguments={})
    operation = finish_operation(handle, context.metadata, result=ToolResult(output='passed',
        metadata={'map_id': 'map', 'review_page_index': 0, 'image_path': str(image),
                  'image_sha256': digest, 'pixel_sha256': pixels,
                  'review_checks': checks, 'review_status': 'passed'}))['operation_id']
    with pytest.raises(ValueError, match='新像素'):
        review_findings(context, issue_id=identity, disposition='fix_verified',
                        reason='只更新了容器信息', evidence_operation_id=operation)
