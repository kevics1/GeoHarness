"""Use a stable per-page conclusion across harmless review retries."""


import pytest

from geoharness.cartography import workflow
from geoharness.cartography.quality import MapReview
from geoharness.config.settings import GeoConfig
from tests.test_pixel_review import image as image_fixture  # noqa: F401


@pytest.fixture
def image(tmp_path):
    from PIL import Image

    path = tmp_path / "page.png"
    Image.new("RGB", (64, 64), "white").save(path)
    return path

async def test_same_pixels_and_page_contract_reuse_conclusion_despite_task_wording(
    image, monkeypatch,
):
    calls = []

    async def reviewer(*args, **kwargs):
        calls.append(kwargs.get("task"))
        return MapReview(status="passed", summary="No visible defects.",
                         diagnostics={"request_sent": True, "stage": "response"})

    monkeypatch.setattr(workflow, "review_map", reviewer)
    metadata = {}
    evidence = {"backend": "qgis", "native_facts": {"frames": {
        "main_map": {"page_index": 0, "purpose": "总览"}}},
        "layout": {"page": {"detail_placement": "page"}}}
    first, first_flags = await workflow.review_with_budget(
        image, config=GeoConfig(), task="检查地图", evidence=evidence,
        metadata=metadata, pixel_only=True)
    second, second_flags = await workflow.review_with_budget(
        image, config=GeoConfig(), task="最终复核", evidence=evidence,
        metadata=metadata, pixel_only=True)
    assert first.status == second.status == "passed"
    assert first_flags["request_sent"] and second_flags["review_cached"]
    assert len(calls) == 1


async def test_committed_pixel_issues_survive_restart_without_trusting_session_cache(
    image, monkeypatch,
):
    from test_managed_inputs import setup_context

    context, _ = setup_context(image.parent)
    calls = []

    async def reviewer(*args, **kwargs):
        calls.append(1)
        return MapReview(status='issues', summary='编号相互遮挡',
            diagnostics={'request_sent': True, 'stage': 'response'})

    monkeypatch.setattr(workflow, 'review_map', reviewer)
    evidence = {'backend': 'qgis'}
    first, _ = await workflow.review_with_budget(image,
        config=context.metadata['geoharness_config'], task='检查', evidence=evidence,
        metadata=context.metadata, pixel_only=True)
    fresh = {key: context.metadata[key] for key in ('geo_task_record', 'geoharness_config')}
    second, flags = await workflow.review_with_budget(image,
        config=fresh['geoharness_config'], task='最终复核', evidence=evidence,
        metadata=fresh, pixel_only=True)
    assert first.status == second.status == 'issues'
    assert flags['review_cached'] and not flags['request_sent'] and len(calls) == 1
    assert second.diagnostics['persistent_pixel_evidence_reused']

    # A modified evidence document cannot assert success after restart.
    from geoharness.task_store import connect, task_directory

    directory = task_directory(fresh, cwd=image.parent)
    with connect(directory) as db:
        path = db.execute('SELECT evidence FROM pixel_reviews').fetchone()['evidence']
    from pathlib import Path

    Path(path).write_text('{"review":{"status":"passed"}}', encoding='utf-8')
    fresh.pop('map_workflow_state')
    _, flags = await workflow.review_with_budget(image,
        config=fresh['geoharness_config'], task='检查', evidence=evidence,
        metadata=fresh, pixel_only=True)
    assert flags['request_sent'] and len(calls) == 2

async def test_changed_page_purpose_requires_a_new_conclusion(image, monkeypatch):
    calls = []

    async def reviewer(*args, **kwargs):
        calls.append(kwargs.get("task"))
        return MapReview(status="passed", summary="No visible defects.",
                         diagnostics={"request_sent": True, "stage": "response"})

    monkeypatch.setattr(workflow, "review_map", reviewer)
    metadata = {}
    first_evidence = {"backend": "qgis", "native_facts": {"frames": {
        "main_map": {"page_index": 0, "purpose": "总览"}}}}
    second_evidence = {"backend": "qgis", "native_facts": {"frames": {
        "main_map": {"page_index": 0, "purpose": "详图"}}}}
    await workflow.review_with_budget(image, config=GeoConfig(), task="检查",
        evidence=first_evidence, metadata=metadata, pixel_only=True)
    await workflow.review_with_budget(image, config=GeoConfig(), task="检查",
        evidence=second_evidence, metadata=metadata, pixel_only=True)
    assert len(calls) == 2

async def test_issue_conclusion_is_reused_not_retried_into_a_pass(image, monkeypatch):
    calls = []
    issue = {"severity": "warning", "description": "Labels overlap.", "issue_code": "label_overlap"}

    async def reviewer(*args, **kwargs):
        calls.append(True)
        return MapReview(status="issues", summary="Visible label overlap.", issues=[issue],
                         diagnostics={"request_sent": True, "stage": "response"})

    monkeypatch.setattr(workflow, "review_map", reviewer)
    metadata = {}
    evidence = {"backend": "qgis", "native_facts": {"frames": {
        "main_map": {"page_index": 0, "purpose": "总览"}}}}
    for text in ("检查", "再看一次", "最终复核"):
        result, _ = await workflow.review_with_budget(image, config=GeoConfig(), task=text,
            evidence=evidence, metadata=metadata, pixel_only=True)
        assert result.status == "issues"
    assert len(calls) == 1
