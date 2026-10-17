"""SQLite review history must reach the actual reviewer without full ledger loads."""

import json

import pytest

from geoharness.cartography import jobs, ledger_store, workflow
from geoharness.cartography.review_context import build_review_iteration_context
from geoharness.config.settings import GeoConfig


def history(tmp_path):
    cfg = GeoConfig()
    metadata = {}
    identity = jobs.ensure_map_job(metadata, cwd=tmp_path, config=cfg, map_id="history-test")
    receipt = tmp_path / cfg.cartography.jobs_dir / identity / "receipt.json"
    ledger_store.write(receipt, {"job_id": identity, "versions": [
        {"version": n, "image_sha256": str(n), "stages": {
            "visual_checked": {"report": {"status": "issues", "issues": [
                {"severity": "warning", "issue_code": "label_overlap",
                 "description": f"Attempt {n}"}]}}}}
        for n in range(1, 5)]})
    return cfg, identity, receipt, metadata


def test_sqlite_context_reads_bounded_history_without_full_load(tmp_path, monkeypatch):
    cfg, identity, receipt, metadata = history(tmp_path)
    original = receipt.read_bytes()
    monkeypatch.setattr(ledger_store, "load", lambda *a: pytest.fail("unbounded load"))
    value = build_review_iteration_context(metadata, identity, cfg, image_sha256="4")
    assert [v["version"] for v in value["previous_visual_reviews"]] == [2, 3]
    assert receipt.read_bytes() == original
    assert "storage" not in json.dumps(value)


def test_history_is_scoped_to_the_reviewed_page(tmp_path):
    cfg, identity, receipt, metadata = history(tmp_path)
    job = ledger_store.load(receipt)
    for version in job["versions"]:
        n = version["version"]
        version["page_reviews"] = {"1": {"image_sha256": f"detail-{n}", "report": {
            "status": "issues", "issues": [{"severity": "warning",
                "issue_code": "label_missing", "description": "Detail labels"}]}}}
    ledger_store.write(receipt, job)
    value = build_review_iteration_context(metadata, identity, cfg, page_index=1,
                                           image_sha256="detail-4")
    assert [v["version"] for v in value["previous_visual_reviews"]] == [2, 3]
    assert all(v["page_index"] == 1 and v["issues"][0]["issue_code"] == "label_missing"
               for v in value["previous_visual_reviews"])


@pytest.mark.asyncio
async def test_workflow_does_not_overwrite_sqlite_history_with_empty_context(tmp_path, monkeypatch):
    cfg, identity, _, metadata = history(tmp_path)
    image = tmp_path / "preview.png"
    image.write_bytes(b"synthetic image digest fixture")
    captured = {}

    async def once(path, **kwargs):
        captured.update(kwargs["evidence"])
        return None, {}

    monkeypatch.setattr(workflow, "_review_once", once)
    await workflow.review_with_budget(image, config=cfg, task="test", evidence={},
                                     metadata=metadata, map_id=identity)
    assert [v["version"] for v in captured["previous_visual_reviews"]] == [3, 4]
