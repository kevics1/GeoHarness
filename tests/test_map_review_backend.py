"""Native review suggestions use image-bound backend identity and executable units."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.tools.base import ToolExecutionContext
from PIL import Image

from geoharness.cartography.quality import review_map
from geoharness.cartography.review_context import build_review_iteration_context
from geoharness.config.settings import GeoConfig
from geoharness.tools.geo_map_review import (
    GeoMapReviewInput,
    GeoMapReviewTool,
    _verified_render_evidence,
)


class Reviewer:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def stream_message(self, request):
        self.requests.append(request)
        yield ApiMessageCompleteEvent(
            message=ConversationMessage.from_user_text(json.dumps(self.response)),
            usage=UsageSnapshot(),
        )


ISSUES = {
    "status": "issues", "summary": "Numbered site labels cover each other.",
    "issues": [{"severity": "warning", "description": "Site labels overlap."}],
}
QGIS_PATCH = {
    "layout": {"width_mm": 420, "height_mm": 297, "body_font_size_pt": 11},
    "layers": [{"layer_index": 0, "style": {"size_mm": 3}, "label_style": {
        "placement": "over_point", "offset_x_mm": 2, "offset_y_mm": 1, "priority": 8,
    }}],
}


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "preview.png"
    Image.new("RGB", (16, 16), "white").save(path)
    return path


def _manifest(path):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "backend": "qgis", "preview": path.name, "preview_sha256": digest,
        "layers": [{"name": "Sites", "kind": "vector", "geometry_type": "Point",
                    "labels_enabled": True}],
    }


async def test_qgis_review_schema_has_native_units_and_early_detail_strategy(image):
    reviewer = Reviewer({**ISSUES, "qgis_patch": QGIS_PATCH, "next_strategy": {
        "strategy": "detail_map", "reason": "The dense sites need a larger local scale.",
    }})
    result = await review_map(image, config=GeoConfig(), task="Inspect evacuation map",
                              evidence={"backend": "qgis", "render_metadata": {
                                  "layers": [{"name": "Sites", "geometry_type": "Point"}],
                              }}, client=reviewer)
    schema = reviewer.requests[0].tools[0]["input_schema"]
    assert "layout_patch" not in schema["properties"]
    assert "LayoutPatch" not in schema["$defs"]
    assert "qgis_patch" in schema["properties"]
    assert "point_size" not in json.dumps(schema)
    assert result.qgis_patch.layout.width_mm == 420
    assert result.qgis_patch.layers[0].label_style.offset_x_mm == 2
    assert result.next_strategy.strategy == "detail_map"
    assert result.diagnostics["qgis_patch_applied"] is False
    assert '"layer_index": 0' in reviewer.requests[0].messages[0].text


async def test_qgis_label_schema_explains_distance_and_offset_placement_semantics(image):
    reviewer = Reviewer(ISSUES)
    await review_map(image, config=GeoConfig(), task="Inspect", evidence={"backend": "qgis"},
                     client=reviewer)
    schema = reviewer.requests[0].tools[0]["input_schema"]
    fields = schema["$defs"]["QgisLabelPatch"]["properties"]
    assert "distance_mm" in fields["placement"]["description"]
    assert "over_point" in fields["offset_x_mm"]["description"]
    assert "Ignored by around_point" in fields["offset_x_mm"]["description"]
    assert "Ignored by around_point" in fields["offset_y_mm"]["description"]
    prompt = reviewer.requests[0].messages[0].text
    assert "环绕模式忽略offset_x_mm/offset_y_mm" in prompt
    assert "over_point不靠distance_mm移动标签" in prompt


async def test_old_around_point_offset_suggestion_keeps_valid_visual_findings(image):
    reviewer = Reviewer({**ISSUES, "qgis_patch": {"layers": [{
        "layer_index": 0, "label_style": {"placement": "around_point", "offset_y_mm": 1.5,
                                         "priority": 10},
    }]}, "next_strategy": {"strategy": "detail_map", "reason": "Dense sites need more space."}})
    result = await review_map(image, config=GeoConfig(), task="Inspect",
                              evidence={"backend": "qgis", "render_metadata": {
                                  "layers": [{"name": "Sites", "geometry_type": "Point"}],
                              }}, client=reviewer)
    assert result.status == "issues"
    assert result.issues[0].description == ISSUES["issues"][0]["description"]
    assert result.qgis_patch.layers[0].label_style.placement == "around_point"
    assert result.qgis_patch.layers[0].label_style.priority == 10
    assert result.qgis_patch.layers[0].label_style.offset_y_mm is None
    assert "ignores" in result.diagnostics["ignored_qgis_layer_fields"][0]["reason"]
    assert result.next_strategy.strategy == "detail_map"


@pytest.mark.parametrize("placement,recorded_mode,expected", [
    ("around_point", None, {"placement": "around_point", "distance_mm": 2, "priority": 8}),
    ("over_point", None, {"placement": "over_point", "offset_x_mm": 3, "priority": 8}),
    ("auto", "OverPoint", {"placement": "auto", "distance_mm": 2, "priority": 8}),
    (None, "OrderedPositionsAroundPoint", {"distance_mm": 2, "priority": 8}),
    (None, "OverPoint", {"offset_x_mm": 3, "priority": 8}),
    (None, None, {"distance_mm": 2, "priority": 8}),
])
async def test_point_patch_filters_unused_fields_for_verified_effective_mode(
    image, placement, recorded_mode, expected,
):
    reviewer = Reviewer({**ISSUES, "qgis_patch": {"layers": [{
        "layer_index": 0, "label_style": {"placement": placement, "distance_mm": 2,
                                         "offset_x_mm": 3, "priority": 8},
    }]}})
    result = await review_map(image, config=GeoConfig(), task="Inspect",
                              evidence={"backend": "qgis", "render_metadata": {"layers": [{
                                  "kind": "vector", "geometry_type": "Point",
                                  "label_style": {"placement": recorded_mode},
                              }]}}, client=reviewer)
    assert result.status == "issues" and result.issues
    assert result.qgis_patch.layers[0].label_style.model_dump(exclude_none=True) == expected
    assert result.diagnostics["ignored_qgis_layer_fields"]


@pytest.mark.parametrize("geometry,placement", [
    ("Line", "around_point"), ("LineString", "around_point"),
    ("MultiLineString", "around_point"), ("Polygon", "around_point"),
    ("MultiPolygon", "over_point"), ("Point", "parallel"),
])
async def test_inapplicable_native_label_mode_keeps_other_fixes_and_findings(
    image, geometry, placement,
):
    reviewer = Reviewer({**ISSUES, "qgis_patch": {"layers": [{
        "layer_index": 0, "style": {"width_mm": 0.6},
        "label_style": {"placement": placement, "size_pt": 11, "priority": 8,
                        "offset_x_mm": 2},
    }]}})
    result = await review_map(image, config=GeoConfig(), task="Inspect",
                              evidence={"backend": "qgis", "render_metadata": {"layers": [{
                                  "kind": "vector", "geometry_type": geometry,
                              }]}}, client=reviewer)
    assert result.status == "issues"
    patch = result.qgis_patch.layers[0]
    assert patch.style.width_mm == 0.6
    assert patch.label_style.placement is None
    assert patch.label_style.offset_x_mm is None
    assert patch.label_style.size_pt == 11 and patch.label_style.priority == 8
    assert "requires" in result.diagnostics["ignored_qgis_layer_fields"][0]["reason"]


async def test_unknown_old_geometry_keeps_font_priority_without_guessing_a_mode(image):
    reviewer = Reviewer({**ISSUES, "qgis_patch": {"layers": [{
        "layer_index": 0, "label_style": {"placement": "over_point", "offset_y_mm": 1.5,
                                         "size_pt": 11, "priority": 10},
    }]}})
    result = await review_map(image, config=GeoConfig(), task="Inspect",
                              evidence={"backend": "qgis", "render_metadata": {"layers": [{
                                  "kind": "vector", "name": "Old manifest layer",
                              }]}}, client=reviewer)
    assert result.status == "issues"
    assert result.qgis_patch.layers[0].label_style.model_dump(exclude_none=True) == {
        "size_pt": 11, "priority": 10,
    }
    assert "unverified" in result.diagnostics["ignored_qgis_layer_fields"][0]["reason"]


async def test_raster_vector_suggestions_are_filtered_without_losing_valid_vector_patch(image):
    reviewer = Reviewer({**ISSUES, "qgis_patch": {"layers": [{
        "layer_index": 0, "style": {"size_mm": 3}, "label_style": {"priority": 8},
    }, {"layer_index": 1, "label_style": {"placement": "parallel", "size_pt": 11}}]}})
    result = await review_map(image, config=GeoConfig(), task="Inspect",
                              evidence={"backend": "qgis", "render_metadata": {"layers": [
                                  {"kind": "raster"},
                                  {"kind": "vector", "geometry_types": ["MultiLineString"]},
                              ]}}, client=reviewer)
    assert result.status == "issues"
    assert len(result.qgis_patch.layers) == 1
    assert result.qgis_patch.layers[0].layer_index == 1
    assert result.qgis_patch.layers[0].label_style.placement == "parallel"
    assert "raster" in result.diagnostics["ignored_qgis_layer_patches"][0]["reason"]


async def test_old_qgis_review_inch_patch_is_ignored_without_losing_findings(image):
    reviewer = Reviewer({**ISSUES, "layout_patch": {"width": 22, "height": 16,
                                                   "point_size": 60}})
    result = await review_map(image, config=GeoConfig(), task="Inspect",
                              evidence={"backend": "qgis"}, client=reviewer)
    assert result.status == "issues"
    assert result.layout_patch.model_dump(exclude_none=True) == {}
    assert result.diagnostics["ignored_layout_patch"]["point_size"] == 60


async def test_qgis_patch_cannot_target_unverified_layer_index(image):
    reviewer = Reviewer({**ISSUES, "qgis_patch": QGIS_PATCH})
    result = await review_map(image, config=GeoConfig(), task="Inspect",
                              evidence={"backend": "qgis"}, client=reviewer)
    assert result.status == "issues"
    assert result.qgis_patch.layers == []
    assert result.diagnostics["ignored_qgis_layer_patches"][0]["layer_index"] == 0


async def test_qgis_style_preference_cannot_trigger_repairs_after_pass(image):
    reviewer = Reviewer({"status": "passed", "summary": "Readable", "issues": [],
                         "qgis_patch": QGIS_PATCH, "next_strategy": {
                             "strategy": "split_maps", "reason": "Prefer two maps.",
                         }})
    result = await review_map(image, config=GeoConfig(), task="Inspect",
                              evidence={"backend": "qgis"}, client=reviewer)
    assert result.status == "passed"
    assert result.qgis_patch is None and result.next_strategy is None


@pytest.mark.parametrize("style", [{"field": "new_threshold"}, {"file_path": "new.geojson"}])
async def test_qgis_review_patch_cannot_change_analysis_or_source(image, style):
    reviewer = Reviewer({**ISSUES, "qgis_patch": {
        "layers": [{"layer_index": 0, "style": style}],
    }})
    result = await review_map(image, config=GeoConfig(), task="Inspect",
                              evidence={"backend": "qgis"}, client=reviewer)
    assert result.status == "unverified"
    assert result.diagnostics["failure_stage"] == "schema"


@pytest.mark.parametrize("patch", [
    {"layers": [{"layer_index": 0, "label_style": {"priority": 8.5}}]},
    {"layers": [{"layer_index": 0, "style": {"opacity": 0}}]},
])
async def test_native_suggestions_reject_fractional_priority_and_hidden_layers(image, patch):
    result = await review_map(image, config=GeoConfig(), task="Inspect",
                              evidence={"backend": "qgis"},
                              client=Reviewer({**ISSUES, "qgis_patch": patch}))
    assert result.status == "unverified"
    assert result.diagnostics["failure_stage"] == "schema"


async def test_strategy_reason_is_redacted_before_persistence(image):
    secret = "unit-test-only-review-key"
    result = await review_map(image, config=GeoConfig(api_key=secret), task="Inspect",
                              evidence={"backend": "qgis"}, client=Reviewer({
                                  **ISSUES, "next_strategy": {
                                      "strategy": "detail_map", "reason": f"Dense area {secret}",
                                  },
                              }))
    assert secret not in result.model_dump_json()
    assert "[redacted]" in result.next_strategy.reason


async def test_matplotlib_review_keeps_its_existing_patch_contract(image):
    reviewer = Reviewer({**ISSUES, "layout_patch": {"width": 12, "point_size": 30}})
    result = await review_map(image, config=GeoConfig(), task="Inspect", evidence={},
                              client=reviewer)
    schema = reviewer.requests[0].tools[0]["input_schema"]
    assert "layout_patch" in schema["properties"]
    assert "qgis_patch" not in schema["properties"]
    assert "QgisLayoutPatch" not in schema["$defs"]
    assert result.layout_patch.width == 12


async def test_public_review_uses_bound_manifest_over_caller_backend_claim(image):
    from geoharness.cartography.jobs import ensure_map_job, record_map_version

    manifest_path = image.parent / "project.json"
    manifest = {**_manifest(image), "project_file": "map.qgz",
                "files": ["project.json", image.name, "map.qgz"]}
    (image.parent / "map.qgz").write_bytes(b"native fixture")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    reviewer = Reviewer({**ISSUES, "qgis_patch": QGIS_PATCH})
    context = ToolExecutionContext(cwd=image.parent, metadata={
        "geoharness_config": GeoConfig(), "map_review_client": reviewer,
    })
    identity = ensure_map_job(context.metadata, cwd=image.parent, config=GeoConfig(),
                              artifact_path=image)
    record_map_version(identity, context.metadata, cwd=image.parent, config=GeoConfig(),
                       image_path=image, preflight=None, layout=None, project_dir=image.parent)
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(image), task="Inspect", evidence={
            "backend": "matplotlib", "render_metadata": {"layers": [{"name": "Fake"}]},
        },
    ), context)
    assert not result.is_error, result.output
    assert result.metadata["backend"] == "qgis"
    prompt = reviewer.requests[0].messages[0].text
    assert '"name": "Sites"' in prompt and '"name": "Fake"' not in prompt
    report = json.loads(Path(result.metadata["review_path"]).read_text(encoding="utf-8"))
    assert report["qgis_patch"]["layers"][0]["layer_index"] == 0
    assert '"remaining_render_versions"' in prompt


async def test_unproven_caller_backend_cannot_enable_native_patch_schema(image):
    reviewer = Reviewer({**ISSUES, "qgis_patch": QGIS_PATCH})
    context = ToolExecutionContext(cwd=image.parent, metadata={
        "geoharness_config": GeoConfig(), "map_review_client": reviewer,
    })
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(image), task="Inspect", evidence={"backend": "qgis"},
    ), context)
    assert result.metadata["backend"] == "unknown"
    schema = reviewer.requests[0].tools[0]["input_schema"]
    assert "layout_patch" not in schema["properties"]
    assert "qgis_patch" not in schema["properties"]
    report = json.loads(Path(result.metadata["review_path"]).read_text(encoding="utf-8"))
    assert report["qgis_patch"] is None


def test_changed_manifest_or_preview_cannot_prove_backend(image):
    manifest_path = image.parent / "project.json"
    manifest_path.write_text(json.dumps(_manifest(image)), encoding="utf-8")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    receipt = {"versions": [{"image_sha256": digest, "stages": {"packaged": {
        "manifest_path": str(manifest_path), "files": [{
            "path": str(manifest_path), "sha256": "0" * 64,
        }],
    }}}]}
    result = _verified_render_evidence(image, digest, receipt, [image.parent])
    assert result["backend"] == "unknown"
    manifest = _manifest(image)
    manifest["preview"] = "missing.png"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _verified_render_evidence(image, digest, {}, [image.parent])["backend"] == "unknown"


def test_backend_proof_rejects_oversized_manifest_before_reading_it(image, monkeypatch):
    manifest_path = image.parent / "project.json"
    manifest_path.write_bytes(b"x" * (1024 * 1024 + 1))
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    original = Path.read_bytes

    def read_bytes(path):
        assert path != manifest_path, "Oversized manifest must be rejected before reading bytes"
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert _verified_render_evidence(image, digest, {}, [image.parent])["backend"] == "unknown"


@pytest.mark.parametrize("receipt", [
    {"versions": None}, {"versions": [None, "invalid"]},
    {"versions": [{"image_sha256": "digest", "stages": None}]},
    {"versions": [{"image_sha256": "digest", "stages": {"packaged": None}}]},
])
def test_backend_proof_tolerates_malformed_receipt_stages(image, receipt):
    result = _verified_render_evidence(image, "digest", receipt, [image.parent])
    assert result["backend"] == "unknown"


def test_manifest_and_preview_paths_must_stay_in_allowed_roots(image, tmp_path):
    manifest_path = image.parent / "project.json"
    manifest_path.write_text(json.dumps(_manifest(image)), encoding="utf-8")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    result = _verified_render_evidence(image, digest, {}, [tmp_path / "other"])
    assert result["backend"] == "unknown"
    manifest = _manifest(image)
    manifest["preview"] = "../preview.png"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _verified_render_evidence(image, digest, {}, [image.parent])["backend"] == "unknown"


def test_iteration_context_keeps_two_earlier_reviews_and_real_remaining_versions():
    config = GeoConfig()
    versions = [{"version": index, "image_sha256": str(index), "stages": {
        "visual_checked": {"report": {**ISSUES, "diagnostics": {"raw_review_excerpt": "raw"},
                                       "issues": [{"severity": "warning", "description": "Crowded",
                                                   **({"issue_code": "label_overlap"}
                                                      if index == 3 else {})}]}},
    }} for index in range(1, 5)]
    metadata = {"map_workflow_state": {"maps": {"map": {"renders": 2}}}}
    context = build_review_iteration_context(
        metadata, "map", config, receipt={"versions": versions}, image_sha256="4",
    )
    assert context["remaining_render_versions"] == 1 + config.map_review.max_repairs - 2
    assert [item["version"] for item in context["previous_visual_reviews"]] == [2, 3]
    assert context["previous_visual_reviews"][0]["issues"][0]["issue_code"] == "other"
    assert context["previous_visual_reviews"][1]["issues"][0]["issue_code"] == "label_overlap"
    assert "raw_review_excerpt" not in json.dumps(context)


def test_iteration_context_reads_only_existing_same_job_receipt(tmp_path):
    config = GeoConfig()
    identity = "31139f2c-13ae-4dff-9f3a-ecf2d99746f6"
    directory = tmp_path / config.cartography.jobs_dir / identity
    directory.mkdir(parents=True)
    path = directory / "receipt.json"
    path.write_text(json.dumps({"job_id": identity, "versions": [{
        "version": 1, "image_sha256": "old", "stages": {"visual_checked": {"report": ISSUES}},
    }]}), encoding="utf-8")
    original = path.read_bytes()
    metadata = {"map_job_state": {"cwd": str(tmp_path)},
                "map_workflow_state": {"maps": {identity: {"renders": 2}}}}
    context = build_review_iteration_context(metadata, identity, config, image_sha256="current")
    assert len(context["previous_visual_reviews"]) == 1
    assert path.read_bytes() == original
    context = build_review_iteration_context(metadata, "missing-job", config)
    assert context["previous_visual_reviews"] == []
    assert not (tmp_path / config.cartography.jobs_dir / "missing-job").exists()
