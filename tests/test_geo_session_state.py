"""Geo session checkpoints preserve budgets without importing runtime or success claims."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, ToolUseBlock
from openharness.services import session_storage
from openharness.tools.base import ToolExecutionContext, ToolResult
from pydantic import BaseModel

from geoharness.cartography.jobs import begin_map_request, ensure_map_job
from geoharness.cartography.quality import MapReview
from geoharness.cartography.retry_guard import guard_map_errors
from geoharness.cartography.workflow import reserve_map_render, review_with_budget
from geoharness.config.settings import AgentConfig, GeoConfig, MapReviewConfig
from geoharness.data.profiles import current_profiles, file_signature, make_profile, record_profiles
from geoharness.session_state import (
    GEO_SESSION_CHECKPOINT_KEY,
    GEO_SESSION_CHECKPOINT_VERSION,
    MAX_BUDGET_ENTRIES,
    MAX_CHECKPOINT_BYTES,
    MAX_REQUEST_FAILURES,
    GeoSessionBackend,
    restore_geo_session_state,
)


@pytest.fixture
def backend(tmp_path, monkeypatch):
    state = tmp_path / ".geoharness"
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(state))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(state / "data"))
    return GeoSessionBackend()


def _metadata(config=None):
    return {"geoharness_config": config or GeoConfig(), "map_workflow_state": {},
            "map_job_state": {}}


def _save(backend, tmp_path, metadata, *, session_id="checkpoint"):
    return backend.save_snapshot(
        cwd=tmp_path, model="offline-model", system_prompt="offline",
        messages=[ConversationMessage.from_user_text("Resume the map")],
        usage=UsageSnapshot(input_tokens=2, output_tokens=3),
        session_id=session_id, tool_metadata=metadata,
    )


def _restore(backend, tmp_path, metadata, *, config=None):
    _save(backend, tmp_path, metadata)
    restored = _metadata(config or metadata["geoharness_config"])
    assert restore_geo_session_state(
        restored, backend.load_latest(tmp_path)["tool_metadata"],
        cwd=tmp_path, config=restored["geoharness_config"],
    )
    return restored


def test_snapshot_keeps_upstream_schema_whitelist_and_public_operations(backend, tmp_path):
    metadata = _metadata()
    metadata.update(task_focus_state={"goal": "Map the data"}, permission_mode="default",
                    invoked_skills=["cartography"], read_file_state={"file": {"line": 2}},
                    data_catalog=object(), api_key="never serialize arbitrary keys")
    path = _save(backend, tmp_path, metadata)
    snapshot = backend.load_latest(tmp_path)

    assert snapshot["session_id"] == "checkpoint"
    assert snapshot["cwd"] == str(tmp_path.resolve())
    assert snapshot["usage"] == {"input_tokens": 2, "output_tokens": 3}
    assert snapshot["summary"] == "Resume the map" and snapshot["message_count"] == 1
    saved = snapshot["tool_metadata"]
    assert saved["permission_mode"] == "default"
    assert saved["read_file_state"] == {"file": {"line": 2}}
    assert saved["invoked_skills"] == ["cartography"]
    assert not {"data_catalog", "api_key", "geoharness_config"} & saved.keys()
    assert saved[GEO_SESSION_CHECKPOINT_KEY]["version"] == GEO_SESSION_CHECKPOINT_VERSION
    assert backend.load_by_id(tmp_path, "checkpoint") == snapshot
    assert backend.list_snapshots(tmp_path)[0]["summary"] == "Resume the map"
    exported = backend.export_markdown(
        cwd=tmp_path, messages=[ConversationMessage.from_user_text("Editable transcript")],
    )
    assert "Editable transcript" in exported.read_text(encoding="utf-8")
    assert path.is_relative_to(tmp_path / ".geoharness")
    other = tmp_path / "other-workspace"
    other.mkdir()
    assert backend.load_latest(other) is None
    assert backend.get_session_dir(other) != path.parent
    assert not list(tmp_path.rglob(".openharness"))


def test_backend_storage_ignores_upstream_environment_without_launcher(
    backend, tmp_path, monkeypatch,
):
    monkeypatch.delenv("OPENHARNESS_CONFIG_DIR")
    monkeypatch.delenv("OPENHARNESS_DATA_DIR")
    path = _save(backend, tmp_path, _metadata())
    assert path.is_relative_to(tmp_path / ".geoharness")
    assert backend.load_by_id(tmp_path, "checkpoint")["summary"] == "Resume the map"
    assert backend.list_snapshots(tmp_path)[0]["session_id"] == "checkpoint"
    backend.export_markdown(cwd=tmp_path, messages=[])
    monkeypatch.setenv("OPENHARNESS_CONFIG_DIR", str(tmp_path / "upstream"))
    monkeypatch.setenv("OPENHARNESS_DATA_DIR", str(tmp_path / "upstream" / "data"))
    assert backend.load_latest(tmp_path)["summary"] == "Resume the map"
    assert not (tmp_path / "upstream").exists() and not (tmp_path / ".openharness").exists()


def test_snapshot_sanitizes_orphan_tool_use_like_upstream(backend, tmp_path):
    messages = [ConversationMessage.from_user_text("Analyze"), ConversationMessage(
        role="assistant", content=[ToolUseBlock(id="orphan", name="geo_python", input={})],
    )]
    backend.save_snapshot(cwd=tmp_path, model="offline", system_prompt="test", messages=messages,
                          usage=UsageSnapshot(), tool_metadata=_metadata())
    snapshot = backend.load_latest(tmp_path)
    assert snapshot["message_count"] == 1 and snapshot["messages"][0]["role"] == "user"


def test_checkpoint_is_present_in_each_atomic_write_before_latest_is_published(
    backend, tmp_path, monkeypatch,
):
    from geoharness import session_state

    writes = []
    actual_write = session_state.atomic_write_text

    def capture(path, text):
        payload = json.loads(text)
        assert GEO_SESSION_CHECKPOINT_KEY in payload["tool_metadata"]
        writes.append((Path(path).name, payload))
        actual_write(path, text)

    monkeypatch.setattr(session_state, "atomic_write_text", capture)
    monkeypatch.setattr(session_storage, "save_session_snapshot",
                        lambda **kwargs: pytest.fail("Do not publish an incomplete normal save"))
    _save(backend, tmp_path, _metadata())
    assert [name for name, _ in writes] == ["session-checkpoint.json", "latest.json"]
    assert writes[0][1] == writes[1][1]


def test_failed_named_write_does_not_replace_latest(backend, tmp_path, monkeypatch):
    from geoharness import session_state

    path = _save(backend, tmp_path, _metadata(), session_id="before")
    previous = path.read_bytes()
    monkeypatch.setattr(session_state, "atomic_write_text",
                        lambda *args: (_ for _ in ()).throw(OSError("offline write failure")))
    with pytest.raises(OSError, match="write failure"):
        _save(backend, tmp_path, _metadata(), session_id="after")
    assert path.read_bytes() == previous


def test_alias_identity_counts_seconds_and_current_lower_limits_survive(backend, tmp_path):
    config = GeoConfig(map_review=MapReviewConfig(max_repairs=4))
    metadata = _metadata(config)
    begin_map_request(metadata, cwd=tmp_path, config=config)
    job = ensure_map_job(metadata, cwd=tmp_path, config=config, map_id="risk")
    assert reserve_map_render("risk", metadata, config) is None
    assert reserve_map_render("risk-v2", metadata, config) is None
    state = metadata["map_workflow_state"]
    state.update(reviews=4, seconds=57.25, reserved_seconds=90.0)
    state["maps"][job]["reviews"] = 3
    metadata["map_job_state"]["jobs_dir"] = "untrusted/old/location"
    smaller = replace(config, map_review=MapReviewConfig(max_repairs=0, total_timeout_seconds=1))
    restored = _restore(backend, tmp_path, metadata, config=smaller)

    workflow = restored["map_workflow_state"]
    maps = workflow["maps"]
    assert maps[job] is maps["risk"] is maps["risk-v2"]
    assert maps[job] == {"renders": 2, "reviews": 3}
    assert workflow["reviews"] == 4 and workflow["seconds"] == 57.25
    assert workflow["reserved_seconds"] == 0
    assert workflow["reserved_seconds_reset_reason"] == (
        "in_flight_reservations_do_not_survive_restart"
    )
    assert restored["map_job_state"]["budget_job_id"] == job
    assert restored["map_job_state"]["request_id"] == metadata["map_job_state"]["request_id"]
    assert restored["map_job_state"]["jobs_dir"] == str(tmp_path / config.cartography.jobs_dir)
    assert reserve_map_render("risk-v3", restored, smaller) is not None
    assert maps[job]["renders"] == 2  # A failed reservation must not double charge old aliases.


def test_equal_distinct_entries_are_not_accidentally_deduplicated(backend, tmp_path, monkeypatch):
    from geoharness.cartography import workflow

    metadata = _metadata()
    job = str(uuid4())
    metadata["map_job_state"] = {"cwd": str(tmp_path), "aliases": {"a": job, "b": job}}
    metadata["map_workflow_state"]["maps"] = {
        "a": {"renders": 1, "reviews": 1}, "b": {"renders": 1, "reviews": 1},
    }
    restored = _restore(backend, tmp_path, metadata)
    maps = restored["map_workflow_state"]["maps"]
    assert maps["a"] is not maps["b"]
    monkeypatch.setattr(workflow, "canonical_budget_map", lambda *args: job)
    assert reserve_map_render("a", restored, GeoConfig()) is None
    assert maps["a"] is maps["b"] is maps[job]
    assert maps[job] == {"renders": 3, "reviews": 2}
    assert reserve_map_render("b", restored, GeoConfig()) is None
    assert maps[job] == {"renders": 4, "reviews": 2}


def test_oversized_used_budget_fails_save_without_erasing_latest(backend, tmp_path):
    path = _save(backend, tmp_path, _metadata(), session_id="valid")
    previous = path.read_bytes()
    metadata = _metadata()
    metadata["map_workflow_state"]["maps"] = {
        f"map-{number}": {"renders": 1, "reviews": 0}
        for number in range(MAX_BUDGET_ENTRIES + 1)
    }
    with pytest.raises(ValueError, match="entry limit"):
        _save(backend, tmp_path, metadata, session_id="too-large")
    assert path.read_bytes() == previous


def test_profiles_keep_measurement_signatures_and_invalidate_after_file_change(backend, tmp_path):
    path = tmp_path / "source.geojson"
    path.write_text("original", encoding="utf-8")
    metadata = _metadata()
    record_profiles(metadata, [make_profile({
        "path": str(path), "name": "Measured source", "crs": "EPSG:32647",
        "bbox": [0, 0, 1, 1], "schema": {"value": "float64"},
        "_measured_file_stat": file_signature(path),
    }, kind="vector")])
    _save(backend, tmp_path, metadata)
    saved_metadata = backend.load_latest(tmp_path)["tool_metadata"]
    restored = _metadata()
    assert restore_geo_session_state(restored, saved_metadata, cwd=tmp_path,
                                     config=restored["geoharness_config"])
    profile = next(iter(current_profiles(restored).values()))
    assert profile["freshness"] == "current" and profile["crs"] == "EPSG:32647"
    assert profile["file_stat"] == file_signature(path)
    path.write_text("changed and longer", encoding="utf-8")
    assert current_profiles(restored) == {}
    after = _metadata()
    assert restore_geo_session_state(after, saved_metadata, cwd=tmp_path,
                                     config=after["geoharness_config"])
    assert current_profiles(after) == {}


def test_unsigned_profiles_cannot_claim_current_after_restore(backend, tmp_path):
    path = tmp_path / "unsigned.geojson"
    path.write_text("unsigned", encoding="utf-8")
    metadata = _metadata()
    metadata["geo_data_context"] = {"profiles": {str(path): {
        "kind": "vector", "path": str(path), "freshness": "current",
        "file_stat": None, "crs": "EPSG:4326", "config": {"api_key": "ignored"},
    }}}
    restored = _restore(backend, tmp_path, metadata)
    profile = next(iter(current_profiles(restored).values()))
    assert profile["freshness"] == "unverified" and "config" not in profile


async def test_restored_failure_cache_rechecks_original_image_and_current_config(
    backend, tmp_path, monkeypatch,
):
    from geoharness.cartography import workflow

    image = tmp_path / "preview.png"
    image.write_bytes(b"first image")
    calls = []

    async def fail_review(path, **kwargs):
        calls.append(kwargs["config"].map_review.max_tokens)
        return MapReview(status="unverified", summary="Offline provider failure",
                         diagnostics={"failure_stage": "provider", "request_sent": True})

    monkeypatch.setattr(workflow, "review_map", fail_review)
    metadata = _metadata()
    await review_with_budget(image, config=metadata["geoharness_config"], task="Check",
                             evidence={}, metadata=metadata)
    restored = _restore(backend, tmp_path, metadata)
    config = restored["geoharness_config"]
    review, flags = await review_with_budget(image, config=config, task="Check", evidence={},
                                            metadata=restored)
    assert review.status == "unverified" and flags["review_cached"] and len(calls) == 1
    changed = replace(config, map_review=config.map_review.model_copy(update={"max_tokens": 9000}))
    _, flags = await review_with_budget(image, config=changed, task="Check", evidence={},
                                       metadata=restored)
    assert not flags["review_cached"] and len(calls) == 2
    image.write_bytes(b"a different image")
    _, flags = await review_with_budget(image, config=changed, task="Check", evidence={},
                                       metadata=restored)
    assert not flags["review_cached"] and len(calls) == 3


async def test_restored_success_is_unverified_until_fresh_review(backend, tmp_path, monkeypatch):
    from geoharness.cartography import workflow

    image = tmp_path / "preview.png"
    image.write_bytes(b"image")
    calls = []

    async def passed_review(*args, **kwargs):
        calls.append(True)
        return MapReview(status="passed", summary="Current reviewer passed")

    monkeypatch.setattr(workflow, "review_map", passed_review)
    metadata = _metadata()
    await review_with_budget(image, config=metadata["geoharness_config"], task="Check",
                             evidence={}, metadata=metadata)
    restored = _restore(backend, tmp_path, metadata)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    cache = restored["map_workflow_state"]["cache"][digest]
    assert cache["status"] == "unverified"
    assert cache["diagnostics"]["checkpoint_previous_status"] == "passed"
    assert digest not in restored["map_workflow_state"]["cache_settings"]
    review, flags = await review_with_budget(image, config=restored["geoharness_config"],
                                            task="Check", evidence={}, metadata=restored)
    assert review.status == "passed" and not flags["review_cached"] and len(calls) == 2


async def test_validation_retry_exhaustion_is_not_replenished(backend, tmp_path, monkeypatch):
    from geoharness.cartography import workflow

    image = tmp_path / "preview.png"
    image.write_bytes(b"image")
    calls = []

    async def invalid_review(*args, **kwargs):
        calls.append(True)
        return MapReview(status="unverified", summary="Invalid JSON",
                         diagnostics={"failure_stage": "json", "request_sent": True})

    monkeypatch.setattr(workflow, "review_map", invalid_review)
    metadata = _metadata()
    for _ in range(2):
        await review_with_budget(image, config=metadata["geoharness_config"], task="Check",
                                 evidence={}, metadata=metadata, retry_invalid_response=True)
    restored = _restore(backend, tmp_path, metadata)
    _, flags = await review_with_budget(image, config=restored["geoharness_config"], task="Check",
                                       evidence={}, metadata=restored, retry_invalid_response=True)
    assert flags["review_cached"] and flags["invalid_response_retry_exhausted"]
    assert len(calls) == 2 and restored["map_workflow_state"]["reviews"] == 2


async def test_deterministic_failures_invalidate_when_input_or_configuration_changes(
    backend, tmp_path,
):
    class Arguments(BaseModel):
        file_path: str

    class FailingTool:
        name = "offline_failure"
        calls = 0

        @guard_map_errors
        async def execute(self, arguments, context):
            self.calls += 1
            return ToolResult(output="Invalid presentation parameter", is_error=True,
                              metadata={"deterministic_error": True, "render_executed": False})

    source = tmp_path / "data.geojson"
    source.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    arguments = Arguments(file_path=str(source))
    tool = FailingTool()
    metadata = _metadata()
    context = ToolExecutionContext(cwd=tmp_path, metadata=metadata)
    assert (await tool.execute(arguments, context)).is_error
    assert metadata["map_deterministic_errors"]
    restored = _restore(backend, tmp_path, metadata)
    context = ToolExecutionContext(cwd=tmp_path, metadata=restored)
    result = await tool.execute(arguments, context)
    assert result.metadata["duplicate_error_blocked"] and tool.calls == 1
    source.write_text('{"changed":true}', encoding="utf-8")
    await tool.execute(arguments, context)
    assert tool.calls == 2
    config = restored["geoharness_config"]
    restored["geoharness_config"] = replace(
        config, cartography=replace(config.cartography, outputs_dir="changed-output"),
    )
    await tool.execute(arguments, context)
    assert tool.calls == 3


def test_checkpoint_redacts_known_secrets_and_drops_live_objects(backend, tmp_path):
    secret = "session-checkpoint-test-secret"
    metadata = _metadata(replace(GeoConfig(), api_key=secret))
    digest = "a" * 64
    review = MapReview(status="unverified", summary=f"Safe failure {secret}", diagnostics={
        "error_excerpt": secret, "raw_response": secret, "client": object(), "nan": float("nan"),
        "api_key": secret,
    }).model_dump()
    metadata["map_workflow_state"]["cache"] = {digest: review}
    metadata["map_deterministic_errors"] = {digest: {
        "output": f"Known failure {secret}",
        "metadata": {"deterministic_error": True, "client": object(), "config": metadata,
                     "password": secret, "safe": True},
    }}
    metadata["data_catalog"] = object()
    path = _save(backend, tmp_path, metadata)
    text = path.read_text(encoding="utf-8")
    assert secret not in text and "[redacted]" in text
    checkpoint = json.loads(text)["tool_metadata"][GEO_SESSION_CHECKPOINT_KEY]
    assert not {"api_key", "raw_response", "client", "nan"} & (
        checkpoint["map_workflow_state"]["cache"][digest]["diagnostics"].keys()
    )
    assert checkpoint["map_deterministic_errors"][digest]["metadata"] == {
        "deterministic_error": True, "safe": True,
    }
    json.dumps(checkpoint, allow_nan=False)


def test_request_failures_keep_bounded_typed_observations_without_messages(backend, tmp_path):
    secret = "safe-observation-test-secret"
    metadata = _metadata(replace(GeoConfig(), api_key=secret))
    records = [{"category": "provider_sse", "request_id": str(number), "model": "offline",
                "status_code": None, "exception_type": "RequestFailure",
                "exception_chain": ["RequestFailure", "APIError"], "raw_chunk_count": 2,
                "attempt_raw_chunk_count": 1, "raw_chunk_source": "sse", "visible_chars": 0,
                "provider_attempts": 2, "last_packet_at": 1234.5, "started_at": 1230.0,
                "end_at": 1236.0, "elapsed_seconds": 6.0, "prompt_too_long": False,
                "completion_token_limit_error": True, "completion_token_limit": 4096,
                "body": secret,
                "exception_message": f"private provider content {secret}", "api_key": secret,
                "client": object()} for number in range(MAX_REQUEST_FAILURES + 4)]
    metadata["geo_request_failures"] = records
    restored = _restore(backend, tmp_path, metadata)
    failures = restored["geo_request_failures"]
    assert len(failures) == MAX_REQUEST_FAILURES and failures[0]["request_id"] == "4"
    assert failures[-1] == {key: value for key, value in records[-1].items()
                            if key not in {"body", "exception_message", "api_key", "client"}}
    assert secret not in json.dumps(backend.load_latest(tmp_path))


def test_request_failures_reject_invalid_types_and_unknown_categories(backend, tmp_path):
    metadata = _metadata()
    metadata["geo_request_failures"] = [{
        "category": "http", "model": {"raw_body": "private"}, "request_id": "x" * 257,
        "status_code": True, "raw_chunk_count": -1, "attempt_raw_chunk_count": False,
        "exception_type": "an exception message", "exception_chain": ["APIError", "private text"],
        "raw_chunk_source": "private provider text", "last_packet_at": float("nan"),
        "elapsed_seconds": -1, "provider_attempts": object(), "visible_chars": 1,
        "raw_response": "private text",
    }, {"category": "private body", "visible_chars": 1}]
    restored = _restore(backend, tmp_path, metadata)
    assert restored["geo_request_failures"] == [{
        "category": "http", "exception_chain": ["APIError"], "visible_chars": 1,
    }]


@pytest.mark.parametrize("invalid", ["version", "bool-version", "cwd", "reference", "counter",
                                      "seconds", "oversize", "live-object"])
def test_invalid_checkpoints_do_not_overwrite_fresh_runtime(backend, tmp_path, invalid):
    _save(backend, tmp_path, _metadata())
    saved = copy.deepcopy(backend.load_latest(tmp_path)["tool_metadata"])
    checkpoint = saved[GEO_SESSION_CHECKPOINT_KEY]
    if invalid == "version":
        checkpoint["version"] = 99
    elif invalid == "bool-version":
        checkpoint["version"] = True
    elif invalid == "cwd":
        checkpoint["cwd"] = str(tmp_path / "other")
    elif invalid == "reference":
        checkpoint["map_workflow_state"]["maps"]["aliases"]["risk"] = "missing"
    elif invalid == "counter":
        checkpoint["map_workflow_state"]["reviews"] = True
    elif invalid == "seconds":
        checkpoint["map_workflow_state"]["seconds"] = float("nan")
    elif invalid == "oversize":
        checkpoint["unknown"] = "x" * (MAX_CHECKPOINT_BYTES + 1)
    else:
        checkpoint["unknown"] = object()
    saved["geoharness_config"] = {"api_key": "untrusted"}
    saved["data_catalog"] = object()
    saved["task_focus_state"] = {"goal": "Keep the prior goal", "api_key": "untrusted"}
    fresh = _metadata()
    live_catalog = fresh["data_catalog"] = object()
    original_workflow = fresh["map_workflow_state"]
    assert not restore_geo_session_state(fresh, saved, cwd=tmp_path,
                                         config=fresh["geoharness_config"])
    assert fresh["map_workflow_state"] is original_workflow
    assert fresh["data_catalog"] is live_catalog
    assert isinstance(fresh["geoharness_config"], GeoConfig)
    assert fresh["task_focus_state"] == {"goal": "Keep the prior goal"}


def test_legacy_snapshot_remains_usable_without_checkpoint(backend, tmp_path):
    upstream = session_storage.save_session_snapshot(
        cwd=tmp_path, model="old", system_prompt="legacy",
        messages=[ConversationMessage.from_user_text("Legacy goal")], usage=UsageSnapshot(),
        tool_metadata={"task_focus_state": {"goal": "Legacy goal"}},
    )
    assert upstream == backend.get_session_dir(tmp_path) / "latest.json"
    fresh = _metadata()
    assert not restore_geo_session_state(fresh, backend.load_latest(tmp_path)["tool_metadata"],
                                         cwd=tmp_path, config=fresh["geoharness_config"])
    assert fresh["task_focus_state"] == {"goal": "Legacy goal"}
    assert fresh["map_workflow_state"] == {}


def test_query_recovery_mode_restores_only_with_current_matching_policy(backend, tmp_path):
    config = replace(GeoConfig(), model="primary(max)", agent=AgentConfig(
        empty_response_fallback_model="configured-fallback", empty_response_max_tokens=4096,
    ))
    metadata = _metadata(config)
    metadata["geo_query_recovery"] = {
        "fallback_active": True, "model": "configured-fallback", "max_tokens": 4096,
        "source_model": "primary(max)",
        "config_fingerprint": hashlib.sha256(config.agent.model_dump_json().encode()).hexdigest(),
        "client_id": 123, "extra_body": {"secret": "must not persist"},
    }
    restored = _restore(backend, tmp_path, metadata)
    mode = restored["geo_query_recovery"]
    assert mode == {key: value for key, value in metadata["geo_query_recovery"].items()
                    if key not in {"client_id", "extra_body"}}
    changed = replace(config, agent=config.agent.model_copy(update={
        "empty_response_max_tokens": 2048,
    }))
    assert _restore(backend, tmp_path, metadata, config=changed)["geo_query_recovery"] == {}
    saved = backend.load_latest(tmp_path)["tool_metadata"]
    saved[GEO_SESSION_CHECKPOINT_KEY]["geo_query_recovery"]["max_tokens"] = 65536
    fresh = _metadata(config)
    assert restore_geo_session_state(fresh, saved, cwd=tmp_path, config=config)
    assert fresh["geo_query_recovery"] == {}
