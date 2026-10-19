"""Stable per-page acceptance rules and verified reuse of committed observations."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def page_key(evidence):
    metadata = evidence.get('render_metadata') or {}
    index = metadata.get('review_page_index', 0)
    pages = metadata.get('page_previews') or (metadata.get('native_facts') or {}).get(
        'page_previews', [])
    page = next((p for p in pages if p.get('page_index') == index), {})
    return page.get('page_id') or str(index)


def page_checks(evidence, custom):
    from geoharness.cartography.pixel_review import page_scope

    role = page_scope(evidence)['page_role']
    shared = ['检查当前页文字、内容裁切和可读性；正常留白、表达偏好不作缺陷。']
    if role == 'roster':
        shared += ['本页只验表题、表头、单位、行列对齐与可读性，不要求地图要素。',
            '分页行数及全册完整性由系统核验，不从一页判断缺失其他页。']
    elif role == 'detail':
        shared += ['重点对象及编号应可辨认；检查标注归属、符号遮挡、图例对应和裁切。']
    else:
        shared += ['总览负责整体分布与分类辨识；密集背景对象不要求逐点辨认或计数。',
            '有真实重点符号或分类被遮盖仍须报告，不以尺度限制豁免可证缺陷。']
    return shared + list(page_scope(evidence).get('required_content', [])) + list(custom)


def update_requirements(context, map_id, evidence, updates, *, allow_change=True):
    """Only explicit page-keyed updates change acceptance; prose notes never do."""
    from geoharness.cartography.jobs import page_review_requirements

    metadata = evidence.get('render_metadata') or {}
    pages = metadata.get('page_previews') or (metadata.get('native_facts') or {}).get(
        'page_previews', [])
    valid = {p.get('page_id') or str(p['page_index']) for p in pages}
    if not valid:
        valid = {page_key(evidence)}
    if set(updates) - valid:
        raise ValueError('page_checks必须使用真实page_id；可用：' + ', '.join(sorted(valid)))
    current = page_review_requirements(context.metadata, cwd=context.cwd,
        config=context.metadata['geoharness_config'], map_id=map_id, updates={})
    if not allow_change and any(key in current and current[key] != value
                                for key, value in updates.items()):
        raise ValueError('已有验收要求发生变化；修复进度写repair_notes。'
                         '仅真实新增/修改业务要求时显式update_page_requirements=true。')
    return page_review_requirements(context.metadata, cwd=context.cwd,
        config=context.metadata['geoharness_config'], map_id=map_id, updates=updates)


def reusable_review(path, digest, receipt, evidence, config, checks):
    """Reuse only hash-verified, same-scope reports, including completed issues."""
    from geoharness.cartography.workflow import review_fingerprint

    binding = evidence.get('review_binding') or {}
    version = next((v for v in receipt.get('versions', [])
                    if v.get('version') == binding.get('version')), {})
    stage = version.get('page_reviews', {}).get(str(binding.get('page_index', 0)), {})
    if stage.get('status') not in {'passed', 'issues'} or stage.get('image_sha256') != digest:
        return None
    reference = stage.get('report_reference') or {}
    report = Path(reference.get('path') or '.')
    try:
        # A report may live in the original preview package after materialization.
        roots = [Path(v.get('stages', {}).get('packaged', {}).get('project_dir') or path.parent)
                 for v in receipt.get('versions', [])]
        roots += [Path(v.get('preview_package', {}).get('project_dir') or path.parent)
                  for v in receipt.get('versions', [])]
        if not any(report.resolve().is_relative_to(root.resolve()) for root in roots):
            return None
        raw = report.read_bytes()
        if hashlib.sha256(raw).hexdigest() != reference.get('sha256'):
            return None
        saved = json.loads(raw)
        diagnostics = saved.get('diagnostics') or {}
        expected, _ = review_fingerprint(config, {**evidence, 'review_checks': checks},
                                          task='', pixel_only=True)
        if (saved.get('image_sha256') != digest
                or diagnostics.get('review_fingerprint') != expected
                or diagnostics.get('failure_stage') or saved.get('status') == 'unverified'):
            return None
        return saved, report
    except (OSError, ValueError, TypeError):
        return None
