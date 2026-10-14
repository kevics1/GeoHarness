"""Offline service recovery: cooldown is not visual evidence or another HTTP request."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.tools.base import ToolExecutionContext, ToolResult
from PIL import Image, PngImagePlugin
from pydantic import ValidationError

from geoharness.cartography import workflow
from geoharness.config.settings import GeoConfig, MapReviewConfig
from geoharness.session_state import _workflow
from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool


class ServiceFailure(Exception):
    def __init__(self, status, retry_after="120"):
        self.status_code = status
        self.code = "service_unavailable"
        self.response = SimpleNamespace(headers={"retry-after": retry_after})
        super().__init__("Secret request body must not be persisted")


class Reviewer:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0

    async def stream_message(self, request):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        yield ApiMessageCompleteEvent(
            message=ConversationMessage.from_user_text(json.dumps(response)),
            usage=UsageSnapshot(), stop_reason="stop",
        )


@pytest.fixture
def image(tmp_path):
    image = tmp_path / "map.png"
    Image.new("RGB", (16, 16), "white").save(image)
    return image


async def test_transient_failure_expires_once_and_success_stays_cached(image, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(workflow.time, "time", lambda: now[0])
    client = Reviewer(ServiceFailure(503), {"status": "passed", "summary": "Legible"})
    metadata = {}

    async def review(**kwargs):
        return await workflow.review_with_budget(
            image, config=GeoConfig(), task="Inspect", evidence={}, metadata=metadata,
            client=client, **kwargs,
        )

    failed, flags = await review()
    assert failed.status == "unverified"
    assert flags["image_provider_calls"] == 1
    assert flags["service_retry_after_seconds"] == 120
    assert failed.diagnostics["service_error_code"] == "service_unavailable"
    assert "Secret request body" not in failed.model_dump_json()
    for _ in range(2):
        held, flags = await review(retry_service_failure=True)
        assert not flags["request_sent"]
        assert not flags["visual_review_completed"]
        assert flags["next_action"] == "wait_then_retry_same_image"
    assert client.calls == 1
    assert flags["image_cache_hits"] == 2
    assert flags["request_provider_calls"] == 1 and flags["request_cache_hits"] == 2
    now[0] += 120
    completed, flags = await review(retry_service_failure=True)
    assert completed.status == "passed"
    assert flags["image_provider_calls"] == 2
    assert flags["visual_review_completed"]
    cache = metadata["map_workflow_state"]["cache"]
    next(iter(cache.values()))["diagnostics"].pop("outcome")
    now[0] += 10000
    cached, flags = await review(retry_service_failure=True)
    assert cached.status == "passed" and not flags["request_sent"]
    assert flags["visual_review_completed"] and client.calls == 2


async def test_cooldown_survives_reexport_request_reset_and_checkpoint(image, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(workflow.time, "time", lambda: now[0])
    client = Reviewer(ServiceFailure(503, retry_after="20"),
                      ServiceFailure(503, retry_after="20"))
    metadata = {}
    config = GeoConfig()

    async def review(path):
        return await workflow.review_with_budget(
            path, config=config, task="Inspect", evidence={}, metadata=metadata,
            client=client, retry_service_failure=True,
        )

    await review(image)
    png_info = PngImagePlugin.PngInfo()
    png_info.add_text("export", "new export has different bytes")
    exported = image.with_name("export_v9.png")
    Image.open(image).save(exported, pnginfo=png_info)
    workflow.reset_map_workflow(metadata)
    metadata["map_workflow_state"] = _workflow(
        metadata["map_workflow_state"], (), restoring=False,
    )
    metadata["map_workflow_state"] = _workflow(
        metadata["map_workflow_state"], (), restoring=True,
    )
    _, held = await review(exported)
    assert held["service_retry_after_seconds"] == 60
    assert not held["request_sent"] and client.calls == 1
    now[0] += 60
    results = await asyncio.gather(review(image), review(exported))
    assert sum(flags["request_sent"] for _, flags in results) == 1
    assert client.calls == 2


@pytest.mark.parametrize("status", [400, 401])
async def test_explicit_recovery_does_not_retry_nontransient_errors(image, status, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(workflow.time, "time", lambda: now[0])
    client = Reviewer(ServiceFailure(status))
    metadata = {}
    for _ in range(2):
        result, flags = await workflow.review_with_budget(
            image, config=GeoConfig(), task="Inspect", evidence={}, metadata=metadata,
            client=client, retry_service_failure=True, retry_invalid_response=True,
        )
        now[0] += 10000
        assert result.status == "unverified"
        assert not flags["service_retry_allowed"]
        assert flags["next_action"] == "inspect_failure_and_fix_configuration_or_input"
    assert client.calls == 1


def test_cooldown_config_is_bounded_and_finite():
    for value in [0, -1, float("inf"), float("nan"), 86401]:
        with pytest.raises(ValidationError):
            MapReviewConfig(service_failure_ttl_seconds=value)


async def test_explicit_service_recovery_never_starts_automatic_redraw(image, monkeypatch):
    from geoharness.cartography import jobs, quality_repair

    async def reviewed(self, arguments, context):
        return ToolResult(output="New review found issues", metadata={
            "map_id": "same-map", "image_path": str(image), "review_status": "issues",
        })

    def no_package(*args, **kwargs):
        raise ValueError("No packaged candidate")

    monkeypatch.setattr(GeoMapReviewTool, "_review_one", reviewed)
    monkeypatch.setattr(jobs, "map_job_receipt", lambda *args, **kwargs: {
        "quality_status": "draft", "stages": {},
    })
    monkeypatch.setattr(quality_repair, "verified_package", no_package)
    result = await GeoMapReviewTool()._execute_locked(
        GeoMapReviewInput(file_path=str(image), task="Recheck", retry_service_failure=True,
                          auto_repair=True),
        ToolExecutionContext(cwd=image.parent, metadata={"geoharness_config": GeoConfig()}),
    )
    assert result.metadata["repair_count"] == 0
    assert result.metadata["repair_stop_reason"] == "review_only"


async def test_completed_cache_survives_reset_but_new_target_rechecks(image):
    client = Reviewer(*[{"status": "passed", "summary": "Legible"}] * 3)
    metadata = {}

    async def review(task="Inspect", **evidence):
        return await workflow.review_with_budget(
            image, config=GeoConfig(), task=task, evidence=evidence, metadata=metadata,
            client=client,
        )

    await review(expected_layers=["fire"])
    workflow.reset_map_workflow(metadata)
    _, flags = await review(expected_layers=["fire"], previous_visual_reviews=["new history"])
    assert not flags["request_sent"] and flags["visual_review_completed"]
    assert flags["request_provider_calls"] == 0 and flags["request_cache_hits"] == 1
    _, flags = await review(task="Inspect evacuation", expected_layers=["fire"])
    assert flags["request_sent"] and client.calls == 2
    _, flags = await review(task="Inspect evacuation", expected_layers=["fire", "shelters"])
    assert flags["request_sent"] and client.calls == 3


async def test_cancelled_started_review_is_counted_without_accepting_conclusion(image):
    entered = asyncio.Event()

    class BlockingReviewer:
        async def stream_message(self, request):
            entered.set()
            await asyncio.Event().wait()
            yield  # This client never completes.

    metadata = {}
    running = asyncio.create_task(workflow.review_with_budget(
        image, config=GeoConfig(), task="Inspect", evidence={}, metadata=metadata,
        client=BlockingReviewer(),
    ))
    await asyncio.wait_for(entered.wait(), timeout=10)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    state = metadata["map_workflow_state"]
    assert state["provider_calls_total"] == 1
    assert next(iter(state["review_activity"].values()))["provider_calls"] == 1
    assert not state["cache"] and not state["reserved_seconds"]
