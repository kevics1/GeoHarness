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


def sync_review_pages(metadata, *, cwd, map_id, pages, version=0, receipt=None):
    """Bind page duties using registered version evidence, never the current page number alone."""
    directory = task_directory(metadata, cwd=Path(cwd))
    if directory is None:
        return
    active = {p['page_id']: p['page_index'] for p in pages if p.get('page_id')}
    known = {}
    for v in (receipt or {}).get('versions', []):
        for p in v.get('stages', {}).get('visual_checked', {}).get('page_reviews', []):
            sha = p.get('sha256') or p.get('image_sha256')
            if sha and p.get('page_id'):
                known.setdefault(sha, set()).add(p['page_id'])
    for p in pages:
        if p.get('sha256') and p.get('page_id'):
            known.setdefault(p['sha256'], set()).add(p['page_id'])
    with connect(directory) as db:
        row = db.execute("SELECT payload FROM state WHERE key='review_page_identity'").fetchone()
        registry = json.loads(row['payload']) if row else {}
        if registry.get(map_id, {}).get('version', 0) > (version or 0):
            return  # Inspecting a historical page cannot reactivate a retired duty.
        findings = _load(db)
        for item in findings.values():
            if item['map_id'] != map_id:
                continue
            if not item.get('page_id'):
                ids = known.get(item.get('last_image_sha256'), set())
                if len(ids) == 1:
                    item['page_id'] = next(iter(ids))
                    item['page_binding'] = 'verified_image_history'
                else:
                    item['page_binding'] = 'legacy_unresolved'
            page_id = item.get('page_id')
            if page_id:
                item['page_active'] = page_id in active
                if page_id in active:
                    item['page_index'] = active[page_id]
                else:
                    item['retirement'] = {'version': version, 'reason': 'page_removed_from_plan',
                        'scope': 'historical_finding_retained; new_page_quality_still_required'}
        registry[map_id] = {'version': version, 'pages': active}
        _save(db, findings)
        db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',
                   ('review_page_identity', json.dumps(registry, ensure_ascii=False)))


def _same_page(item, page_id, page_index):
    return (item.get('page_active') is not False and
        (item.get('page_id') == page_id if page_id else
         not item.get('page_id') and item['page_index'] == page_index))


def _located_issue(issue, review, contract):
    """Use frame-local regions for identity; retain the provider's original observation."""
    from geoharness.cartography.review_progress import _box

    value = dict(issue)
    bbox = _box(issue)
    regions = contract.get('regions') or {}
    paper = contract.get('paper') or {}
    width, height = paper.get('width_mm'), paper.get('height_mm')
    if not bbox or not width or not height:
        return value
    rois = review.get('diagnostics', {}).get('review_rois', [])
    roi = next((r for r in rois if r.get('roi_ref') == issue.get('roi_ref')
                and r.get('frame_id') == issue.get('frame_id')), None)
    if roi:
        left,top,right,bottom = roi['pixel_bbox']
        w,h = roi['original_size']
        bbox = [(left+bbox[0]*(right-left))/w, (top+bbox[1]*(bottom-top))/h,
                (left+bbox[2]*(right-left))/w, (top+bbox[3]*(bottom-top))/h]
    elif issue.get('roi_ref') not in {'', 'whole'}:
        return value
    cx,cy = (bbox[0]+bbox[2])*width/2, (bbox[1]+bbox[3])*height/2
    for name, rect in regions.items():
        x,y,w,h = rect
        if x <= cx <= x+w and y <= cy <= y+h:
            local = [(bbox[0]*width-x)/w, (bbox[1]*height-y)/h,
                     (bbox[2]*width-x)/w, (bbox[3]*height-y)/h]
            if all(0 <= v <= 1 for v in local):
                value.update(frame_id=name, roi_ref=name, bbox=local)
                value['coordinate_scope'] = 'frame_local'
                value['original_observation'] = dict(issue)
            break
    return value


