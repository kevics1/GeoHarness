"""One review round and one optional repair; delivery permission is not a visual pass."""

from __future__ import annotations

import copy

from geoharness.cartography.review_limits import _usage


def single_pass(config):
    return config.map_review.workflow == 'single_pass'


def state(metadata):
    with _usage(metadata) as usage:
        return copy.deepcopy(usage.get('single_pass', {}))


def claim_review(metadata, *, map_id, pages):
    with _usage(metadata) as usage:
        policy = usage.setdefault('single_pass', {})
        if policy.get('review_started'):
            return False
        policy.update(review_started=True, review_map_id=map_id, pages=pages[:256])
        return True


def finish_review(metadata, *, status):
    with _usage(metadata) as usage:
        usage.setdefault('single_pass', {}).update(review_complete=True, review_status=status)


def remember_render(metadata, result):
    with _usage(metadata) as usage:
        policy = usage.setdefault('single_pass', {})
        if policy.get('review_started'):
            policy['repair_rendered'] = True
        policy['initial_rendered'] = True
        policy['render'] = {key: result[key] for key in (
            'scene_ref', 'project_dir', 'preview_path', 'output_path', 'map_id') if key in result}


def render_reuse(metadata):
    policy = state(metadata)
    if policy.get('initial_rendered') and (
            not policy.get('review_started') or policy.get('repair_rendered')):
        return policy.get('render') or None
    return None


def delivery_authorization(metadata, config, receipt):
    """Only waive layout/visual gates; input/hash/project integrity still applies."""
    if not single_pass(config):
        return False
    policy = state(metadata)
    return bool(policy.get('review_started') and policy.get('review_map_id') in {
        receipt.get('job_id'), receipt.get('map_id')})


def next_action(metadata):
    policy = state(metadata)
    return ('单轮审图流程：最多合并修改并render一次，然后直接geo_deliver交付图片/PDF/QGIS工程；'
            '不得再次审图，不再为标签局部重叠拆页、手算坐标或试偏移。修改后未复审须如实说明。'
            if policy.get('review_started') and not policy.get('repair_rendered') else
            '单轮审图和修图步骤已结束，直接geo_deliver交付图片/PDF/QGIS工程；'
            '保留严重问题和未复审说明，不再调用制图或审图。'
            if policy.get('repair_rendered') else
            '先geo_map_review(all_pages=true)全册审查一次；布局问题仅作参考，'
            '不要先用Python手算标注/分组或多轮调整偏移。')
