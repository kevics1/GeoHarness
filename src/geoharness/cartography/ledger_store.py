"""Transactional version records and deduplicated reports behind bounded JSON locators."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from contextlib import closing
from functools import wraps
from pathlib import Path

RECORD_LIMIT = 16 * 1024 * 1024
MIGRATION_LIMIT = 128 * 1024 * 1024
DATABASE = "ledger.sqlite3"


def _errors(function):
    @wraps(function)
    def checked(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except sqlite3.Error as exc:
            raise ValueError(f"Map ledger storage failed: {exc}") from exc
    return checked


def _encode(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    if len(payload.encode()) > RECORD_LIMIT:
        raise ValueError("Map ledger individual record exceeds the size limit")
    return payload


def _digest(payload):
    return hashlib.sha256(payload.encode()).hexdigest()


def _regular(path):
    if path.is_symlink() or path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ValueError("Map ledger requires private regular files")


def _connect(path, *, create=False, writable=False):
    _regular(path)
    if not create and not path.is_file():
        raise ValueError("Map ledger database is missing")
    return sqlite3.connect(path if create else path.as_uri() + (
        "?mode=rw" if writable else "?mode=ro"), uri=not create)


def is_current(receipt):
    _regular(receipt)
    if not receipt.exists() or receipt.stat().st_size > RECORD_LIMIT:
        return False
    value = json.loads(receipt.read_text(encoding="utf-8"))
    return isinstance(value, dict) and value == {
        "schema_version": 2, "job_id": receipt.parent.name, "storage": DATABASE}


def _checked(payload, digest):
    if len(payload.encode()) > RECORD_LIMIT or _digest(payload) != digest:
        raise ValueError("Map ledger record integrity check failed")
    return json.loads(payload)


def _freeze(value, connection):
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "report" and isinstance(item, dict):
                payload = _encode(item)
                digest = _digest(payload)
                connection.execute("INSERT OR IGNORE INTO reports VALUES (?,?)", (digest, payload))
                result[key] = {"$ledger_report": digest}
            else:
                result[key] = _freeze(item, connection)
        return result
    return [_freeze(item, connection) for item in value] if isinstance(value, list) else value


def _thaw(value, connection, cache):
    if isinstance(value, dict):
        if set(value) == {"$ledger_report"}:
            digest = value["$ledger_report"]
            if digest not in cache:
                row = connection.execute("SELECT payload FROM reports WHERE digest=?",
                                         (digest,)).fetchone()
                if row is None:
                    raise ValueError("Map ledger report is missing")
                cache[digest] = _checked(row[0], digest)
            return copy.deepcopy(cache[digest])
        return {key: _thaw(item, connection, cache) for key, item in value.items()}
    return [_thaw(item, connection, cache) for item in value] if isinstance(value, list) else value


def _publish(path, job_id):
    _regular(path)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(_encode({"schema_version": 2, "job_id": job_id, "storage": DATABASE}))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@_errors
def write(receipt, job):
    if (not isinstance(job, dict) or job.get('job_id') != receipt.parent.name
            or not isinstance(job.get('versions'), list)):
        raise ValueError('Map ledger task/version identities are invalid')
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(receipt.parent / DATABASE, create=True)) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY, payload TEXT, digest TEXT);
            CREATE TABLE IF NOT EXISTS versions
                (number INTEGER PRIMARY KEY, payload TEXT, digest TEXT);
            CREATE TABLE IF NOT EXISTS reports (digest TEXT PRIMARY KEY, payload TEXT);
        """)
        with connection:
            for expected, version in enumerate(job["versions"], 1):
                if (not isinstance(version, dict) or type(version.get("version")) is not int
                        or version["version"] != expected):
                    raise ValueError("Map ledger version identities are invalid")
                payload = _encode(_freeze(version, connection))
                digest = _digest(payload)
                connection.execute("INSERT INTO versions VALUES (?,?,?) ON CONFLICT(number) "
                    "DO UPDATE SET payload=excluded.payload,digest=excluded.digest "
                    "WHERE versions.digest != excluded.digest", (expected, payload, digest))
            count = connection.execute("SELECT COUNT(*) FROM versions").fetchone()[0]
            if count != len(job["versions"]):
                raise ValueError("Map ledger history cannot be truncated")
            metadata = {key: item for key, item in job.items() if key != "versions"}
            metadata["version_count"] = count
            payload = _encode(metadata)
            connection.execute("INSERT OR REPLACE INTO metadata VALUES (1,?,?)",
                               (payload, _digest(payload)))
    if not is_current(receipt):
        _publish(receipt, job["job_id"])


