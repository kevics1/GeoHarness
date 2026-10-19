"""Retain issued data locators without making recent context an authorization list.

Records only locate measured sources and signed producer files. Every consumer
still checks their original signatures, hashes and producer state before use.
"""

from __future__ import annotations

import json
from pathlib import Path


def retained_references(metadata: dict, *, cwd: Path, current: dict | None = None) -> dict:
    from geoharness.task_store import connect, task_directory

    directory = task_directory(metadata, cwd=cwd)
    if directory is None:
        registry = metadata.setdefault('_issued_data_refs', {})
        if current:
            registry.update(current)
        return dict(registry)
    # Use the existing task database rather than an unrelated global cache.
    with connect(directory) as db:
        db.execute('CREATE TABLE IF NOT EXISTS data_references '
                   '(ref TEXT PRIMARY KEY, record TEXT NOT NULL)')
        if current:
            db.executemany('INSERT OR REPLACE INTO data_references VALUES(?,?)',
                           [(ref, json.dumps(record, ensure_ascii=False, allow_nan=False))
                            for ref, record in current.items()])
        return {row['ref']: json.loads(row['record'])
                for row in db.execute('SELECT ref,record FROM data_references')}