def observe_findings(metadata, *, cwd, map_id, page_index, image_sha256, review,
                     pixel_sha256='', page_id='', page_contract=None):
    directory = task_directory(metadata, cwd=Path(cwd))
    if directory is None:
        return {'state': 'unavailable', 'reason': 'no_active_task'}
    with connect(directory) as db:
        row = db.execute("SELECT payload FROM state WHERE key='review_page_identity'").fetchone()
        registry = json.loads(row['payload']).get(map_id, {}) if row else {}
        observed_version = ((review.get('diagnostics') or {}).get('review_binding') or {}).get(
            'version')
        if registry and page_id and (page_id not in registry['pages'] or
                observed_version is not None and observed_version < registry.get('version', 0)):
            return {'state': 'historical_observation', 'open_count': 0,
                    'reason': 'retained_report_does_not_change_current_page_findings'}
        findings = _load(db)
        previous = [item for item in findings.values()
                    if item['map_id'] == map_id and _same_page(item, page_id, page_index)]
        current = []
        for issue in review.get('issues', []) if review.get('status') == 'issues' else []:
            if issue.get('severity') != 'error':
                continue
            issue = {key: value for key, value in issue.items()
                     if key not in {'issue_id', 'state', 'map_id', 'page_index'}}
            issue = _located_issue(issue, review, page_contract or {})
            matched = next((old for old in previous if _same_concern(old['issue'], issue)), None)
            if matched is None:
                identity = 'issue:' + hashlib.sha256(json.dumps(
                    [map_id, page_id or page_index, image_sha256, issue], sort_keys=True,
                    ensure_ascii=False).encode()).hexdigest()[:20]
                matched = {'issue_id': identity, 'map_id': map_id, 'page_index': page_index,
                           'page_id': page_id, 'page_active': True,
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
            matched.update(issue=issue, last_image_sha256=image_sha256,
                           page_contract=page_contract or {}, page_index=page_index)
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
                  and _same_page(item, page_id, page_index)
                  and item['issue'].get('severity') == 'error'
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
        if (item['map_id'] not in identities or item['state'] in {'dismissed', 'fix_verified'}
                or item.get('page_active') is False or item['issue'].get('severity') != 'error'):
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


def pending_page_targets(metadata, *, cwd, map_id, page_index, page_id='', page_contract=None):
    directory = task_directory(metadata, cwd=Path(cwd))
    if directory is None:
        return []
    with connect(directory) as db:
        findings = _load(db)
    targets = [dict(item) for item in findings.values()
               if item['map_id'] == map_id and _same_page(item, page_id, page_index)
               and item['issue'].get('severity') == 'error'
               and item['state'] not in {'dismissed', 'fix_verified'}]
    for item in targets:
        old, current = item.get('page_contract') or {}, page_contract or {}
        extent_changed = any(frame.get('extent') != (current.get('frames') or {}).get(
            name, {}).get('extent') for name, frame in (old.get('frames') or {}).items())
        if old.get('regions') != current.get('regions') or extent_changed:
            issue = dict(item['issue'])
            if issue.get('coordinate_scope') != 'frame_local' or extent_changed:
                issue['bbox'] = None
                issue['relocation_notice'] = ('版面区域变化，原整页坐标失效；'
                                             '按描述独立重新定位，不能猜测已修复')
            item['issue'] = issue
        item['issue'] = {k:v for k,v in item['issue'].items() if k != 'original_observation'}
    return targets


def apply_targeted_assessments(metadata, *, cwd, map_id, page_index, image_path,
                               image_sha256, pixel_sha256, review, targets, report_path):
    """Close only explicit, new-pixel findings after the report is committed."""
    from geoharness.cartography.workflow import _pixel_digest

    directory = task_directory(metadata, cwd=Path(cwd))
    diagnostics = review.get('diagnostics') or {}
    binding = diagnostics.get('review_binding') or {}
    if (directory is None or not diagnostics.get('finding_assessments')
            or review.get('status') not in {'passed', 'issues'}
            or diagnostics.get('failure_stage') or not pixel_sha256
            or binding.get('map_id') != map_id or binding.get('page_index') != page_index
            or binding.get('image_sha256') != image_sha256):
        return {'resolved': [], 'reason': 'incomplete_review'}
    path, report_path = Path(image_path), Path(report_path)
    if (hashlib.sha256(path.read_bytes()).hexdigest() != image_sha256
            or _pixel_digest(path) != pixel_sha256):
        raise ValueError('定向复核图片在登记后已变化')
    saved = json.loads(report_path.read_text('utf-8'))
    if (saved.get('image_sha256') != image_sha256
            or saved.get('pixel_sha256') != pixel_sha256
            or saved.get('diagnostics', {}).get('finding_assessments') !=
            diagnostics.get('finding_assessments')):
        raise ValueError('定向复核必须来自已登记的原始报告')
    by_id = {item['issue_id']: item for item in targets}
    assessments = diagnostics.get('finding_assessments', [])
    ids = [item.get('issue_id') for item in assessments]
    if len(ids) != len(set(ids)) or set(ids) - set(by_id):
        return {'resolved': [], 'reason': 'ambiguous_or_unknown_target'}
    resolved = []
    dismissed = []
    with connect(directory) as db:
        findings = _load(db)
        for assessment in assessments:
            target = by_id[assessment['issue_id']]
            current = findings.get(target['issue_id'])
            if (not current or current['map_id'] != map_id
                    or not _same_page(current, binding.get('page_id', ''), page_index)
                    or current['state'] in {'dismissed', 'fix_verified'}
                    or assessment.get('state') not in {'resolved', 'not_a_defect'}
                    or not str(assessment.get('reason') or '').strip()
                    or any(_same_concern(target['issue'], _located_issue(issue, review,
                               current.get('page_contract') or {}))
                           for issue in review.get('issues', [])
                           if issue.get('severity') == 'error')):
                continue
            nondefect = assessment['state'] == 'not_a_defect'
            if nondefect and assessment.get('basis') not in {
                    'outside_page_duty','no_reading_impact','data_semantics','preference'}:
                continue
            if not nondefect and (image_sha256 in target.get('seen_images', [])
                    or not target.get('seen_pixels') or pixel_sha256 in target['seen_pixels']):
                continue
            current['state'] = 'dismissed' if nondefect else 'fix_verified'
            current['dispositions'].append({'state': current['state'],
                'reason': assessment['reason'], 'report_path': str(report_path),
                'basis':assessment.get('basis',''),
                'report_sha256': hashlib.sha256(report_path.read_bytes()).hexdigest(),
                'image_sha256': image_sha256, 'pixel_sha256': pixel_sha256,
                'evidence_kind': ('committed_targeted_nondefect_assessment' if nondefect
                                  else 'committed_targeted_pixel_review')})
            (dismissed if nondefect else resolved).append(target['issue_id'])
        _save(db, findings)
    return {'resolved': resolved, 'dismissed':dismissed, 'assessed_count': len(assessments)}
