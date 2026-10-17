"""A map version passes only after all its recorded map-page images pass."""

import hashlib
import json

import pytest
from PIL import Image

from geoharness.cartography.jobs import (
    ensure_map_job,
    map_job_receipt,
    record_map_review,
    record_map_version,
)
from geoharness.config.settings import GeoConfig


async def test_batch_reviews_verified_pages_once_without_hidden_builds(tmp_path):
    from openharness.api.client import ApiMessageCompleteEvent
    from openharness.api.usage import UsageSnapshot
    from openharness.engine.messages import ConversationMessage
    from openharness.tools.base import ToolExecutionContext

    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    config, metadata, job, image, _, _ = setup_pages(tmp_path)

    class Reviewer:
        calls = 0

        async def stream_message(self, request):
            self.calls += 1
            yield ApiMessageCompleteEvent(message=ConversationMessage.from_user_text(
                json.dumps({"status": "passed", "summary": "Synthetic pixels checked",
                            "issues": []})), usage=UsageSnapshot())

    reviewer = Reviewer()
    metadata.update(geoharness_config=config, map_review_client=reviewer)
    context = ToolExecutionContext(cwd=tmp_path, metadata=metadata)
    args = GeoMapReviewInput(file_path=str(image), task="Inspect both pages", all_pages=True)
    result = await GeoMapReviewTool().execute(args, context)
    assert not result.is_error, result.output
    body = json.loads(result.output)
    assert body["status"] == "passed"
    assert [p["page_index"] for p in body["pages"]] == [0, 1]
    assert not body["render_executed"] and reviewer.calls == 2
    assert map_job_receipt(metadata, cwd=tmp_path, config=config, map_id=job)[
        "latest_version"] == 1
    again = await GeoMapReviewTool().execute(args, context)
    assert not again.is_error, again.output
    assert reviewer.calls == 2
    assert not any(p["request_sent"] for p in json.loads(again.output)["pages"])


async def test_batch_cannot_pass_a_declared_page_without_returned_preview(tmp_path, monkeypatch):
    from openharness.api.client import ApiMessageCompleteEvent
    from openharness.api.usage import UsageSnapshot
    from openharness.engine.messages import ConversationMessage
    from openharness.tools.base import ToolExecutionContext

    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    config, metadata, job, image, _, _ = setup_pages(tmp_path)
    manifest = tmp_path / "project.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["native_facts"]["page_previews"] = value["native_facts"]["page_previews"][:1]
    value["layout"]["effective_parameters"] = {"detail_placement": "page"}
    manifest.write_text(json.dumps(value), encoding="utf-8")
    record_map_version(job, metadata, cwd=tmp_path, config=config, image_path=image,
                       project_dir=tmp_path, layout=value["layout"], preflight=None)

    class Reviewer:
        async def stream_message(self, request):
            yield ApiMessageCompleteEvent(message=ConversationMessage.from_user_text(
                json.dumps({"status": "passed", "summary": "Pixels checked", "issues": []})),
                usage=UsageSnapshot())

    metadata.update(geoharness_config=config, map_review_client=Reviewer())
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(file_path=str(image),
        task="Inspect", all_pages=True), ToolExecutionContext(cwd=tmp_path, metadata=metadata))
    assert not result.is_error, result.output
    body = json.loads(result.output)
    assert body["status"] == "unverified"
    assert body["unreviewed_page_indices"] == [1]


def test_batch_cannot_enable_second_repair_controller():
    from pydantic import ValidationError

    from geoharness.tools.geo_map_review import GeoMapReviewInput

    with pytest.raises(ValidationError, match="read-only"):
        GeoMapReviewInput(file_path="map.png", task="Inspect", all_pages=True, auto_repair=True)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def setup_pages(root):
    image, detail = root / "preview.png", root / "detail_page_2.png"
    Image.new("RGB", (100, 100), "white").save(image)
    Image.new("RGB", (100, 100), "lightblue").save(detail)
    (root / "map.qgz").write_bytes(b"saved layout")
    layout = {"bounds_check": {"checked": True, "valid": True, "problems": []}}
    manifest = root / "project.json"
    manifest.write_text(json.dumps({"backend": "qgis", "preview": image.name,
        "preview_sha256": sha(image), "project_file": "map.qgz", "layout": layout,
        "files": [image.name, detail.name, "map.qgz", manifest.name],
        "native_facts": {"page_previews": [
            {"page_index": 0, "path": str(image), "frames": ["main_map"]},
            {"page_index": 1, "path": str(detail), "frames": ["detail_map"]}]}}),
        encoding="utf-8")
    config, metadata = GeoConfig(), {}
    job = ensure_map_job(metadata, cwd=root, config=config)
    receipt = record_map_version(job, metadata, cwd=root, config=config, image_path=image,
        project_dir=root, layout=layout, preflight={"status": "passed",
            "scope": "map_data_preflight", "display_crs": "EPSG:4326",
            "map_extent": [0, 0, 1, 1], "layers": [{"feature_count": 1}],
            "checks": {"geometry": "passed"}})
    binding = {"version": receipt["current_version"], "primary_image_sha256": sha(image),
               "manifest_sha256": sha(manifest)}
    return config, metadata, job, image, detail, binding


