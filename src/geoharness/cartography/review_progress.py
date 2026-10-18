"""保守识别重复的可定位读图疑点；不据此认定地图质量或自动修图。"""

import json


def _concerns(review: dict) -> set[str]:
    result = set()
    for issue in review.get('issues', []):
        if not isinstance(issue, dict) or issue.get('severity') not in {'warning', 'error'}:
            continue
        # Without a location and stable wording we cannot prove it is the same concern.
        if not issue.get('location') or not issue.get('description'):
            continue
        result.add(json.dumps({k: issue.get(k, '') for k in (
            'issue_code', 'severity', 'frame_id', 'roi_ref', 'location', 'description',
        )}, sort_keys=True, ensure_ascii=False))
    return result


def assess_repair_progress(history: list, current: dict) -> dict:
    """三张不同版本同页图片重复同组问题时建议询问；不把换措辞当确定的同一问题。"""
    previous = [r for r in history if isinstance(r, dict) and r.get('status') == 'issues'][-2:]
    now = _concerns(current) if current.get('status') == 'issues' else set()
    digests = [r.get('image_sha256') for r in [*previous, current]]
    distinct = all(digests) and len(set(digests)) == 3
    repeated = bool(now and distinct and len(previous) == 2
                    and all(_concerns(r) == now for r in previous))
    return {'state': 'needs_user_decision' if repeated else 'not_established',
            'requires_user_decision': repeated,
            'repeated_concern_count': len(now) if repeated else 0,
            'scope': 'same_located_review_concerns; no_independent_pixel_verification',
            'next_action': ('ask_user_question' if repeated else 'verify_findings_before_repair'),
            'message': ('同组可定位问题在多轮中重复出现，不能确认继续改图是否有效。'
                        '请询问用户选择局部放大、保留现状或明确修复目标；保留草稿状态。'
                        if repeated else '先核实读图疑点，再合并有依据的修改；不凭空猜测。')}
