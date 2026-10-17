"""Image probes use one request, and never certify a business map from a small test."""

import hashlib
import json

import pytest
from PIL import Image

from geoharness.cartography import quality, review_doctor
from geoharness.config.settings import GeoConfig


async def test_synthetic_probe_checks_unprompted_digits_and_shapes(tmp_path, monkeypatch):
    monkeypatch.setattr(review_doctor.secrets, "randbelow", lambda limit: 234567)
    calls = []

    async def review(path, **kwargs):
        calls.append(kwargs)
        assert Image.open(path).size == (640, 360)
        assert "334567" not in kwargs["task"]
        return quality.MapReview(status="passed", summary="334567 红圆、蓝方、绿三角",
                                 diagnostics={"outcome": "completed"})

    monkeypatch.setattr(quality, "review_map", review)
    result = await review_doctor.run_review_probe(GeoConfig(), tmp_path)
    assert len(calls) == 1 and calls[0]["pixel_only"]
    assert result["image_capability_verified"] is True
    assert result["business_quality_verified"] is False
    assert json.loads((tmp_path / result["report_path"]).read_text())["status"] == "succeeded"


async def test_wrong_answer_is_not_image_capability_success(tmp_path, monkeypatch):
    async def review(*args, **kwargs):
        return quality.MapReview(
            status="passed", summary="123456 red circle blue square green triangle",
                                 diagnostics={"outcome": "completed"})

    monkeypatch.setattr(review_doctor.secrets, "randbelow", lambda limit: 234567)
    monkeypatch.setattr(quality, "review_map", review)
    result = await review_doctor.run_review_probe(GeoConfig(), tmp_path)
    assert result["status"] == "failed"
    assert result["review_completed"] is True
    assert result["image_capability_verified"] is False


async def test_failed_review_does_not_certify_image_or_leak_secrets(tmp_path, monkeypatch):
    async def review(*args, **kwargs):
        return quality.MapReview(status="unverified", summary="secret-key",
            diagnostics={"outcome": "failed", "failure_kind": "total_deadline"})

    monkeypatch.setattr(quality, "review_map", review)
    result = await review_doctor.run_review_probe(GeoConfig(api_key="secret-key"), tmp_path)
    assert not result["image_capability_verified"]
    assert not result["review_completed"]
    assert "secret-key" not in json.dumps(result)


async def test_actual_image_probe_never_changes_input_or_certifies_business(tmp_path, monkeypatch):
    image = tmp_path / "map.png"
    Image.new("RGB", (60, 60), "red").save(image)
    before = hashlib.sha256(image.read_bytes()).hexdigest()

    async def review(path, **kwargs):
        assert path == image
        return quality.MapReview(status="issues", summary="Label overlap",
                                 diagnostics={"outcome": "completed"})

    monkeypatch.setattr(quality, "review_map", review)
    result = await review_doctor.run_review_probe(GeoConfig(), tmp_path / "reports", image)
    assert result["status"] == "succeeded"
    assert result["image_capability_verified"] is None
    assert result["business_quality_verified"] is False
    assert hashlib.sha256(image.read_bytes()).hexdigest() == before


@pytest.mark.parametrize("summary", [
    "334567 红三角、蓝圆、绿方", "334567 red square blue triangle green circle", "红圆蓝方绿三角",
])
def test_capability_does_not_accept_wrong_color_shape_pairing(summary):
    assert not review_doctor._identified(summary, "334567")
