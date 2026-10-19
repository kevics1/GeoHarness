"""Stable cache identity for image review conclusions."""

import json

from geoharness.cartography.review_contract import add_scope, review_identity


def test_task_wording_does_not_invalidate_same_page_contract():
    metadata = {"backend": "qgis", "native_facts": {"frames": {
        "main_map": {"page_index": 0, "purpose": "总览", "label_sources": ["places"]}}},
        "layout": {"page": {"detail_placement": "page"}}}
    evidence = {"backend": "qgis", "render_metadata": metadata}
    first = add_scope(evidence, task="检查地图")
    second = add_scope(evidence, task="最终复核这张图")
    assert first["review_scope_identity"] == second["review_scope_identity"]
    assert first["review_scope_contract"]["task_scope"] == "检查地图"

def test_page_purpose_and_analysis_evidence_do_change_contract():
    metadata = {"backend": "qgis", "native_facts": {"frames": {
        "main_map": {"page_index": 0, "purpose": "总览"}}}, "analysis": {
            "status": "verified", "analyses": [{"action": "qgis_processing",
                                                  "manifest_digest": "a"}]}}
    overview = add_scope({"backend": "qgis", "render_metadata": metadata}, task="检查")
    changed = metadata.copy()
    changed["native_facts"] = {"frames": {"main_map": {"page_index": 0, "purpose": "详图"}}}
    detail = add_scope({"backend": "qgis", "render_metadata": changed}, task="检查")
    assert overview["review_scope_identity"] != detail["review_scope_identity"]
    assert overview["review_scope_contract"]["analysis_manifest_count"] == 1

def test_analysis_binding_changes_do_not_repeat_same_pixel_check():
    first = {"backend": "qgis", "render_metadata": {"native_facts": {"frames": {
        "main_map": {"page_index": 0, "purpose": "总览"}}}, "analysis": {"status": "verified",
        "analyses": [{"manifest_path": "old", "manifest_digest": "a"}]}}}
    repath = json.loads(json.dumps(first))
    repath["render_metadata"]["analysis"]["analyses"][0]["manifest_path"] = "new"
    changed = json.loads(json.dumps(repath))
    changed["render_metadata"]["analysis"]["analyses"][0]["manifest_digest"] = "b"
    assert review_identity(first) == review_identity(repath)
    assert review_identity(first) == review_identity(changed)

def test_other_page_placement_does_not_change_current_pixel_question():
    first = {"backend": "qgis", "render_metadata": {"native_facts": {"frames": {
        "main_map": {"page_index": 0, "purpose": "总览"}}},
        "layout": {"page": {"detail_placement": "page"}}}}
    changed = json.loads(json.dumps(first))
    changed["render_metadata"]["layout"]["page"]["detail_placement"] = "separate"
    assert review_identity(first) == review_identity(changed)
    changed['review_checks'] = ['新增：检查当前页的编号可读性']
    assert review_identity(first) != review_identity(changed)
