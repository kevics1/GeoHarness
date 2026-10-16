"""Repeated pixels/text cannot masquerade as changed, verified map subjects."""

import json

from openharness.engine.stream_events import ToolExecutionCompleted
from test_quality_repair import overlapping_review, package

from geoharness.cartography.quality import ReviewStrategy
from geoharness.cartography.quality_repair import build_repair_plan, review_semantic_state
from geoharness.cartography.workflow import claim_repair_attempt
from geoharness.config.settings import GeoConfig
from geoharness.engine import GeoQueryEngine


def test_same_subject_and_plan_deduplicate_across_new_pixels_and_issue_wording(tmp_path):
    image, receipt, metadata, _ = package(tmp_path)
    review = overlapping_review()
    first = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    second_image, second_receipt, _, _ = package(tmp_path, metadata,
        name="second", color="gray", map_id=receipt["job_id"])
    review.issues[0].issue_id = "new-id"
    review.issues[0].description = "different provider wording"
    review.qgis_patch.issue_ids = ["new-id"]
    second = build_repair_plan(second_image, review, second_receipt,
                              config=GeoConfig(), cwd=tmp_path)
    assert first.digest != second.digest
    assert first.semantic_digest == second.semantic_digest
    assert claim_repair_attempt(metadata, first.semantic_digest)
    assert not claim_repair_attempt(metadata, second.semantic_digest)
    changed = review.model_copy(deep=True)
    changed.qgis_patch.layers[0].style.outline_color = "black"
    third = build_repair_plan(second_image, changed, second_receipt,
                             config=GeoConfig(), cwd=tmp_path)
    assert third.semantic_digest != second.semantic_digest


def test_subject_identity_changes_and_strategy_escalation_remain_allowed(tmp_path):
    _, _, _, manifest = package(tmp_path)
    review = overlapping_review()
    original = review_semantic_state(review, manifest, map_id="map")
    moved = json.loads(json.dumps(manifest))
    moved["native_facts"]["frames"]["main_map"]["points"][0]["feature_id"] = 99
    assert review_semantic_state(review, moved, map_id="map")["signature"] != original["signature"]
    # No native point proof: remain conservative rather than collapsing all
    # defects in the same layer into one anonymous subject.
    moved["native_facts"]["frames"]["main_map"]["points"] = []
    assert not review_semantic_state(review, moved, map_id="map")["complete"]
    partial = json.loads(json.dumps(manifest))
    partial["native_facts"]["frames"]["main_map"]["points"].pop()
    assert not review_semantic_state(review, partial, map_id="map")["complete"]
    unbound = review.model_copy(deep=True)
    unbound.diagnostics["issue_binding_errors"] = [{"issue_id": "capacity-covered"}]
    assert not review_semantic_state(unbound, manifest, map_id="map")["complete"]
    unbound = review.model_copy(deep=True)
    unbound.issues[0].frame_id = "footer"
    assert not review_semantic_state(unbound, manifest, map_id="map")["complete"]
    partial["native_facts"]["frames"]["main_map"]["points"] = (
        manifest["native_facts"]["frames"]["main_map"]["points"])
    partial["native_facts"]["frames"]["main_map"]["sample_limit"] = 2
    assert not review_semantic_state(review, partial, map_id="map")["complete"]
    variant = review.model_copy(deep=True)
    variant.next_strategy = ReviewStrategy(strategy="detail_map", reason="needs detail",
                                          issue_ids=["capacity-covered"])
    # Actual build strategy, rather than a pre-escalation provider hint, decides identity.
    image, receipt, _, _ = package(tmp_path, name="strategy")
    basic = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    detail = build_repair_plan(image, type(review).model_validate(variant.model_dump()), receipt,
                              config=GeoConfig(), cwd=tmp_path)
    assert basic.strategy != detail.strategy
    assert basic.semantic_digest != detail.semantic_digest


def test_quality_fingerprint_ignores_run_ids_but_preserves_page_and_subject(tmp_path):
    _, _, _, manifest = package(tmp_path)
    state = review_semantic_state(overlapping_review(), manifest, map_id="map")
    def event(version, current):
        return ToolExecutionCompleted(tool_name="geo_map_review", output=json.dumps({
            "latest_version": version, "summary": f"Provider wording {version}",
            "status": "issues", "diagnostics": {"semantic_quality": current}})
            + f"\nReport: run-{version}.json")
    assert GeoQueryEngine._progress_fingerprint(event(1, state)) == (
        GeoQueryEngine._progress_fingerprint(event(2, state)))
    page = review_semantic_state(overlapping_review(), manifest, page_index=1, map_id="map")
    assert GeoQueryEngine._progress_fingerprint(event(1, page)) != (
        GeoQueryEngine._progress_fingerprint(event(1, state)))
    review = overlapping_review()
    review.diagnostics["review_binding"] = {"map_id": "another-map"}
    other = review_semantic_state(review, manifest)
    assert GeoQueryEngine._progress_fingerprint(event(1, other)) != (
        GeoQueryEngine._progress_fingerprint(event(1, state)))
