"""The public reviewer sees real pixels and can never change a rendered map."""

import asyncio
import hashlib
import json
from types import SimpleNamespace as NS

import pytest
from openharness.tools.base import ToolExecutionContext
from PIL import Image
from pydantic import ValidationError

from geoharness.cartography import quality
from geoharness.config.settings import GeoConfig, MapReviewConfig
from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

PASS = {"status": "passed", "summary": "Visible content is readable.", "issues": []}


def chunk(*, args="", name=None, content=None, reasoning=None, finish=None, usage=None):
    delta = NS(content=content, reasoning_content=reasoning,
               tool_calls=[NS(index=0, function=NS(name=name, arguments=args))]
               if args or name else [])
    return NS(model="returned-model", usage=usage,
              choices=[NS(index=0, delta=delta, finish_reason=finish)])


class Stream:
    def __init__(self, events, error=None):
        self.events, self.error, self.closed = events, error, False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def __aiter__(self):
        for event in self.events:
            yield event
        if self.error:
            raise self.error


def install_transport(monkeypatch, stream):
    requests, options, closed = [], [], []

    async def create(**params):
        requests.append(params)
        return stream

    class Client:
        def __init__(self, **kwargs):
            self._client = self
            self.chat = NS(completions=NS(create=create))

        def with_options(self, **kwargs):
            options.append(kwargs)
            return self

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(quality, "OpenAICompatibleClient", Client)
    return requests, options, closed


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "map.png"
    Image.new("RGB", (80, 80), "white").save(path)
    return path


async def test_pixel_request_keeps_images_max_and_excludes_engineering_and_repairs(
    image, monkeypatch,
):
    raw = json.dumps(PASS)
    stream = Stream([chunk(reasoning="private thinking"),
                     chunk(name="record_map_review", args=raw[:20]),
                     chunk(args=raw[20:], finish="tool_calls")])
    requests, options, closed = install_transport(monkeypatch, stream)
    config = GeoConfig(api_key="private-test-key", map_review=MapReviewConfig(
        reasoning_effort="max", request_extra_body={"thinking": {"type": "disabled"}}))
    evidence = {"backend": "qgis", "render_metadata": {
        "native_facts": {"frames": {"main_map": {
            "page_index": 0, "purpose": "Overview; numbering on the detail page",
            "points": [{"private_engineering_marker": "x"}] * 2000,
        }}}, "review_page_index": 0,
    }}
    result = await quality.review_map(image, config=config, task="Inspect pixels",
                                      evidence=evidence, pixel_only=True)
    assert result.status == "passed"
    request = requests[0]
    assert request["stream"] is True
    assert request["extra_body"]["reasoning_effort"] == "max"
    assert request["extra_body"]["thinking"] == {"type": "enabled"}
    body = json.dumps(request, ensure_ascii=False)
    assert "data:image/png;base64," in body
    assert "private_engineering_marker" not in body
    assert "qgis_patch" not in body and "layout_patch" not in body
    assert "next_strategy" not in body
    assert "Overview; numbering" in body
    assert len(body) < 15000
    assert result.diagnostics["first_reasoning_seconds"] >= 0
    assert "private thinking" not in json.dumps(result.model_dump())
    assert result.diagnostics["review_mode"] == "pixels_only"
    assert result.diagnostics["output_token_limit"] == config.map_review.max_tokens
    assert result.diagnostics["finish_reason"] == "tool_calls"
    assert result.qgis_patch is None
    assert options[0]["max_retries"] == 0
    assert closed and stream.closed


@pytest.mark.parametrize("events,error,stage,kind", [
    ([chunk(reasoning="secret", finish="length")], None, "budget", "output_budget"),
    ([chunk(args='{"status":')], None, "response", "incomplete_stream"),
    ([], TimeoutError(), "network", "transport_timeout"),
    ([chunk(content=json.dumps({**PASS, "qgis_patch": {"layers": []}}), finish="stop")],
     None, "schema", None),
])
async def test_stream_failures_never_accept_partial_or_repair_results(
    image, monkeypatch, events, error, stage, kind,
):
    stream = Stream(events, error)
    install_transport(monkeypatch, stream)
    result = await quality.review_map(image, config=GeoConfig(api_key="test"), task="Inspect",
                                      evidence={}, pixel_only=True)
    assert result.status == "unverified"
    assert result.diagnostics["failure_stage"] == stage
    if kind:
        assert result.diagnostics["failure_kind"] == kind
    assert result.qgis_patch is None
    assert stream.closed


async def test_public_review_ignores_geometry_shortcut_and_never_renders(image, monkeypatch):
    from openharness.api.client import ApiMessageCompleteEvent
    from openharness.api.usage import UsageSnapshot
    from openharness.engine.messages import ConversationMessage

    calls = []

    class Reviewer:
        async def stream_message(self, request):
            calls.append(request)
            yield ApiMessageCompleteEvent(
                message=ConversationMessage.from_user_text(json.dumps(PASS)), usage=UsageSnapshot())

    def forbidden(*args, **kwargs):
        pytest.fail("Image review must not invoke native rendering")

    monkeypatch.setattr("geoharness.tools.geo_qgis_project.GeoQgisProjectTool.execute", forbidden)
    config = GeoConfig(map_review=MapReviewConfig(auto_repair=True))
    context = ToolExecutionContext(cwd=image.parent, metadata={
        "geoharness_config": config, "map_review_client": Reviewer()})
    # A stale/incomplete sidecar must never replace a real image review.
    image.with_suffix(".layout.json").write_text(json.dumps({
        "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "scope": "layout_geometry", "panel_overlaps": [["main", "legend"]],
    }))
    before = image.read_bytes()
    result = await GeoMapReviewTool().execute(
        GeoMapReviewInput(file_path=str(image), task="Inspect"), context)
    assert not result.is_error
    assert len(calls) == 1
    assert image.read_bytes() == before
    assert result.metadata["render_executed"] is False
    payload, _ = json.JSONDecoder().raw_decode(result.output)
    assert not {"qgis_patch", "layout_patch", "next_strategy"} & payload.keys()


