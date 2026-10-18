"""Bounded GeoHarness checkpoints within compatible OpenHarness session snapshots.

Only measured descriptions, request budgets and safe failure/review records are
checkpointed. Config, clients, catalogs and synchronization objects are rebuilt
by the runtime. A restored review is history, never new evidence of visual success.
"""

from __future__ import annotations

import json
import math
import re
import time
from hashlib import sha1, sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage, sanitize_conversation_messages
from openharness.services import session_storage
from openharness.services.session_backend import OpenHarnessSessionBackend
from openharness.utils.fs import atomic_write_text
from pydantic import ValidationError

from geoharness.cartography.quality import MapReview, _review_secrets
from geoharness.config.settings import GeoConfig, get_config_dir
from geoharness.data.profiles import MAX_PROFILES, MAX_SCHEMA_FIELDS, make_profile, profile_key

GEO_SESSION_CHECKPOINT_KEY = "geo_session_checkpoint"
GEO_SESSION_CHECKPOINT_VERSION = 1
# Cache limits match the largest supported request review count. Budget entries
# must never be silently truncated: an oversized budget fails the save instead.
MAX_BUDGET_ENTRIES = 16
MAX_BUDGET_ALIASES = 128
MAX_REVIEW_CACHE = 20
MAX_ERROR_CACHE = 64
MAX_REQUEST_FAILURES = 16
MAX_CHECKPOINT_BYTES = 1024 * 1024
_MAX_TEXT = 8192
_MAX_PATH = 4096
_MAX_COUNTER = 2**63 - 1
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_PRIVATE_KEY = re.compile(
    r"(?:api[_-]?key|password|credential|secret|connection_string|dsn|authorization|"
    r"cookie|private_key|access_token|refresh_token)|^(?:token|config|code|source_text|"
    r"script|gdf|geodataframe|raw_response)$", re.I,
)
_SKIP = object()
_RESERVATION_REASON = "in_flight_reservations_do_not_survive_restart"


def _text(value: Any, limit: int = _MAX_TEXT) -> str | None:
    return value if isinstance(value, str) and len(value) <= limit else None


def _counter(value: Any) -> int | None:
    if type(value) is int and 0 <= value <= _MAX_COUNTER:
        return value
    return None


