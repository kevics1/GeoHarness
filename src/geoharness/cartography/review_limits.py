"""Durable task-owned review reservations, independent of image/file/model names."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path

from geoharness.task_store import connect, task_directory

_LOCK = threading.RLock()
_KEY = 'visual_review_limits'
STOP_MESSAGE = ('已达到自动审图上限，停止自动改图和新增审查。保留现有图片、报告及未解决问题；'
                '存在严重缺陷或未完成检查时保持草稿，不宣称通过。向用户说明情况，等待人工处理；'
                '不要换文件名、页面ID、模型或新建地图绕过上限。')


@contextmanager
def _usage(metadata):
    task = metadata.get('geo_task_record') or {}
    cwd = task.get('workspace') or metadata.get('map_job_state', {}).get('cwd')
    with _LOCK:
        directory = task_directory(metadata, cwd=Path(cwd)) if cwd else None
        if directory is None:
            yield metadata.setdefault(_KEY, {'total': 0, 'pages': {}})
        else:
            with connect(directory) as db:
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT payload FROM state WHERE key=?', (_KEY,)).fetchone()
                usage = json.loads(row['payload']) if row else {'total': 0, 'pages': {}}
                yield usage
                db.execute('INSERT OR REPLACE INTO state VALUES(?,?)',
                           (_KEY, json.dumps(usage, ensure_ascii=False)))


def _status(usage, settings, page=None):
    if settings.workflow == 'single_pass':
        return {'automatic_review_stopped': False, 'task_review_limit_reached': False,
                'page_review_limit_reached': False, 'review_requests_used': usage['total'],
                'page_review_requests_used': usage['pages'].get(str(page), 0),
                'review_workflow': 'single_pass', 'next_action':
                    '全册只审查一轮，最多修图一轮后直接交付，不复审。'}
    total_reached = usage['total'] >= settings.max_review_requests_per_task
    page_reached = usage['pages'].get(str(page), 0) >= settings.max_review_requests_per_page
    outcomes = usage.get('outcomes', {})
    stopped = total_reached or (
        page_reached and outcomes.get(str(page)) != 'passed' if page is not None else
        any(v >= settings.max_review_requests_per_page and outcomes.get(k) != 'passed'
            for k, v in usage['pages'].items()))
    return {'automatic_review_stopped': stopped,
            'task_review_limit_reached': total_reached,
            'page_review_limit_reached': page_reached,
            'review_requests_used': usage['total'],
            'review_request_limit': settings.max_review_requests_per_task,
            'page_review_requests_used': usage['pages'].get(str(page), 0),
            'page_review_request_limit': settings.max_review_requests_per_page,
            'review_count_scope': 'task_and_page_position; includes_failed_requests; cache_free',
            **({'next_action': STOP_MESSAGE} if stopped else {})}


def review_limit_status(metadata, config, *, page=None):
    with _usage(metadata) as usage:
        return _status(usage, config.map_review, page)


def reserve_review_request(metadata, config, *, page):
    """Reserve before awaiting transport, so concurrent calls cannot exceed the cap."""
    with _usage(metadata) as usage:
        status = _status(usage, config.map_review, page)
        if config.map_review.workflow == 'single_pass':
            policy = usage.setdefault('single_pass', {})
            pages = policy.setdefault('requested_pages', [])
            if str(page) in pages:
                return False, {**status, 'review_budget_exhausted': True,
                    'single_pass_no_repeat': True,
                    'next_action': '本页已审查一次，不再复审，直接交付。'}
            pages.append(str(page))
            usage['total'] += 1
            usage['pages'][str(page)] = usage['pages'].get(str(page), 0) + 1
            return True, _status(usage, config.map_review, page)
        if status['task_review_limit_reached'] or status['page_review_limit_reached']:
            usage.setdefault('outcomes', {})[str(page)] = 'unverified'
            return False, {**status, 'automatic_review_stopped': True, 'next_action': STOP_MESSAGE}
        usage['total'] += 1
        key = str(page)
        usage['pages'][key] = usage['pages'].get(key, 0) + 1
        usage.setdefault('outcomes', {})[key] = 'inflight'
        return True, _status(usage, config.map_review, page)


def record_review_outcome(metadata, *, page, status):
    """A page passing its last attempt must not halt unrelated pages with quota left."""
    with _usage(metadata) as usage:
        usage.setdefault('outcomes', {})[str(page)] = status


def refund_review_request(metadata, *, page):
    """Only local preparation failures with no started provider request are refunded."""
    with _usage(metadata) as usage:
        key = str(page)
        usage['total'] = max(0, usage['total'] - 1)
        usage['pages'][key] = max(0, usage['pages'].get(key, 0) - 1)
        pages = usage.get('single_pass', {}).get('requested_pages', [])
        if key in pages:
            pages.remove(key)