def test_public_schema_has_no_repair_or_arbitrary_evidence_input():
    properties = GeoMapReviewInput.model_json_schema()["properties"]
    assert "auto_repair" not in properties
    assert "evidence" not in properties
    assert GeoMapReviewTool().is_read_only(GeoMapReviewInput(file_path="map.png", task="Inspect"))
    with pytest.raises(ValidationError, match="read-only"):
        GeoMapReviewInput(file_path="map.png", task="Inspect", auto_repair=True)


async def test_total_deadline_is_distinct_from_transport_timeout(image, monkeypatch):
    class SlowStream(Stream):
        async def __aiter__(self):
            yield chunk(reasoning="secret")
            await asyncio.sleep(1)

    stream = SlowStream([])
    install_transport(monkeypatch, stream)
    result = await quality.review_map(image, task="Inspect", evidence={}, pixel_only=True,
        config=GeoConfig(api_key="test", map_review=MapReviewConfig(timeout_seconds=.05)))
    assert result.status == "unverified"
    assert result.diagnostics["failure_kind"] == "total_deadline"
    assert result.diagnostics["first_reasoning_seconds"] >= 0
    assert stream.closed


async def test_cancelled_owned_stream_closes_transport(image, monkeypatch):
    entered = asyncio.Event()

    class Waiting(Stream):
        async def __aiter__(self):
            entered.set()
            await asyncio.Event().wait()
            yield chunk()

    stream = Waiting([])
    _, _, closed = install_transport(monkeypatch, stream)
    task = asyncio.create_task(quality.review_map(image, config=GeoConfig(api_key="test"),
        task="Inspect", evidence={}, pixel_only=True))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed and closed


async def test_changed_image_cannot_inherit_previous_pixel_pass(image, monkeypatch):
    class Changed(Stream):
        async def __aiter__(self):
            Image.new("RGB", (80, 80), "red").save(image)
            yield chunk(name="record_map_review", args=json.dumps(PASS), finish="tool_calls")

    stream = Changed([])
    install_transport(monkeypatch, stream)
    result = await quality.review_map(image, config=GeoConfig(api_key="test"),
        task="Inspect", evidence={}, pixel_only=True)
    assert result.status == "unverified"
    assert result.diagnostics["failure_kind"] == "image_changed"
    assert result.diagnostics["failure_stage"] == "binding"


async def test_ambiguous_calls_are_unverified_and_usage_tail_is_retained(image, monkeypatch):
    event = chunk(args=json.dumps(PASS), name="record_map_review", finish="tool_calls")
    event.choices[0].delta.tool_calls.append(NS(index=1, function=NS(name="other", arguments="{}")))
    stream = Stream([event])
    install_transport(monkeypatch, stream)
    result = await quality.review_map(image, config=GeoConfig(api_key="test"),
        task="Inspect", evidence={}, pixel_only=True)
    assert result.status == "unverified"
    assert result.diagnostics["failure_kind"] == "ambiguous_response"
    assert stream.closed
    usage = NS(prompt_tokens=20, completion_tokens=40, total_tokens=60,
               completion_tokens_details=NS(reasoning_tokens=30))
    tail = NS(model="returned-model", choices=[], usage=usage)
    stream = Stream([chunk(name="record_map_review", args=json.dumps(PASS), finish="tool_calls"),
                     tail])
    install_transport(monkeypatch, stream)
    result = await quality.review_map(image, config=GeoConfig(api_key="test"),
        task="Inspect", evidence={}, pixel_only=True)
    assert result.status == "passed"
    assert result.diagnostics["reasoning_tokens"] == 30
    assert result.diagnostics["total_tokens"] == 60


async def test_legacy_cache_cannot_skip_pixel_review_and_deadline_is_not_service_outage(
    image, monkeypatch,
):
    from geoharness.cartography import workflow

    calls = []

    async def reviewer(*args, **kwargs):
        calls.append(kwargs.get("pixel_only", False))
        return quality.MapReview(status="passed", summary="Reviewed", diagnostics={
            "request_sent": True, "stage": "response"})

    monkeypatch.setattr(workflow, "review_map", reviewer)
    metadata = {}
    for pixel_only in [False, True, True]:
        await workflow.review_with_budget(image, config=GeoConfig(), task="Inspect", evidence={},
            metadata=metadata, pixel_only=pixel_only)
    assert calls == [False, True]

    async def deadline(*args, **kwargs):
        return quality.MapReview(status="unverified", summary="Deadline", diagnostics={
            "request_sent": True, "stage": "network", "failure_stage": "network",
            "failure_kind": "total_deadline", "service_failure_transient": False})

    monkeypatch.setattr(workflow, "review_map", deadline)
    _, flags = await workflow.review_with_budget(image, config=GeoConfig(), task="New scope",
        evidence={}, metadata=metadata, pixel_only=True)
    assert not flags["service_retry_allowed"]
    assert flags["service_retry_after_seconds"] == 0
    assert flags["next_action"] == "inspect_failure_and_fix_configuration_or_input"
