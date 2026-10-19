"""Shared map accounting, review caching and repair deduplication per request."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from geoharness.cartography.jobs import canonical_budget_map
from geoharness.cartography.quality import MapReview, review_map
from geoharness.config.settings import GeoConfig

# Runtime synchronization belongs outside JSON-persisted tool metadata. Weak
# references release locks after active calls finish; cache entries stay in state.
_REVIEW_LOCKS: WeakValueDictionary[tuple[int, str], asyncio.Lock] = WeakValueDictionary()
_REPAIR_LOCKS: WeakValueDictionary[tuple[int, str], asyncio.Lock] = WeakValueDictionary()
# Bump only when the response contract/compatibility changes. Legacy failed
# cache entries can then recover without repeatedly resending unchanged images.
REVIEW_CONTRACT_VERSION = 10
_BUDGET_LOCK = threading.RLock()


def reset_map_workflow(metadata: dict[str, Any]) -> None:
    metadata.pop("map_deterministic_errors", None)
    with _BUDGET_LOCK:
        state = metadata.setdefault("map_workflow_state", {})
        service_recovery = state.get("service_recovery", {})
        # Keep actual in-memory conclusions; changing the target invalidates their
        # fingerprint, and checkpoint restoration still invalidates old passes.
        old_settings = state.get("cache_settings", {})
        review_checks = state.get('review_checks', {})
        retained_cache = {
            digest: review for digest, review in state.get("cache", {}).items()
            if isinstance(review, dict) and review.get("status") in {"passed", "issues"}
            and isinstance(review.get("diagnostics"), dict)
            and not review["diagnostics"].get("failure_stage")
            and not review["diagnostics"].get("checkpoint_requires_visual_review")
            and review["diagnostics"].get("stage") != "layout"
            and review["diagnostics"].get("cached_stage") != "layout"
            and digest in old_settings
        }
        state.clear()
        state.update(
            reviews=0,
            seconds=0.0,
            reserved_seconds=0.0,
            maps={},
            cache=retained_cache,
            cache_settings={digest: old_settings[digest] for digest in retained_cache},
            validation_retries={},
            service_recovery=service_recovery,
            review_activity={},
            provider_calls_total=0,
            cache_hits_total=0,
            repair_attempts={},
            review_checks=review_checks,
        )
        if isinstance(metadata.get("map_job_state"), dict):
            metadata["map_job_state"].pop("budget_job_id", None)


def _state(metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata.get("map_workflow_state"), dict):
        metadata["map_workflow_state"] = {}
    state = metadata["map_workflow_state"]
    state.setdefault("reviews", 0)
    state.setdefault("seconds", 0.0)
    state.setdefault("reserved_seconds", 0.0)
    state.setdefault("maps", {})
    state.setdefault("cache", {})
    state.setdefault("cache_settings", {})
    state.setdefault("validation_retries", {})
    state.setdefault("service_recovery", {})
    state.setdefault("review_activity", {})
    state.setdefault("provider_calls_total", 0)
    state.setdefault("cache_hits_total", 0)
    state.setdefault("repair_attempts", {})
    return state


def repair_loop_lock(metadata: dict[str, Any], map_id: str) -> asyncio.Lock:
    key = (id(_state(metadata)), map_id)
    lock = _REPAIR_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _REPAIR_LOCKS[key] = lock
    return lock


def claim_repair_attempt(metadata: dict[str, Any], digest: str) -> bool:
    """A failed refunded plan must not loop, including after a resumed query."""
    with _BUDGET_LOCK:
        attempts = _state(metadata)["repair_attempts"]
        if digest in attempts:
            return False
        attempts[digest] = True
        return True


def map_identity(name: str) -> str:
    name = re.sub(r"-\d{8}T\d{6}Z-[0-9a-f]+$", "", name)
    return re.sub(r"(?:[-_](?:v\d+|final|preview))+$", "", name, flags=re.I) or "map"


def image_map_identity(path: Path) -> str:
    for directory in (path.parent, *list(path.parents)[1:4]):
        if (directory / "manifest.json").is_file():
            return map_identity(directory.name)
    return map_identity(path.stem)


def _pixel_digest(path: Path) -> str:
    """Hash rendered pixels separately from container metadata."""
    try:
        from PIL import Image

        with Image.open(path) as image:
            rgba = image.convert("RGBA")
            payload = json.dumps({"size": rgba.size, "mode": "RGBA"},
                                 sort_keys=True).encode() + rgba.tobytes()
        return hashlib.sha256(payload).hexdigest()
    except Exception:
        # Unsupported image formats retain the safe file identity.
        return hashlib.sha256(path.read_bytes()).hexdigest()


def _budget_entry(
    name: str, metadata: dict[str, Any], config: GeoConfig
) -> tuple[str, dict[str, Any]]:
    identity = canonical_budget_map(name, metadata, config)
    maps = _state(metadata)["maps"]
    job_state = metadata.get("map_job_state", {})
    aliases = job_state.get("aliases", {}) if isinstance(job_state, dict) else {}
    names = {name, identity, *(alias for alias, job_id in aliases.items() if job_id == identity)}
    # Old consumers may already have charged an alias before a canonical ID was
    # established. Merge distinct entries once, then keep every alias identical.
    entries = {id(maps[key]): maps[key] for key in names if key in maps}
    entry = maps.get(identity)
    if entry is None:
        entry = next(iter(entries.values()), {"renders": 0, "reviews": 0})
    if entries:
        renders = sum(value.get("renders", 0) for value in entries.values())
        reviews = sum(value.get("reviews", 0) for value in entries.values())
        entry.update(renders=renders, reviews=reviews)
    for key in names:
        maps[key] = entry
    return identity, entry


def reserve_map_render(
    name: str,
    metadata: dict[str, Any],
    config: GeoConfig,
) -> str | None:
    """Record a render attempt; legacy callers never receive a budget stop."""
    with _BUDGET_LOCK:
        _, entry = _budget_entry(name, metadata, config)
        entry["renders"] += 1
        return None


def refund_map_render(name: str, metadata: dict[str, Any]) -> bool:
    """Release one failed render reservation; callers must own that reservation."""
    with _BUDGET_LOCK:
        job_state = metadata.get("map_job_state", {})
        aliases = job_state.get("aliases", {}) if isinstance(job_state, dict) else {}
        maps = _state(metadata)["maps"]
        entry = (
            maps.get(name)
            or maps.get(job_state.get("budget_job_id"))
            or maps.get(aliases.get(name, name))
        )
        if entry is None or entry["renders"] <= 0:
            return False
        entry["renders"] -= 1
        return True


def reserve_python_render(
    *,
    code: str,
    name: str,
    map_id: str,
    metadata: dict[str, Any],
    config: GeoConfig,
) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None  # The runner reports the actual syntax error.
    draw_names = set()
    draw_modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            draw_modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name.startswith("geoharness.cartography")
            )
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "geoharness":
                draw_modules.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "cartography"
                )
            if node.module.startswith("matplotlib"):
                draw_names.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "savefig"
                )
            elif node.module.startswith("geoharness.cartography"):
                draw_names.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name
                    in {
                        "render_map",
                        "render_static",
                        "render_layers",
                        "save_map_figure",
                    }
                )

    def qualified_name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return qualified_name(node.value) + "." + node.attr
        return ""

    if not any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Attribute) and node.func.attr == "savefig")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr
                in {
                    "render_map",
                    "render_static",
                    "render_layers",
                    "save_map_figure",
                }
                and qualified_name(node.func.value) in draw_modules
            )
            or (isinstance(node.func, ast.Name) and node.func.id in draw_names)
        )
        for node in ast.walk(tree)
    ):
        return None
    return reserve_map_render(map_id or map_identity(name), metadata, config)


def _review_service_key(config: GeoConfig) -> str:
    settings = config.map_review
    # Scope outages to the service, not filenames, PNG metadata or render exports.
    return hashlib.sha256(json.dumps({
        "model": settings.model or config.model,
        "base_url": settings.base_url or config.base_url,
        "credential_digest": hashlib.sha256(
            (settings.api_key or config.api_key).encode()
        ).hexdigest(),
    }, sort_keys=True).encode()).hexdigest()


def _transient_review_failure(review: MapReview) -> bool:
    return review.status == "unverified" and (
        review.diagnostics.get("service_failure_transient") is True
        or review.diagnostics.get("http_status") in {408, 425, 429, 500, 502, 503, 504}
    )


async def review_with_budget(
    path: Path,
    *,
    config: GeoConfig,
    task: str,
    evidence: dict[str, Any],
    metadata: dict[str, Any],
    map_id: str = "",
    client: Any = None,
    retry_invalid_response: bool = False,
    retry_service_failure: bool = False,
    pixel_only: bool = False,
) -> tuple[MapReview, dict[str, Any]]:
    with _BUDGET_LOCK:
        identity, _ = _budget_entry(map_id or image_map_identity(path), metadata, config)
        state = _state(metadata)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    pixel_digest = _pixel_digest(path)
    from geoharness.cartography.review_context import build_review_iteration_context

    # History belongs to the actual artifact, not to the shared accounting UUID.
    iteration = build_review_iteration_context(
        metadata, map_id or image_map_identity(path), config, image_sha256=digest,
        page_index=(evidence.get("render_metadata") or {}).get("review_page_index", 0),
    )
    evidence = {**evidence, **iteration}
    service_key = _review_service_key(config)
    lock_key = (
        id(state), service_key if service_key in state["service_recovery"] else pixel_digest
    )
    lock = _REVIEW_LOCKS.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _REVIEW_LOCKS[lock_key] = lock
    async with lock:
        return await _review_once(
            path,
            state=state,
            metadata=metadata,
            digest=digest,
            pixel_digest=pixel_digest,
            config=config,
            task=task,
            evidence=evidence,
            map_id=identity,
            client=client,
            retry_invalid_response=retry_invalid_response,
            retry_service_failure=retry_service_failure,
            pixel_only=pixel_only,
        )


async def _review_once(
    path: Path,
    *,
    state: dict[str, Any],
    metadata: dict[str, Any],
    digest: str,
    pixel_digest: str,
    config: GeoConfig,
    task: str,
    evidence: dict[str, Any],
    map_id: str,
    client: Any,
    retry_invalid_response: bool,
    retry_service_failure: bool,
    pixel_only: bool,
) -> tuple[MapReview, dict[str, Any]]:
    settings = config.map_review
    native_evidence = evidence.get('render_metadata', {})
    expected_evidence = {key: value for key, value in evidence.items() if key not in {
        'previous_visual_reviews', 'version_limit', 'remaining_render_versions',
        'generation_unlimited'}}
    identity_contract = None
    if pixel_only:
        from geoharness.cartography.review_contract import add_scope, stable_contract

        evidence = add_scope(evidence, task=task)
        native_evidence = stable_contract(evidence)
        identity_contract = evidence['review_scope_identity']
        # Only rendered geometry and explicit checks define a pixel review scope.
        expected_evidence = {}
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "contract_version": REVIEW_CONTRACT_VERSION,
                "pixel_only": pixel_only,
                "backend": evidence.get("backend", "matplotlib"),
                "enabled": settings.enabled,
                "model": settings.model or config.model,
                "base_url": settings.base_url or config.base_url,
                "credential_digest": hashlib.sha256(
                    (settings.api_key or config.api_key).encode()
                ).hexdigest(),
                "max_tokens": settings.max_tokens,
                "timeout_seconds": settings.timeout_seconds,
                "read_timeout_seconds": settings.read_timeout_seconds,
                "max_image_side": settings.max_image_side,
                "max_rois": getattr(settings, "max_rois", 5),
                "roi_max_side": getattr(settings, "roi_max_side", 2048),
                "native_evidence": native_evidence,
                "scope_identity": identity_contract,
                "task": task if not pixel_only else "",
                "expected_evidence": expected_evidence if not pixel_only else {},
                "request_extra_body": settings.request_extra_body,
                "reasoning_effort": settings.reasoning_effort,
                "response_format": settings.response_format,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    flags = {
        "image_sha256": digest,
        "pixel_sha256": pixel_digest,
        "review_cached": False,
        "review_budget_exhausted": False,
        "request_sent": False,
        "invalid_response_retry": False,
        "invalid_response_retry_exhausted": False,
    }
    cache_key = f"{pixel_digest}:{fingerprint}"
    retry_key = cache_key
    service_key = _review_service_key(config)
    now = time.time()
    activity = state["review_activity"].setdefault(digest, {"provider_calls": 0, "cache_hits": 0})
    request_counted = False

    def request_started() -> None:
        nonlocal request_counted
        if not request_counted:
            activity["provider_calls"] += 1
            state["provider_calls_total"] += 1
            request_counted = True

    def facts(review: MapReview, *, cached: bool) -> dict[str, Any]:
        if cached:
            activity["cache_hits"] += 1
            state["cache_hits_total"] += 1
        elif review.diagnostics.get("request_sent", False) and not request_counted:
            request_started()  # Compatibility for a replacement review implementation.
        retry_at = state["service_recovery"].get(service_key, {}).get("retry_at", 0)
        waiting = _transient_review_failure(review) and retry_at > time.time()
        completed = review.status in {"passed", "issues"} and not (
            review.diagnostics.get("failure_stage")
            or review.diagnostics.get("stage") == "layout"
            or review.diagnostics.get("cached_stage") == "layout"
            or review.diagnostics.get("checkpoint_requires_visual_review")
        )
        next_action = (
            "reuse_completed_review" if completed else
            "wait_then_retry_same_image" if waiting else
            "retry_same_image" if _transient_review_failure(review) else
            "retry_invalid_response_once" if review.diagnostics.get("failure_stage")
            in {"json", "schema"} and not state["validation_retries"].get(retry_key) else
            "inspect_failure_and_fix_configuration_or_input"
        )
        evidence = {
            "visual_review_completed": completed,
            "image_provider_calls": activity["provider_calls"],
            "image_cache_hits": activity["cache_hits"],
            "request_provider_calls": state["provider_calls_total"],
            "request_cache_hits": state["cache_hits_total"],
            "provider_call_count_scope": "review_client_invocations_not_underlying_http_attempts",
            "service_retry_requested": retry_service_failure,
            "service_retry_allowed": _transient_review_failure(review) and not waiting,
            "service_retry_at": retry_at if waiting else None,
            "service_retry_after_seconds": max(0.0, retry_at - time.time()) if waiting else 0.0,
            "next_action": next_action,
            "quality_note": (
                "Visual review completed for this image and scope."
                if completed else
                "Visual review is incomplete; map quality remains unverified. "
                "Inspect failure_kind before attributing the failure to the network."
            ),
        }
        review.diagnostics.update(evidence)
        return evidence

    recovery = False
    cached = None
    if pixel_only and cache_key not in state['cache']:
        from geoharness.task_store import read_pixel_review

        committed = read_pixel_review(metadata, cache_key)
        if committed is not None:
            try:
                restored = MapReview.model_validate(committed)
                if restored.status in {'passed', 'issues'} and not restored.diagnostics.get(
                        'failure_stage'):
                    restored.diagnostics['persistent_pixel_evidence_reused'] = True
                    state['cache'][cache_key] = restored.model_dump()
                    state['cache_settings'][cache_key] = fingerprint
            except ValueError:
                pass
    lookup_key = cache_key if cache_key in state["cache"] else digest
    if lookup_key in state["cache"] and state["cache_settings"].get(lookup_key) == fingerprint:
        cached = MapReview.model_validate(state["cache"][lookup_key])
        failed_contract = cached.diagnostics.get("failure_stage") in {"json", "schema"}
        already_retried = bool(state["validation_retries"].get(retry_key))
        recovery = retry_invalid_response and failed_contract and not already_retried
        if not recovery and not _transient_review_failure(cached):
            cached.diagnostics = {
                **cached.diagnostics,
                "stage": "cache",
                "outcome": "hit",
                "cached_stage": cached.diagnostics.get("stage", "unknown"),
                "cached_outcome": cached.diagnostics.get("outcome", "unknown"),
                "request_sent": False,
            }
            return cached, {
                **flags,
                **facts(cached, cached=True),
                "review_cached": True,
                "review_failure_stage": cached.diagnostics.get("failure_stage", ""),
                "invalid_response_retry_exhausted": failed_contract and already_retried,
            }
    service = state["service_recovery"].get(service_key, {})
    if cached is not None and _transient_review_failure(cached) and not service:
        # Older checkpoints did not keep cooldown state. Establish it once.
        retry_at = cached.diagnostics.get("service_retry_at")
        if not isinstance(retry_at, (int, float)):
            retry_at = now + max(settings.service_failure_ttl_seconds,
                                 cached.diagnostics.get("retry_after_seconds", 0))
        service = {"retry_at": retry_at, "failure": cached.diagnostics}
        state["service_recovery"][service_key] = service
    if service.get("retry_at", 0) > now:
        # The bool is an explicit recovery request, never an override of Retry-After.
        held = cached if cached is not None and _transient_review_failure(cached) else MapReview(
            status="unverified", summary="Review service cooldown is active; "
            "no request was sent and this image remains visually unverified.",
            diagnostics=dict(service.get("failure", {})),
        )
        held.diagnostics.update(stage="cache", outcome="hit", request_sent=False,
                                cached_outcome="failed")
        return held, {**flags, **facts(held, cached=True), "review_cached": True,
                      "review_failure_stage": held.diagnostics.get("failure_stage", "")}
    identity = map_id or image_map_identity(path)
    entry = state["maps"].setdefault(identity, {"renders": 0, "reviews": 0})
    if recovery:
        state["validation_retries"][retry_key] = True
        flags["invalid_response_retry"] = True
    # Keep usage accounting without stopping later images or shortening requests.
    state["reviews"] += 1
    entry["reviews"] += 1
    timeout = settings.timeout_seconds
    state["reserved_seconds"] += timeout
    bounded = settings.model_copy(
        update={
            "timeout_seconds": timeout,
        }
    )
    from dataclasses import replace

    started = time.monotonic()
    try:
        result = await review_map(
            path,
            config=replace(config, map_review=bounded),
            task=task,
            evidence=evidence,
            client=client,
            on_request_started=request_started,
            **({"pixel_only": True} if pixel_only else {}),
        )
    finally:
        state["seconds"] += time.monotonic() - started
        state["reserved_seconds"] = max(0.0, state["reserved_seconds"] - timeout)
    if _transient_review_failure(result):
        delay = max(settings.service_failure_ttl_seconds,
                    result.diagnostics.get("retry_after_seconds", 0))
        retry_at = time.time() + delay
        result.diagnostics["service_retry_at"] = retry_at
        state["service_recovery"][service_key] = {
            "retry_at": retry_at,
            "failure": {key: value for key, value in result.diagnostics.items() if key in {
                "failure_stage", "error_type", "http_status", "service_failure_transient",
                "service_error_code", "retry_after_seconds", "service_retry_at",
            }},
        }
    else:
        state["service_recovery"].pop(service_key, None)
    # Successful reviews stay cached; transient outages expire without rerendering.
    result.diagnostics["review_contract_version"] = REVIEW_CONTRACT_VERSION
    flags["request_sent"] = result.diagnostics.get("request_sent", True)
    flags["review_failure_stage"] = result.diagnostics.get("failure_stage", "")
    flags.update(facts(result, cached=False))
    state["cache"][cache_key] = result.model_dump()
    state["cache_settings"][cache_key] = fingerprint
    # Keep the file-key alias for checkpoint compatibility with older sessions.
    state["cache"][digest] = result.model_dump()
    state["cache_settings"][digest] = fingerprint
    if (pixel_only and result.status in {'passed', 'issues'}
            and flags.get('visual_review_completed')):
        from geoharness.task_store import save_pixel_review

        save_pixel_review(metadata, cache_key, result.model_dump())
    return result, flags
