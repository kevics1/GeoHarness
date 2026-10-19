"""Task-owned SQLite operations and immutable, paginated execution evidence.

The session keeps an identity, never a second copy of full tool output. A running
record is written before execution and the result before returning to OpenHarness.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID, uuid4

from openharness.utils.fs import atomic_write_text


def task_directory(metadata, *, cwd: Path) -> Path | None:
    task = metadata.get('geo_task_record')
    config = metadata.get('geoharness_config')
    if not task or config is None:
        return None
    root = Path(cwd).resolve()
    if Path(task['workspace']).resolve() != root:
        raise ValueError('Task belongs to another workspace')
    identity = str(UUID(task['task_id']))
    directory = Path(config.agent.task_state_dir).expanduser()
    directory = (directory if directory.is_absolute() else root / directory).resolve()
    if directory == root or not directory.is_relative_to(root):
        raise ValueError('agent.task_state_dir must be a subfolder of the workspace')
    return directory / identity


def _safe(value, metadata):
    from geoharness.cartography.quality import _review_secrets

    config = metadata.get('geoharness_config')
    secrets = _review_secrets(config) if config is not None else ()

    def redact(item):
        if isinstance(item, str):
            for secret in secrets:
                if secret:
                    item = item.replace(secret, '[REDACTED]')
            return item
        if isinstance(item, dict):
            return {redact(str(k)): redact(v) for k, v in item.items()
                    if str(k).lower() not in {'api_key', 'authorization', 'password', 'token'}}
        if isinstance(item, (list, tuple)):
            return [redact(v) for v in item]
        if item is None or type(item) in (int, float, bool):
            return item
        return str(item)

    return redact(value)


@contextmanager
def connect(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(directory / 'task.sqlite3', timeout=30) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=30000')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, tool TEXT NOT NULL, action TEXT,
                status TEXT NOT NULL, started REAL NOT NULL, finished REAL,
                evidence TEXT, digest TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY, definition TEXT NOT NULL, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, operation_id TEXT, result TEXT, artifacts TEXT,
                dependencies TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS pixel_reviews (
                key TEXT PRIMARY KEY, evidence TEXT NOT NULL, digest TEXT NOT NULL);
        ''')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(operations)')}
        for name in ('request', 'request_digest'):
            if name not in columns:
                db.execute(f'ALTER TABLE operations ADD COLUMN {name} TEXT')
        yield db


def save_record(metadata, *, cwd):
    from geoharness.data.profiles import current_profiles

    directory = task_directory(metadata, cwd=Path(cwd))
    if directory is None:
        return
    with connect(directory) as db:
        db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (
            'task', json.dumps(_safe(metadata['geo_task_record'], metadata), ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (
            'analysis_stages', json.dumps(_safe(metadata.get('analysis_workflow_state', {}),
                                               metadata), ensure_ascii=False)))
        db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (
            'input_profiles', json.dumps(_safe(current_profiles(metadata), metadata),
                                         ensure_ascii=False)))
        if 'geo_page_reuse' in metadata:
            db.execute('INSERT OR REPLACE INTO state VALUES (?, ?)', (
                'page_reuse', json.dumps(_safe(metadata['geo_page_reuse'], metadata),
                                         ensure_ascii=False)))


def read_pixel_review(metadata, key):
    """Restore an application-committed conclusion, never trust a session assertion."""
    task = metadata.get('geo_task_record') or {}
    if not task.get('workspace'):
        return None
    directory = task_directory(metadata, cwd=Path(task['workspace']))
    if directory is None or not (directory / 'task.sqlite3').is_file():
        return None
    with connect(directory) as db:
        row = db.execute('SELECT * FROM pixel_reviews WHERE key=?', (key,)).fetchone()
    if row is None:
        return None
    path = Path(row['evidence']).resolve()
    try:
        if (not path.is_relative_to(directory.resolve() / 'evidence')
                or path.stat().st_size > 1024*1024
                or hashlib.sha256(path.read_bytes()).hexdigest() != row['digest']):
            return None
        packet = json.loads(path.read_text(encoding='utf-8'))
        if packet['key'] != key:
            return None
        return packet['review']
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_pixel_review(metadata, key, review):
    task = metadata.get('geo_task_record') or {}
    if not task.get('workspace') or review.get('status') not in {'passed', 'issues'}:
        return
    directory = task_directory(metadata, cwd=Path(task['workspace']))
    if directory is None:
        return
    path = directory / 'evidence' / ('pixel-' + uuid4().hex + '.json')
    text = json.dumps(_safe({'key': key, 'review': review}, metadata),
                      ensure_ascii=False, allow_nan=False)
    if len(text.encode('utf-8')) > 1024*1024:
        return
    atomic_write_text(path, text)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with connect(directory) as db:
        db.execute('INSERT OR REPLACE INTO pixel_reviews VALUES(?,?,?)',
                   (key, str(path), digest))


def begin_operation(metadata, *, cwd, tool, arguments, node_id=None):
    directory = task_directory(metadata, cwd=Path(cwd))
    if directory is None:
        return None
    identity = uuid4().hex
    value = (arguments.model_dump(mode='json') if hasattr(arguments, 'model_dump')
             else dict(arguments))
    safe = _safe(value, metadata)
    request = directory / 'evidence' / (identity + '-request.json')
    atomic_write_text(request, json.dumps({'operation_id': identity, 'arguments': safe},
                                         ensure_ascii=False, allow_nan=False))
    request_digest = hashlib.sha256(request.read_bytes()).hexdigest()
    with connect(directory) as db:
        db.execute('INSERT INTO operations(id,tool,action,status,started,request,request_digest) '
                   'VALUES(?,?,?,?,?,?,?)',
                   (identity, tool, value.get('action'), 'running', time.time(),
                    str(request), request_digest))
        if node_id:
            changed = db.execute("UPDATE nodes SET operation_id=? WHERE id=? AND status='running'",
                                 (identity, node_id)).rowcount
            if changed != 1:
                raise ValueError('Task node lost its running state before execution')
    return {'id': identity, 'directory': directory, 'arguments': safe}


def finish_operation(handle, metadata, *, result=None, error=None):
    if handle is None:
        return None
    identity, directory = handle['id'], handle['directory']
    document = _safe({'operation_id': identity, 'arguments': handle['arguments'],
        'output': result.output if result is not None else None,
        'metadata': result.metadata if result is not None else {},
        'is_error': result.is_error if result is not None else True,
        'error_type': error}, metadata)
    text = json.dumps(document, ensure_ascii=False, allow_nan=False, default=str)
    path = directory / 'evidence' / (identity + '.json')
    atomic_write_text(path, text)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    status = ('interrupted' if error in {'CancelledError', 'GeneratorExit'} else
              'failed' if error or result is None or result.is_error else 'succeeded')
    with connect(directory) as db:
        db.execute('UPDATE operations SET status=?,finished=?,evidence=?,digest=?,error=? '
                   'WHERE id=?', (status, time.time(), str(path), digest, error, identity))
    return {'operation_id': identity, 'evidence_path': str(path), 'evidence_sha256': digest}


def read_evidence(metadata, *, cwd, operation_id='', section='output', offset=0, limit=4000):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 12000:
        raise ValueError('Evidence requires offset >= 0 and limit between 1 and 12000')
    directory = task_directory(metadata, cwd=Path(cwd))
    if directory is None:
        raise ValueError('No persistent task is active')
    if section not in {'output', 'arguments', 'metadata', 'record'}:
        raise ValueError('Evidence section must be output/arguments/metadata/record')
    if not operation_id:
        with connect(directory) as db:
            records = [dict(row) for row in db.execute(
                'SELECT id,tool,action,status,started,finished,error FROM operations '
                'ORDER BY started DESC LIMIT ? OFFSET ?', (min(limit, 100), offset))]
        return {'operations': records, 'offset': offset, 'count': len(records)}
    # The locator is looked up in SQLite, never accepted from model text.
    with connect(directory) as db:
        row = db.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown operation_id in this task')
    locator, digest = row['evidence'], row['digest']
    if not locator and section == 'arguments' and row['request']:
        locator, digest = row['request'], row['request_digest']
    if not locator and section != 'record':
        return {'operation_id': operation_id, 'status': row['status'],
                'note': '调用尚无持久化结果；恢复时不要直接重发外部请求。'}
    document = {}
    if locator:
        path = Path(locator).resolve()
        if not path.is_relative_to(directory.resolve() / 'evidence') or (
                hashlib.sha256(path.read_bytes()).hexdigest() != digest):
            raise ValueError('Operation evidence changed; it cannot be used for recovery')
        document = json.loads(path.read_text(encoding='utf-8'))
    value = dict(row) if section == 'record' else document[section]
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return {'operation_id': operation_id, 'status': row['status'], 'section': section,
            'offset': offset, 'total_chars': len(content), 'content': content[offset:offset+limit],
            'next_offset': offset+limit if offset+limit < len(content) else None}


@contextmanager
def execution_lock(directory: Path):
    """OS-owned task lease, released even when its owning process is killed."""
    import os

    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'execution.lock').open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError(
                '当前任务正在由另一执行器运行；可读取状态和证据，请勿重复提交') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