def _seconds(value: Any) -> float | None:
    if type(value) not in (int, float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and 0 <= number <= 1e12 else None


def _finite(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return value if str(UUID(value)) == value else None
    except ValueError:
        return None


def _digest(value: Any) -> bool:
    return isinstance(value, str) and _HEX.fullmatch(value) is not None


def _safe_json(value: Any, secrets: tuple[str, ...], depth: int = 0) -> Any:
    """Copy JSON values only; never call str/model_dump on runtime objects."""
    if depth > 8:
        return _SKIP
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return value if abs(value) <= _MAX_COUNTER else _SKIP
    if type(value) is float:
        return value if math.isfinite(value) else _SKIP
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[redacted]")
        return value[:_MAX_TEXT]
    if isinstance(value, dict):
        result = {}
        for key, item in list(value.items())[:64]:
            if _text(key, 256) is None or _PRIVATE_KEY.search(key):
                continue
            if any(secret in key for secret in secrets):
                continue
            safe = _safe_json(item, secrets, depth + 1)
            if safe is not _SKIP:
                result[key] = safe
        return result
    if isinstance(value, list):
        result = [_safe_json(item, secrets, depth + 1) for item in value[:64]]
        return [item for item in result if item is not _SKIP]
    return _SKIP


def _budget_maps(raw: Any, *, encoded: bool) -> dict[str, Any]:
    """Encode identity relationships, or reconstruct shared budget dictionaries."""
    if not isinstance(raw, dict):
        raise ValueError("Invalid map budget checkpoint")
    if encoded:
        entries, aliases = raw.get("entries"), raw.get("aliases")
        if not isinstance(entries, dict) or not isinstance(aliases, dict):
            raise ValueError("Invalid map budget references")
        if len(entries) > MAX_BUDGET_ENTRIES or len(aliases) > MAX_BUDGET_ALIASES:
            raise ValueError("Map budget checkpoint exceeds its entry/alias limit")
        validated = {}
        for key, value in entries.items():
            if _text(key, 32) is None or not isinstance(value, dict):
                raise ValueError("Invalid map budget entry")
            renders, reviews = _counter(value.get("renders")), _counter(value.get("reviews"))
            if renders is None or reviews is None:
                raise ValueError("Invalid used map budget")
            validated[key] = {"renders": renders, "reviews": reviews}
        result = {}
        for alias, key in aliases.items():
            if not alias or _text(alias, _MAX_PATH) is None or not isinstance(key, str):
                raise ValueError("Invalid map budget alias")
            if key not in validated:
                raise ValueError("Unknown map budget reference")
            result[alias] = validated[key]
        if set(aliases.values()) != set(entries):
            raise ValueError("Unreferenced map budget entry")
        return result
    if len(raw) > MAX_BUDGET_ALIASES:
        raise ValueError("Map budget checkpoint exceeds its alias limit")
    entries, aliases, identities = {}, {}, {}
    for alias, value in raw.items():
        if not alias or _text(alias, _MAX_PATH) is None or not isinstance(value, dict):
            raise ValueError("Invalid map budget entry")
        renders, reviews = _counter(value.get("renders", 0)), _counter(value.get("reviews", 0))
        if renders is None or reviews is None:
            raise ValueError("Invalid used map budget")
        identity = id(value)
        if identity not in identities:
            if len(entries) >= MAX_BUDGET_ENTRIES:
                raise ValueError("Map budget checkpoint exceeds its entry limit")
            key = f"budget-{len(entries)}"
            identities[identity] = key
            entries[key] = {"renders": renders, "reviews": reviews}
        aliases[alias] = identities[identity]
    return {"entries": entries, "aliases": aliases}


def _review_cache(raw: Any, secrets: tuple[str, ...], *, restoring: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for digest, value in list(raw.items())[-MAX_REVIEW_CACHE:]:
        if not _digest(digest) or not isinstance(value, dict):
            continue
        safe = _safe_json(value, secrets)
        try:
            review = MapReview.model_validate(safe)
        except ValidationError:
            continue
        if restoring and review.status == "passed":
            # Delivery reads cache by digest directly, without cache_settings.
            # Keeping passed here would certify forged or obsolete metadata.
            review.status = "unverified"
            review.diagnostics.update(
                checkpoint_previous_status="passed",
                checkpoint_requires_visual_review=True,
            )
        result[digest] = review.model_dump(mode="json")
    return result


def _workflow(raw: Any, secrets: tuple[str, ...], *, restoring: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Invalid map workflow checkpoint")
    reviews, seconds = _counter(raw.get("reviews", 0)), _seconds(raw.get("seconds", 0.0))
    if reviews is None or seconds is None:
        raise ValueError("Invalid used review budget")
    maps = _budget_maps(raw.get("maps", {}), encoded=restoring)
    cache = _review_cache(raw.get("cache"), secrets, restoring=restoring)
    settings = raw.get("cache_settings")
    settings = settings if isinstance(settings, dict) else {}
    fingerprints = {
        digest: fingerprint for digest, fingerprint in list(settings.items())[-MAX_REVIEW_CACHE:]
        if digest in cache and _digest(fingerprint)
        and not cache[digest]["diagnostics"].get("checkpoint_requires_visual_review")
    }
    retries = raw.get("validation_retries")
    retries = retries if isinstance(retries, dict) else {}
    validated_retries = {}
    for key, value in list(retries.items())[-MAX_REVIEW_CACHE:]:
        if not isinstance(key, str) or len(key) != 129 or value is not True:
            continue
        digest, separator, fingerprint = key.partition(":")
        if separator and _digest(digest) and _digest(fingerprint):
            validated_retries[key] = True
    attempts = raw.get("repair_attempts")
    attempts = attempts if isinstance(attempts, dict) else {}
    validated_attempts = {key: True for key, value in list(attempts.items())[-MAX_REVIEW_CACHE:]
                          if _digest(key) and value is True}
    services = raw.get("service_recovery")
    services = services if isinstance(services, dict) else {}
    validated_services = {}
    for key, value in list(services.items())[-MAX_REVIEW_CACHE:]:
        if not _digest(key) or not isinstance(value, dict):
            continue
        retry_at = _seconds(value.get("retry_at"))
        failure = value.get("failure")
        if retry_at is None or not isinstance(failure, dict):
            continue
        diagnostic = _safe_json({field: failure[field] for field in {
            "failure_stage", "error_type", "http_status", "service_failure_transient",
            "service_error_code", "retry_after_seconds", "service_retry_at",
        } if field in failure}, secrets)
        validated_services[key] = {"retry_at": retry_at, "failure": diagnostic}
    activity = raw.get("review_activity")
    activity = activity if isinstance(activity, dict) else {}
    validated_activity = {}
    for key, value in list(activity.items())[-MAX_REVIEW_CACHE:]:
        if not _digest(key) or not isinstance(value, dict):
            continue
        calls, hits = _counter(value.get("provider_calls")), _counter(value.get("cache_hits"))
        if calls is not None and hits is not None:
            validated_activity[key] = {"provider_calls": calls, "cache_hits": hits}
    return {
        "reviews": reviews, "seconds": seconds,
        # Reservation is an in-flight promise, not measured consumption. After
        # cancellation/restart no task owns it; retaining it would waste budget.
        "reserved_seconds": 0.0,
        "reserved_seconds_reset_reason": _RESERVATION_REASON,
        "maps": maps, "cache": cache, "cache_settings": fingerprints,
        "validation_retries": validated_retries,
        "service_recovery": validated_services,
        "review_activity": validated_activity,
        "provider_calls_total": _counter(raw.get("provider_calls_total")) or 0,
        "cache_hits_total": _counter(raw.get("cache_hits_total")) or 0,
        "repair_attempts": validated_attempts,
    }


def _job_state(raw: Any, cwd: Path, config: GeoConfig) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    saved_cwd = raw.get("cwd")
    if saved_cwd is not None and _workspace(saved_cwd) != cwd:
        return {}
    configured = Path(config.cartography.jobs_dir).expanduser()
    directory = (configured if configured.is_absolute() else cwd / configured).resolve()
    if directory == cwd or not directory.is_relative_to(cwd):
        raise ValueError("cartography.jobs_dir must be a subdirectory of the workspace")
    result: dict[str, Any] = {"cwd": str(cwd), "jobs_dir": str(directory)}
    for field in ("active_job_id", "budget_job_id", "request_id"):
        if (value := _uuid(raw.get(field))) is not None:
            result[field] = value
    aliases = raw.get("aliases")
    result["aliases"] = {
        alias: identity for alias, identity in list(aliases.items())[-MAX_BUDGET_ALIASES:]
        if alias and _text(alias, _MAX_PATH) is not None and _uuid(identity) is not None
    } if isinstance(aliases, dict) else {}
    return result


def _workspace(value: Any) -> Path | None:
    if not value or _text(value, _MAX_PATH) is None:
        return None
    if "://" in value or "|" in value or value.startswith(("\\\\", "//")):
        return None
    try:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else None
    except (OSError, ValueError, RuntimeError):
        return None


def _signature(value: Any) -> dict | None:
    if not isinstance(value, dict) or not value or len(value) > 5:
        return None
    result = {}
    for name, details in value.items():
        if not name or _text(name, 256) is None or Path(name).name != name:
            return None
        if details is None:
            result[name] = None
            continue
        if not isinstance(details, dict):
            return None
        record = {key: _counter(details.get(key)) for key in ("size", "mtime_ns", "ctime_ns")}
        if any(item is None for item in record.values()):
            return None
        result[name] = record
    return result


def _profiles(raw: Any, secrets: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for value in list(raw.values())[-MAX_PROFILES:]:
        if not isinstance(value, dict) or not isinstance(value.get("kind"), str) or value[
            "kind"
        ] not in {"vector", "raster"}:
            continue
        path = _workspace(value.get("path"))
        if path is None or any(secret in str(path) for secret in secrets):
            continue
        detail: dict[str, Any] = {"path": str(path)}
        measured = value.get("file_stat")
        if measured is not None:
            signature = _signature(measured)
            if signature is None:
                continue
            detail["_measured_file_stat"] = signature
        for field in ("name", "crs", "geometry_type", "dtype", "nodata"):
            item = value.get(field)
            if isinstance(item, str):
                detail[field] = _safe_json(item[:256], secrets)
            elif item is None or _finite(item):
                detail[field] = item
        for field in ("feature_count", "width", "height", "band_count"):
            if (item := _counter(value.get(field))) is not None:
                detail[field] = item
        for field, length in (("bbox", 4), ("resolution", 2)):
            item = value.get(field)
            if isinstance(item, list) and len(item) == length and all(
                _finite(number) for number in item
            ):
                detail[field] = item
        schema = value.get("schema")
        if isinstance(schema, dict):
            detail["schema"] = {key: item for key, item in list(schema.items())[:MAX_SCHEMA_FIELDS]
                                if _text(key, 256) is not None and _text(item, 256) is not None}
        columns = value.get("columns")
        if isinstance(columns, list):
            detail["columns"] = [item for item in columns[:MAX_SCHEMA_FIELDS]
                                 if _text(item, 256) is not None]
        profile = make_profile(detail, kind=value["kind"])
        if profile is None:
            continue
        for field in ("schema_omitted", "columns_omitted"):
            if (item := _counter(value.get(field))) is not None:
                profile[field] = item
        result[profile_key(path)] = profile
    return result


def _errors(raw: Any, secrets: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for key, value in list(raw.items())[-MAX_ERROR_CACHE:]:
        if not _digest(key) or not isinstance(value, dict):
            continue
        output, metadata = value.get("output"), value.get("metadata")
        if not isinstance(output, str) or not isinstance(metadata, dict):
            continue
        if metadata.get("deterministic_error") is not True:
            continue
        result[key] = {"output": _safe_json(output, secrets),
                       "metadata": _safe_json(metadata, secrets)}
    return result


def _focus(raw: Any, secrets: tuple[str, ...]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    result: dict[str, Any] = {}
    for field in ("goal", "next_step"):
        if isinstance(raw.get(field), str):
            result[field] = _safe_json(raw[field], secrets)
    for field in ("recent_goals", "active_artifacts", "verified_state"):
        if isinstance(raw.get(field), list):
            result[field] = [_safe_json(item, secrets) for item in raw[field][-32:]
                             if isinstance(item, str)]
    return result


def _request_failures(raw: Any, secrets: tuple[str, ...]) -> list[dict[str, Any]]:
    """Keep transport observations only, never exception messages/provider bodies."""
    if not isinstance(raw, list):
        return []
    result = []
    for value in raw[-MAX_REQUEST_FAILURES:]:
        if not isinstance(value, dict) or not isinstance(value.get("category"), str) or value[
            "category"
        ] not in {
            "http", "transport", "parser", "provider_sse",
        }:
            continue
        record: dict[str, Any] = {"category": value["category"]}
        for field in ("request_id", "model"):
            item = _text(value.get(field), 256)
            if item is not None:
                record[field] = _safe_json(item, secrets)
        status = value.get("status_code")
        if status is None and "status_code" in value:
            record["status_code"] = None
        elif type(status) is int and 100 <= status <= 599:
            record["status_code"] = status
        for field in ("prompt_too_long", "completion_token_limit_error"):
            if type(value.get(field)) is bool:
                record[field] = value[field]
        cap = _counter(value.get("completion_token_limit"))
        if cap is not None and cap > 0:
            record["completion_token_limit"] = cap
        identifier = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
        exception = value.get("exception_type")
        if isinstance(exception, str) and identifier.fullmatch(exception):
            record["exception_type"] = exception
        chain = value.get("exception_chain")
        if isinstance(chain, list):
            record["exception_chain"] = [item for item in chain[:8]
                                         if isinstance(item, str) and identifier.fullmatch(item)]
        for field in ("raw_chunk_count", "attempt_raw_chunk_count", "provider_attempts",
                      "visible_chars"):
            if (item := _counter(value.get(field))) is not None:
                record[field] = item
        for field in ("source", "raw_chunk_source"):
            item = value.get(field)
            if isinstance(item, str) and item in {"sse", "sdk_chunk"} or (
                item is None and field in value
            ):
                record[field] = item
        for field in ("last_packet_at", "started_at", "end_at", "elapsed_seconds"):
            if value.get(field) is None and field in value:
                record[field] = None
            elif (item := _seconds(value.get(field))) is not None:
                record[field] = item
        result.append(record)
    return result


def _query_mode(raw: Any, config: GeoConfig) -> dict[str, Any]:
    """Restore a request mode only with the same explicitly configured policy."""
    if not isinstance(raw, dict) or raw.get("fallback_active") is not True or (
        not config.agent.empty_response_retries
    ):
        return {}
    fingerprint = sha256(config.agent.model_dump_json().encode()).hexdigest()
    fallback = config.agent.empty_response_fallback_model.strip() or config.model
    cap = _counter(raw.get("max_tokens"))
    allowed = min(config.agent.max_tokens,
                  config.agent.empty_response_max_tokens or config.agent.max_tokens)
    if raw.get("config_fingerprint") != fingerprint or raw.get("model") != fallback or (
        raw.get("source_model") != config.model or cap is None or not 0 < cap <= allowed
    ):
        return {}
    return {"fallback_active": True, "model": fallback, "max_tokens": cap,
            "source_model": config.model, "config_fingerprint": fingerprint}


def _checkpoint(metadata: dict[str, Any], cwd: Path, config: GeoConfig) -> dict[str, Any]:
    secrets = _review_secrets(config)
    data = metadata.get("geo_data_context")
    data = data if isinstance(data, dict) else {}
    result = {
        "version": GEO_SESSION_CHECKPOINT_VERSION, "cwd": str(cwd),
        "map_workflow_state": _workflow(metadata.get("map_workflow_state", {}), secrets,
                                        restoring=False),
        "map_job_state": _job_state(metadata.get("map_job_state", {}), cwd, config),
        "geo_data_context": {"profiles": _profiles(data.get("profiles"), secrets)},
        "map_deterministic_errors": _errors(metadata.get("map_deterministic_errors"), secrets),
        "geo_request_failures": _request_failures(metadata.get("geo_request_failures"), secrets),
        "geo_query_recovery": _query_mode(metadata.get("geo_query_recovery"), config),
        "analysis_workflow_state": _analysis_stages(
            metadata.get("analysis_workflow_state"), secrets),
    }
    serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if any(secret in serialized for secret in secrets):
        # Budget names/paths are identities, so redacting or silently dropping
        # them would reset consumed budget. Reject instead of publishing secrets.
        raise ValueError("Geo checkpoint contains a credential in an identity")
    if len(serialized.encode()) > MAX_CHECKPOINT_BYTES:
        raise ValueError("Geo session checkpoint exceeds its size limit")
    return result


def _analysis_stages(value: Any, secrets: tuple[str, ...]) -> dict:
    if not isinstance(value, dict):
        return {}
    result = {}
    for name, record in list(value.items())[-64:]:
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name)
                or not isinstance(record, dict) or not _digest(record.get('fingerprint'))
                or record.get('status') not in {'succeeded', 'failed', 'timed_out'}):
            continue
        copied = _safe_json(record, secrets)
        if isinstance(copied, dict):
            # Dependency use rechecks the actual producer and every file hash.
            result[name] = copied
    return result


def restore_geo_session_state(
    metadata: dict[str, Any], restored_metadata: dict[str, Any], *,
    cwd: str | Path, config: GeoConfig,
) -> bool:
    """Restore validated fields into a fresh runtime; return whether Geo state loaded.

    Old sessions still restore task focus. A missing/invalid/foreign checkpoint
    leaves fresh Geo state untouched; no config or live object is imported from it.
    Counts are kept even if current limits became smaller. Positive visual history
    is downgraded until a current review establishes new evidence.
    """
    if not isinstance(restored_metadata, dict):
        return False
    secrets = _review_secrets(config)
    focus = _focus(restored_metadata.get("task_focus_state"), secrets)
    if focus is not None:
        metadata["task_focus_state"] = focus
    checkpoint = restored_metadata.get(GEO_SESSION_CHECKPOINT_KEY)
    if not isinstance(checkpoint, dict) or type(checkpoint.get("version")) is not int:
        return False
    root = Path(cwd).resolve()
    if checkpoint["version"] != GEO_SESSION_CHECKPOINT_VERSION or _workspace(
        checkpoint.get("cwd"),
    ) != root:
        return False
    try:
        serialized = json.dumps(checkpoint, ensure_ascii=False, allow_nan=False).encode()
        if len(serialized) > MAX_CHECKPOINT_BYTES:
            return False
        workflow = _workflow(checkpoint.get("map_workflow_state"), secrets, restoring=True)
        jobs = _job_state(checkpoint.get("map_job_state"), root, config)
        data = checkpoint.get("geo_data_context")
        profiles = _profiles(data.get("profiles"), secrets) if isinstance(data, dict) else {}
        errors = _errors(checkpoint.get("map_deterministic_errors"), secrets)
        failures = _request_failures(checkpoint.get("geo_request_failures"), secrets)
        recovery = _query_mode(checkpoint.get("geo_query_recovery"), config)
        analysis_stages = _analysis_stages(checkpoint.get('analysis_workflow_state'), secrets)
    except (ValueError, TypeError, OverflowError, RecursionError):
        return False
    # Commit only after validation; alias references inside workflow still point
    # to the same dictionary. Never update the whole untrusted metadata object.
    metadata["map_workflow_state"] = workflow
    metadata["map_job_state"] = jobs
    metadata["geo_data_context"] = {"profiles": profiles}
    metadata["map_deterministic_errors"] = errors
    metadata["geo_request_failures"] = failures
    metadata["geo_query_recovery"] = recovery
    metadata['analysis_workflow_state'] = analysis_stages
    return True


class GeoSessionBackend(OpenHarnessSessionBackend):
    """Compatible sessions in GeoHarness storage, independent of upstream env vars."""

    def get_session_dir(self, cwd: str | Path) -> Path:
        root = Path(cwd).resolve()
        digest = sha1(str(root).encode("utf-8")).hexdigest()[:12]
        directory = get_config_dir() / "data" / "sessions" / f"{root.name}-{digest}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    @staticmethod
    def _load(path: Path) -> dict | None:
        if not path.is_file():
            return None
        # The upstream normalizer preserves extra metadata while repairing
        # interrupted tool-use conversations for all loading entry points.
        return session_storage._sanitize_snapshot_payload(
            json.loads(path.read_text(encoding="utf-8")),
        )

    def load_latest(self, cwd: str | Path) -> dict | None:
        return self._load(self.get_session_dir(cwd) / "latest.json")

    def load_by_id(self, cwd: str | Path, session_id: str) -> dict | None:
        directory = self.get_session_dir(cwd)
        # Session IDs from disk/UI must not become arbitrary path locators.
        if not isinstance(session_id, str) or re.fullmatch(
            r"[A-Za-z0-9_-]{1,128}", session_id,
        ) is None:
            return None
        snapshot = self._load(directory / f"session-{session_id}.json")
        if snapshot is not None:
            return snapshot
        latest = self._load(directory / "latest.json")
        return latest if latest is not None and (
            session_id == "latest" or latest.get("session_id") == session_id
        ) else None

    def list_snapshots(self, cwd: str | Path, limit: int = 20) -> list[dict]:
        directory = self.get_session_dir(cwd)
        paths = sorted(directory.glob("session-*.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
        sessions, seen = [], set()
        for path in [*paths, directory / "latest.json"]:
            if len(sessions) >= limit:
                break
            try:
                data = self._load(path)
                if data is None:
                    continue
                sid = data.get("session_id", path.stem.removeprefix("session-"))
                if sid in seen:
                    continue
                seen.add(sid)
                summary = data.get("summary") or next((
                    message.text.strip()[:80] for message in (
                        ConversationMessage.model_validate(item)
                        for item in data.get("messages", [])
                    ) if message.role == "user" and message.text.strip()
                ), "")
                sessions.append({"session_id": sid, "summary": summary,
                                 "message_count": data.get("message_count", 0),
                                 "model": data.get("model", ""),
                                 "created_at": data.get("created_at", path.stat().st_mtime)})
            except (ValueError, OSError, TypeError):
                continue
        sessions.sort(key=lambda item: item.get("created_at", 0), reverse=True)
        return sessions[:limit]

    def export_markdown(
        self, *, cwd: str | Path, messages: list[ConversationMessage],
    ) -> Path:
        path = self.get_session_dir(cwd) / "transcript.md"
        parts = ["# OpenHarness Session Transcript"]
        for message in messages:
            parts.append(f"\n## {message.role.capitalize()}\n")
            if text := message.text.strip():
                parts.append(text)
            for block in message.tool_uses:
                parts.append(f"\n```tool\n{block.name} "
                             f"{json.dumps(block.input, ensure_ascii=True)}\n```")
            for block in message.content:
                if getattr(block, "type", "") == "tool_result":
                    parts.append(f"\n```tool-result\n{block.content}\n```")
        atomic_write_text(path, "\n".join(parts).strip() + "\n")
        return path

    def save_snapshot(
        self, *, cwd: str | Path, model: str, system_prompt: str,
        messages: list[ConversationMessage], usage: UsageSnapshot,
        session_id: str | None = None, tool_metadata: dict[str, object] | None = None,
    ) -> Path:
        root = Path(cwd).resolve()
        source = tool_metadata if isinstance(tool_metadata, dict) else {}
        # Keep the v0.1.9 upstream whitelist/normalization unchanged. The private
        # helper is reused read-only because no public payload-builder is exposed.
        persisted = session_storage._persistable_tool_metadata(source)
        config = source.get("geoharness_config")
        if isinstance(config, GeoConfig):
            persisted[GEO_SESSION_CHECKPOINT_KEY] = _checkpoint(source, root, config)
            from geoharness.telemetry import safe_metrics

            for key in ("geo_request_metrics", "geo_tool_metrics"):
                persisted[key] = safe_metrics(source.get(key),
                    limit=config.agent.request_metrics_limit, secrets=_review_secrets(config))
            timing = source.get("geo_task_timing", {})
            if isinstance(timing, dict) and _seconds(timing.get("started_at")) is not None:
                persisted["geo_task_timing"] = {"started_at": timing["started_at"]}
        messages = sanitize_conversation_messages(messages)
        sid = session_id or uuid4().hex[:12]
        if not isinstance(sid, str) or not sid or len(sid) > 128 or re.fullmatch(
            r"[A-Za-z0-9_-]+", sid,
        ) is None:
            raise ValueError("Session ID must be a safe filename identifier")
        summary = next((message.text.strip()[:80] for message in messages
                        if message.role == "user" and message.text.strip()), "")
        payload = {
            "session_id": sid, "cwd": str(root), "model": model, "system_prompt": system_prompt,
            "messages": [message.model_dump(mode="json") for message in messages],
            "usage": usage.model_dump(), "tool_metadata": persisted, "created_at": time.time(),
            "summary": summary, "message_count": len(messages),
        }
        data = json.dumps(payload, indent=2, allow_nan=False) + "\n"
        directory = self.get_session_dir(root)
        # Both files contain complete identical payloads. Publish latest last;
        # a failed named write cannot advertise a partially saved new session.
        atomic_write_text(directory / f"session-{sid}.json", data)
        latest = directory / "latest.json"
        atomic_write_text(latest, data)
        return latest
