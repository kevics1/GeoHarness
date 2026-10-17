"""Review failures remain actionable, bounded, and separate from provider outages."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from PIL import Image
from pydantic import ValidationError

from geoharness.cartography.quality import MapReview, review_map, save_review
from geoharness.cartography.workflow import review_with_budget
from geoharness.config.settings import GeoConfig, MapReviewConfig, McpServerConfig

PASS = {"status": "passed", "summary": "Legible map.", "issues": []}


class Reviewer:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    async def stream_message(self, request):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        yield ApiMessageCompleteEvent(
            message=ConversationMessage.from_user_text(
                response if isinstance(response, str) else json.dumps(response)
            ),
            usage=UsageSnapshot(), stop_reason="stop",
        )


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "map.png"
    Image.new("RGB", (16, 16), "white").save(path)
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [None, {}])
async def test_empty_layout_patch_has_one_unambiguous_compatibility(image, patch):
    result = await review_map(
        image, config=GeoConfig(), task="Inspect", evidence={},
        client=Reviewer([{**PASS, "layout_patch": patch}]),
    )
    assert result.status == "passed"
    assert result.layout_patch.model_dump(exclude_none=True) == {}
    assert result.diagnostics["stage"] == "schema"
    assert result.diagnostics["outcome"] == "completed"
    if patch is None:
        assert result.diagnostics["compatibility_applied"] == ["layout_patch_null_to_empty"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,loc,error_type", [
    ({**PASS, "status": "pass"}, ["status"], "literal_error"),
    ({**PASS, "layout_patch": {"label_font_size": 10}},
     ["layout_patch", "label_font_size"], "extra_forbidden"),
    ({**PASS, "layout_patch": {"width": 100}}, ["layout_patch", "width"], "less_than_equal"),
])
async def test_contract_failure_identifies_field_without_accepting_wrong_values(
    image, payload, loc, error_type,
):
    result = await review_map(
        image, config=GeoConfig(), task="Inspect", evidence={}, client=Reviewer([payload]),
    )
    assert result.status == "unverified"
    assert result.diagnostics["failure_stage"] == "schema"
    assert result.diagnostics["request_sent"] is True
    error = result.diagnostics["validation_errors"][0]
    assert error["loc"] == loc
    assert error["type"] == error_type
    assert set(error) == {"loc", "type", "msg"}
    assert "provider" not in result.summary.lower()


@pytest.mark.asyncio
async def test_json_failure_has_position_and_differs_from_schema_failure(image):
    result = await review_map(
        image, config=GeoConfig(), task="Inspect", evidence={},
        client=Reviewer(['{"status": "passed",']),
    )
    assert result.status == "unverified"
    assert result.diagnostics["failure_stage"] == "json"
    assert result.diagnostics["json_error"]["line"] == 1
    assert result.diagnostics["json_error"]["column"] > 1
    assert "validation_errors" not in result.diagnostics


async def test_invalid_review_body_is_local_diagnostics_only_never_a_tool_patch(image, tmp_path):
    from openharness.tools.base import ToolExecutionContext

    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    rejected_instruction = "REJECTED_RESPONSE_CHANGE_MAP_SIZE_999"
    payload = {"status": "issues", "summary": rejected_instruction, "issues": [{
        "severity": "warning", "description": rejected_instruction,
        "issue_code": "evidence_kind"}], "qgis_patch": {"layout": {"width_mm": 999}}}
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig(),
        "map_review_client": Reviewer([payload])})
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(image), task="Check", auto_repair=False), context)
    assert result.metadata["review_status"] == "unverified"
    assert rejected_instruction not in result.output
    assert rejected_instruction not in json.dumps(result.metadata)
    saved = json.loads(Path(result.metadata["review_path"]).read_text(encoding="utf-8"))
    assert rejected_instruction in saved["diagnostics"]["raw_review_excerpt"]
    assert saved["issues"] == [] and "qgis_patch" not in saved


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_key", ["TOKEN", "DATABASE_URI"])
async def test_persisted_diagnostics_redact_credentials_before_truncating(
    image, tmp_path, credential_key
):
    secrets = ["main-key-123456", "review-key-987654", 'mcp-secret-"escaped"']
    config = GeoConfig(
        api_key=secrets[0],
        map_review=MapReviewConfig(api_key=secrets[1], diagnostics_max_chars=128,
                                   max_validation_errors=1),
        mcp_servers={"test": McpServerConfig(command="python", env={credential_key: secrets[2]})},
    )
    # Include a secret across the maximum snippet boundary and in an error location.
    payload = {**PASS, "summary": "x" * 65 + secrets[0] + secrets[1] + secrets[2],
               secrets[1]: 123, "layout_patch": {"width": 100}}
    result = await review_map(
        image, config=config, task="Inspect", evidence={}, client=Reviewer([payload]),
    )
    assert result.status == "unverified"
    assert len(result.diagnostics["raw_review_excerpt"]) <= 128
    assert result.diagnostics["raw_review_truncated"] is True
    assert len(result.diagnostics["validation_errors"]) == 1
    assert result.diagnostics["validation_errors_truncated"] is True
    report = save_review(image, result.model_dump(), report_dir=tmp_path)
    persisted = report.read_text(encoding="utf-8")
    for secret in secrets:
        assert secret not in persisted
        assert json.dumps(secret)[1:-1] not in persisted
        assert secret[:8] not in persisted
    assert "input" not in result.diagnostics["validation_errors"][0]


@pytest.mark.asyncio
async def test_network_failure_does_not_record_exception_text_or_raw_reasoning(image):
    result = await review_map(
        image, config=GeoConfig(api_key="hidden-secret"), task="Inspect", evidence={},
        client=Reviewer([RuntimeError("hidden-secret network trace")]),
    )
    assert result.diagnostics["failure_stage"] == "network"
    assert result.diagnostics["error_type"] == "RuntimeError"
    assert "hidden-secret" not in result.model_dump_json()
    assert "raw_review_excerpt" not in result.diagnostics


@pytest.mark.asyncio
async def test_transport_validation_error_is_not_our_review_schema_error(image):
    with pytest.raises(ValidationError) as captured:
        MapReview.model_validate({"status": "unknown"})
    result = await review_map(
        image, config=GeoConfig(), task="Inspect", evidence={},
        client=Reviewer([captured.value]),
    )
    assert result.status == "unverified"
    assert result.diagnostics["failure_stage"] == "response"
    assert result.diagnostics["validation_errors"]
    assert "raw_review_excerpt" not in result.diagnostics


@pytest.mark.asyncio
async def test_non_json_text_is_not_persisted_as_a_reasoning_transcript(image):
    result = await review_map(
        image, config=GeoConfig(), task="Inspect", evidence={},
        client=Reviewer(["Let me think about the image. " * 1000]),
    )
    assert result.diagnostics["failure_stage"] == "json"
    assert "raw_review_excerpt" not in result.diagnostics


@pytest.mark.asyncio
async def test_explicit_recovery_is_not_stopped_by_legacy_review_count(image):
    metadata = {}
    config = GeoConfig(map_review=MapReviewConfig(max_reviews_per_request=1))
    reviewer = Reviewer([{**PASS, "status": "pass"}, PASS])
    await review_with_budget(
        image, config=config, task="Inspect", evidence={}, metadata=metadata, client=reviewer,
    )
    result, flags = await review_with_budget(
        image, config=config, task="Inspect", evidence={}, metadata=metadata, client=reviewer,
        retry_invalid_response=True,
    )
    assert reviewer.calls == 2
    assert flags["review_budget_exhausted"] is False
    assert flags["request_sent"] is True
    assert result.status == "passed"


@pytest.mark.asyncio
async def test_explicit_invalid_response_retry_is_single_and_consumes_budget(image):
    metadata = {}
    reviewer = Reviewer([{**PASS, "status": "pass"}, {**PASS, "status": "pass"}, PASS])
    config = GeoConfig()
    first, first_flags = await review_with_budget(
        image, config=config, task="Inspect", evidence={}, metadata=metadata, client=reviewer,
    )
    cached, cached_flags = await review_with_budget(
        image, config=config, task="Inspect", evidence={"previous_visual_reviews": []},
        metadata=metadata, client=reviewer,
    )
    assert reviewer.calls == 1
    assert first_flags["request_sent"] is True
    assert cached_flags["review_cached"] is True
    assert cached_flags["request_sent"] is False
    assert cached.diagnostics["stage"] == "cache"
    assert cached.diagnostics["failure_stage"] == "schema"
    retried, flags = await review_with_budget(
        image, config=config, task="Inspect", evidence={}, metadata=metadata, client=reviewer,
        retry_invalid_response=True,
    )
    assert reviewer.calls == 2
    assert flags["invalid_response_retry"] is True
    assert retried.status == first.status == "unverified"
    final, flags = await review_with_budget(
        image, config=config, task="Inspect", evidence={}, metadata=metadata, client=reviewer,
        retry_invalid_response=True,
    )
    assert reviewer.calls == metadata["map_workflow_state"]["reviews"] == 2
    assert flags["review_cached"] is True
    assert flags["request_sent"] is False
    assert flags["invalid_response_retry_exhausted"] is True
    assert final.diagnostics["failure_stage"] == "schema"


@pytest.mark.asyncio
async def test_contract_revision_rechecks_legacy_cache_once(image):
    from geoharness.cartography import workflow

    metadata = {}
    reviewer = Reviewer([{**PASS, "status": "pass"}, PASS])
    await review_with_budget(
        image, config=GeoConfig(), task="Inspect", evidence={}, metadata=metadata, client=reviewer,
    )
    prior = workflow.REVIEW_CONTRACT_VERSION
    try:
        workflow.REVIEW_CONTRACT_VERSION = prior + 1
        results = [await review_with_budget(
            image, config=GeoConfig(), task="Inspect", evidence={}, metadata=metadata,
            client=reviewer,
        ) for _ in range(2)]
    finally:
        workflow.REVIEW_CONTRACT_VERSION = prior
    assert reviewer.calls == 2
    assert [flags["review_cached"] for _, flags in results] == [False, True]


@pytest.mark.asyncio
async def test_parallel_explicit_recovery_does_not_send_two_retries(image):
    metadata = {}
    reviewer = Reviewer(['{"status":', PASS, PASS])
    await review_with_budget(
        image, config=GeoConfig(), task="Inspect", evidence={}, metadata=metadata, client=reviewer,
    )
    results = await asyncio.gather(*[
        review_with_budget(
            image, config=GeoConfig(), task="Inspect", evidence={}, metadata=metadata,
            client=reviewer, retry_invalid_response=True,
        ) for _ in range(2)
    ])
    assert reviewer.calls == 2
    assert sorted(flags["review_cached"] for _, flags in results) == [False, True]