def test_all_map_pages_required_without_new_render_and_tampering_stales(tmp_path):
    config, metadata, job, image, detail, binding = setup_pages(tmp_path)
    receipt = record_map_review(job, metadata, cwd=tmp_path, config=config, image_path=image,
        review={"image_sha256": sha(image), "status": "unverified", "issues": [],
            "diagnostics": {"page_visual_status": "passed", "review_binding": {
                **binding, "page_index": 0}}})
    assert receipt["quality_status"] == "draft"
    assert [item["status"] for item in receipt["stages"]["visual_checked"]["page_reviews"]] == [
        "passed", "unverified"]
    receipt = record_map_review(job, metadata, cwd=tmp_path, config=config, image_path=detail,
        review={"image_sha256": sha(detail), "status": "passed", "issues": [],
            "diagnostics": {"review_binding": {**binding, "page_index": 1}}})
    assert receipt["quality_status"] == "passed"
    assert receipt["latest_version"] == 1
    assert receipt["stages"]["visual_checked"]["scope"] == "all_native_map_pages"
    detail.write_bytes(b"edited after review")
    stale = map_job_receipt(metadata, cwd=tmp_path, config=config, map_id=job)
    assert stale["quality_status"] == "draft"
    assert stale["stages"]["visual_checked"]["status"] == "stale"


def test_secondary_review_requires_exact_version_and_primary_hash(tmp_path):
    config, metadata, job, image, detail, binding = setup_pages(tmp_path)
    record_map_review(job, metadata, cwd=tmp_path, config=config, image_path=detail,
        review={"image_sha256": sha(detail), "status": "passed", "diagnostics": {
            "review_binding": {**binding, "version": 9, "page_index": 1}}})
    receipt = map_job_receipt(metadata, cwd=tmp_path, config=config, map_id=job)
    assert receipt["stages"]["visual_checked"]["status"] != "passed"
    assert receipt["latest_version"] == 1


async def test_public_page_review_keeps_one_version_and_reuses_passed_pixels(tmp_path):
    from openharness.api.client import ApiMessageCompleteEvent
    from openharness.api.usage import UsageSnapshot
    from openharness.engine.messages import ConversationMessage
    from openharness.tools.base import ToolExecutionContext

    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    class Reviewer:
        calls = 0

        async def stream_message(self, request):
            self.calls += 1
            yield ApiMessageCompleteEvent(message=ConversationMessage.from_user_text(json.dumps(
                {"status": "passed", "summary": "Readable", "issues": []})), usage=UsageSnapshot())

    config, metadata, job, image, detail, _ = setup_pages(tmp_path)
    reviewer = Reviewer()
    context = ToolExecutionContext(cwd=tmp_path, metadata={**metadata,
        "geoharness_config": config, "map_review_client": reviewer})
    tool = GeoMapReviewTool()
    main = await tool.execute(GeoMapReviewInput(file_path=str(image), map_id=job,
        task="Check all map pages", auto_repair=False), context)
    assert not main.is_error, main.output
    assert main.metadata["map_receipt"]["quality_status"] == "draft"
    reviewed = await tool.execute(GeoMapReviewInput(file_path=str(detail), map_id=job,
        task="Check all map pages", auto_repair=False), context)
    assert not reviewed.is_error, reviewed.output
    assert reviewed.metadata["map_receipt"]["quality_status"] == "passed"
    assert reviewed.metadata["map_receipt"]["latest_version"] == 1
    repeated = await tool.execute(GeoMapReviewInput(file_path=str(image), map_id=job,
        task="Check all map pages", auto_repair=False), context)
    assert not repeated.is_error, repeated.output
    assert repeated.metadata["review_status"] == "passed"
    assert repeated.metadata["map_receipt"]["quality_status"] == "passed"
    assert reviewer.calls == 2  # Each actual page once; no call for unchanged accepted pixels.
