"""Task-owned finding identities and explicit dispositions, independent of wording."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from geoharness.cartography.review_progress import _same_concern
from geoharness.task_store import committed_result, connect, task_directory


def _load(db):
    row = db.execute("SELECT payload FROM state WHERE key='review_findings'").fetchone()
    return json.loads(row['payload']) if row else {}


def _save(db, findings):
    db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',
               ('review_findings', json.dumps(findings, ensure_ascii=False, allow_nan=False)))


def observe_findings(metadata, *, cwd, map_id, page_index, image_sha256, review,
                     pixel_sha256=''):
    directory = task_directory(metadata, cwd=Path(cwd))
    if directory is None:
        return {'state': 'unavailable', 'reason': 'no_active_task'}
    with connect(directory) as db:
        findings = _load(db)
        previous = [item for item in findings.values()
                    if item['map_id'] == map_id and item['page_index'] == page_index]
        current = []
        for issue in review.get('issues', []) if review.get('status') == 'issues' else []:
            if issue.get('severity') not in {'warning', 'error'}:
                continue
            issue = {key: value for key, value in issue.items()
                     if key not in {'issue_id', 'state', 'map_id', 'page_index'}}
            matched = next((old for old in previous if _same_concern(old['issue'], issue)), None)
            if matched is None:
                identity = 'issue:' + hashlib.sha256(json.dumps(
                    [map_id, page_index, image_sha256, issue], sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest()[:20]
                matched = {'issue_id': identity, 'map_id': map_id, 'page_index': page_index,
                           'first_image_sha256': image_sha256, 'state': 'open',
                           'seen_images': [], 'seen_pixels': [], 'dispositions': []}
                findings[identity] = matched
            if image_sha256 not in matched['seen_images']:
                matched['seen_images'].append(image_sha256)
                # A prior dismissal or fix for different pixels cannot waive a
                # newly observed concern on a changed image.
                changed = (pixel_sha256 not in matched.get('seen_pixels', [])
                           if pixel_sha256 else True)
                if changed and matched['state'] in {'dismissed', 'fix_verified', 'needs_recheck'}:
                    matched['state'] = 'open'
            # Never let model-authored identity/state fields overwrite the task-owned
            # finding id or lifecycle. Keep only the visible observations in `issue`.
            matched.update(issue=issue, last_image_sha256=image_sha256)
            if pixel_sha256 and pixel_sha256 not in matched.setdefault('seen_pixels', []):
                matched['seen_pixels'].append(pixel_sha256)
            current.append(matched)
        if review.get('status') in {'passed', 'issues'}:
            seen = {item['issue_id'] for item in current}
            for old in previous:
                if (old['issue_id'] not in seen
                        and old['state'] not in {'dismissed', 'fix_verified'}):
                    old['state'] = 'needs_recheck'
        _save(db, findings)
    open_items = [item for item in findings.values() if item['map_id'] == map_id
                  and item['page_index'] == page_index
                  and item['state'] not in {'dismissed', 'fix_verified'}]
    return {'current': [{'issue_id': i['issue_id'], 'state': i['state'], 'issue': i['issue']}
                        for i in current], 'open_count': len(open_items),
            'inspect_call': {'action': 'review_issues', 'map_id': map_id, 'page_index': page_index},
            'scope': 'finding_tracking_only; dispositions_do_not_override_map_quality'}


def review_findings(context, *, map_id='', page_index=0, issue_id='', disposition='inspect',
                    reason='', evidence_operation_id=''):
    directory = task_directory(context.metadata, cwd=Path(context.cwd))
    if directory is None:
        raise ValueError('当前没有持久任务')
    with connect(directory) as db:
        findings = _load(db)
    if disposition != 'inspect':
        if issue_id not in findings or not reason.strip() or not evidence_operation_id:
            raise ValueError('处置需要实际issue_id、具体reason和已成功执行的evidence_operation_id')
        operation, document = committed_result(context.metadata, cwd=context.cwd,
                                               operation_id=evidence_operation_id)
        if operation['status'] != 'succeeded' or document.get('is_error'):
            raise ValueError('处置证据必须来自成功且完整提交的操作')
        item = findings[issue_id]
        if disposition == 'fix_verified':
            from geoharness.cartography.workflow import _pixel_digest

            evidence = document.get('metadata', {})
            digest = evidence.get('image_sha256')
            pixels = evidence.get('pixel_sha256')
            checks = evidence.get('review_checks', [])
            image = Path(evidence.get('image_path') or '.')
            if (operation['tool'] != 'geo_map_review'
                    or evidence.get('review_status') != 'passed'
                    or evidence.get('map_id') != item['map_id']
                    or evidence.get('review_page_index') != item['page_index']
                    or not any(issue_id in check for check in checks)
                    or not digest or digest in item['seen_images']
                    or not pixels or not item.get('seen_pixels')
                    or pixels in item['seen_pixels']
                    or not image.is_file()
                    or hashlib.sha256(image.read_bytes()).hexdigest() != digest
                    or _pixel_digest(image) != pixels):
                raise ValueError('fix_verified需要同一地图/页面的新像素通过原生审查，'
                                 'checks明确包含issue_id，且图片哈希仍匹配；未再报告不等于已修复')
        item['state'] = disposition
        item['dispositions'].append({'state': disposition, 'reason': reason,
                                     'evidence_operation_id': evidence_operation_id})
        with connect(directory) as db:
            # Do not overwrite findings committed by another page while evidence
            # was checked outside this transaction.
            latest = _load(db)
            latest[issue_id] = item
            _save(db, latest)
            findings = latest
    items = [item for item in findings.values() if (not map_id or item['map_id'] == map_id)
             and item['page_index'] == page_index
             and (not issue_id or item['issue_id'] == issue_id)]
    return {'findings': items, 'count': len(items),
            'scope': 'explicit_agent_assessment; does_not_upgrade_visual_or_business_acceptance'}


def pending_findings(metadata, *, cwd, map_ids):
    directory = task_directory(metadata, cwd=Path(cwd))
    if directory is None:
        return []
    with connect(directory) as db:
        findings = _load(db)
    identities = {value for value in map_ids if value}
    aliases = metadata.get('map_job_state', {}).get('aliases', {})
    identities.update(alias for alias, target in aliases.items() if target in identities)
    pending = []
    for item in findings.values():
        if item['map_id'] not in identities or item['state'] in {'dismissed', 'fix_verified'}:
            continue
        # The visual model may return an empty issue_id. Spread first, then restore
        # system-owned identity/state/page/version fields so whitespace or an empty
        # string cannot make downstream findings untraceable or overwrite the ledger.
        pending.append({
            **item['issue'],
            'issue_id': item['issue_id'],
            'state': item['state'],
            'map_id': item['map_id'],
            'page_index': item['page_index'],
            'first_image_sha256': item.get('first_image_sha256'),
            'last_image_sha256': item.get('last_image_sha256'),
        })
    return pending
