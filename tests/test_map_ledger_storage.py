"""The map history must remain readable after crossing the old monolithic limit."""
import hashlib
import json
import uuid

import pytest

from geoharness.cartography import jobs
from geoharness.config.settings import GeoConfig


def legacy_job(tmp_path):
    identity = str(uuid.uuid4())
    directory = tmp_path / GeoConfig().cartography.jobs_dir
    report = {"rows": ["x" * 1900 + str(i) for i in range(100)]}
    value = {"schema_version": 1, "job_id": identity, "current_version": 68,
             "versions": [{"version": i+1, "stages": {
                 "parameters": {"report": report}, "data_checked": {"report": report}}}
                 for i in range(68)]}
    jobs._atomic(directory / identity / "receipt.json", value)
    return directory, identity, value


def test_oversized_legacy_migrates_without_losing_or_repeating_reports(tmp_path):
    from geoharness.cartography.ledger_store import migrate, stats

    directory, identity, original = legacy_job(tmp_path)
    receipt = directory / identity / "receipt.json"
    raw = receipt.read_bytes()
    assert len(raw) > 16 * 1024 * 1024
    result = migrate(receipt)
    assert result["versions"] == 68
    assert hashlib.sha256(raw).hexdigest() == result["legacy_sha256"]
    assert (receipt.parent / result["backup"]).read_bytes() == raw
    assert receipt.stat().st_size < 1024
    assert jobs._job(directory, identity) == original
    assert stats(receipt)["report_records"] == 1
    assert migrate(receipt)["status"] == "already_current"


def test_current_head_query_does_not_load_historical_reports(tmp_path, monkeypatch):
    from geoharness.cartography import ledger_store

    directory, identity, original = legacy_job(tmp_path)
    ledger_store.migrate(directory / identity / "receipt.json")
    monkeypatch.setattr(ledger_store, "load", lambda *a, **kw: pytest.fail("history read"))
    state = jobs.map_mutation_state({}, cwd=tmp_path, config=GeoConfig(), map_id=identity)
    assert state["latest_version"] == original["current_version"]


def test_failed_migration_preserves_authoritative_legacy_receipt(tmp_path, monkeypatch):
    from geoharness.cartography import ledger_store

    directory, identity, _ = legacy_job(tmp_path)
    receipt = directory / identity / "receipt.json"
    raw = receipt.read_bytes()
    monkeypatch.setattr(ledger_store, "verify", lambda *a: (_ for _ in ()).throw(
        ValueError("injected verification failure")))
    with pytest.raises(ValueError, match="verification failure"):
        ledger_store.migrate(receipt)
    assert receipt.read_bytes() == raw


def test_report_integrity_is_checked_without_trusting_database_text(tmp_path):
    import sqlite3

    from geoharness.cartography import ledger_store

    directory, identity, _ = legacy_job(tmp_path)
    receipt = directory / identity / "receipt.json"
    ledger_store.migrate(receipt)
    with sqlite3.connect(receipt.parent / "ledger.sqlite3") as connection:
        connection.execute("UPDATE reports SET payload=?", (json.dumps({"changed": True}),))
    with pytest.raises(ValueError, match="integrity"):
        jobs._job(directory, identity)