def _head(connection):
    row = connection.execute("SELECT payload,digest FROM metadata WHERE id=1").fetchone()
    if row is None:
        raise ValueError("Map ledger task head is missing")
    value = _checked(*row)
    if (not isinstance(value, dict) or type(value.get('version_count')) is not int
            or value['version_count'] < 0):
        raise ValueError('Map ledger task head is invalid')
    return value


@_errors
def head(receipt):
    with closing(_connect(receipt.parent / DATABASE)) as connection:
        result = _head(connection)
        if result.get("job_id") != receipt.parent.name:
            raise ValueError("Map ledger task identity does not match")
        return result


@_errors
def load(receipt):
    with closing(_connect(receipt.parent / DATABASE)) as connection:
        connection.execute("BEGIN")
        result = _head(connection)
        if result.get('job_id') != receipt.parent.name:
            raise ValueError('Map ledger task identity does not match')
        expected = result.pop("version_count")
        cache = {}
        result["versions"] = [_thaw(_checked(payload, digest), connection, cache)
            for _, payload, digest in connection.execute(
                "SELECT number,payload,digest FROM versions ORDER BY number")]
        if len(result["versions"]) != expected:
            raise ValueError("Map ledger version count integrity check failed")
        return result


@_errors
def update_head(receipt, updates):
    with closing(_connect(receipt.parent / DATABASE, writable=True)) as connection, connection:
        value = _head(connection)
        value.update(updates)
        payload = _encode(value)
        connection.execute("UPDATE metadata SET payload=?,digest=? WHERE id=1",
                           (payload, _digest(payload)))


def verify(receipt, job):
    with closing(_connect(receipt.parent / DATABASE)) as connection:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("Map ledger database integrity check failed")
    if load(receipt) != job:
        raise ValueError("Map ledger migration verification failed")


@_errors
def migrate(receipt):
    if is_current(receipt):
        return {"status": "already_current", "versions": head(receipt)["version_count"]}
    _regular(receipt)
    if receipt.stat().st_size > MIGRATION_LIMIT:
        raise ValueError("Legacy map ledger exceeds the bounded migration limit")
    original = receipt.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    job = json.loads(original)
    if (not isinstance(job, dict) or job.get("job_id") != receipt.parent.name
            or not isinstance(job.get("versions"), list)):
        raise ValueError("Legacy map ledger identities are invalid")
    backup = receipt.with_name("receipt.v1." + digest + ".json")
    _regular(backup)
    if not backup.exists():
        with backup.open("xb") as output:
            output.write(original)
            output.flush()
            os.fsync(output.fileno())
    if backup.read_bytes() != original or receipt.read_bytes() != original:
        raise ValueError("Legacy map ledger changed during migration")
    with tempfile.TemporaryDirectory(prefix="ledger-migration-", dir=receipt.parent) as stage:
        staged = Path(stage) / job['job_id'] / "receipt.json"
        write(staged, job)
        verify(staged, job)
        if receipt.read_bytes() != original:
            raise ValueError("Legacy map ledger changed during migration")
        os.replace(staged.parent / DATABASE, receipt.parent / DATABASE)
        _publish(receipt, job["job_id"])
    return {"status": "migrated", "versions": len(job["versions"]),
            "legacy_sha256": digest, "backup": backup.name}


@_errors
def stats(receipt):
    with closing(_connect(receipt.parent / DATABASE)) as connection:
        return {"version_records": connection.execute(
                    "SELECT COUNT(*) FROM versions").fetchone()[0],
                "report_records": connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0]}


@_errors
def history(receipt, offset=0, limit=8):
    """Read a bounded version index without loading historical report bodies."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("History requires offset>=0 and 1<=limit<=100")
    with closing(_connect(receipt.parent / DATABASE)) as connection:
        rows = connection.execute("SELECT payload,digest FROM versions ORDER BY number "
                                  "LIMIT ? OFFSET ?", (limit, offset))
        return [{"version": value["version"], "image_path": value.get("image_path"),
                 "stages": {key: item.get("status") for key, item in
                            value.get("stages", {}).items()}}
                for payload, digest in rows for value in [_checked(payload, digest)]]
