"""Shared budgets for map versions and image reviews within one user request."""

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
# Bump only when the response contract/compatibility changes. Legacy failed
# cache entries can then recover without repeatedly resending unchanged images.
REVIEW_CONTRACT_VERSION = 4
_BUDGET_LOCK = threading.RLock()


def reset_map_workflow(metadata: dict[str, Any]) -> None:
    metadata.pop("map_deterministic_errors", None)
    with _BUDGET_LOCK:
        state = metadata.setdefault("map_workflow_state", {})
        state.clear()
        state.update(
            reviews=0,
            seconds=0.0,
            reserved_seconds=0.0,
            maps={},
            cache={},
            cache_settings={},
            validation_retries={},
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
    return state


def map_identity(name: str) -> str:
    name = re.sub(r"-\d{8}T\d{6}Z-[0-9a-f]+$", "", name)
    return re.sub(r"(?:[-_](?:v\d+|final|preview))+$", "", name, flags=re.I) or "map"


def image_map_identity(path: Path) -> str:
    for directory in (path.parent, *list(path.parents)[1:4]):
        if (directory / "manifest.json").is_file():
            return map_identity(directory.name)
    return map_identity(path.stem)


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
    with _BUDGET_LOCK:
        _, entry = _budget_entry(name, metadata, config)
        limit = 1 + config.map_review.max_repairs
        if entry["renders"] >= limit:
            return (
                f"Map '{name}' reached its {limit}-version budget for this request. "
                "Stop automatic refinements and deliver the latest map, editable project and "
                "remaining issues. A new user request can authorize further edits."
            )
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
) -> tuple[MapReview, dict[str, Any]]:
    with _BUDGET_LOCK:
        identity, _ = _budget_entry(map_id or image_map_identity(path), metadata, config)
        state = _state(metadata)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    from geoharness.cartography.review_context import build_review_iteration_context

    # A request shares one budget even when reviewing another persisted map.
    # History belongs to the actual artifact, not to that shared budget UUID.
    iteration = build_review_iteration_context(
        metadata, map_id or image_map_identity(path), config, image_sha256=digest,
    )
    budget = build_review_iteration_context(metadata, identity, config, receipt={})
    evidence = {**evidence, **iteration,
                "remaining_render_versions": budget["remaining_render_versions"]}
    lock_key = (id(state), digest)
    lock = _REVIEW_LOCKS.get(lock_key)
    if lock is None:
        lock = asyncio.Lock()
        _REVIEW_LOCKS[lock_key] = lock
    async with lock:
        return await _review_once(
            path,
            state=state,
            digest=digest,
            config=config,
            task=task,
            evidence=evidence,
            map_id=identity,
            client=client,
            retry_invalid_response=retry_invalid_response,
        )


async def _review_once(
    path: Path,
    *,
    state: dict[str, Any],
    digest: str,
    config: GeoConfig,
    task: str,
    evidence: dict[str, Any],
    map_id: str,
    client: Any,
    retry_invalid_response: bool,
) -> tuple[MapReview, dict[str, Any]]:
    settings = config.map_review
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "contract_version": REVIEW_CONTRACT_VERSION,
                "backend": evidence.get("backend", "matplotlib"),
                "enabled": settings.enabled,
                "model": settings.model or config.model,
                "base_url": settings.base_url or config.base_url,
                "credential_digest": hashlib.sha256(
                    (settings.api_key or config.api_key).encode()
                ).hexdigest(),
                "max_tokens": settings.max_tokens,
                "timeout_seconds": settings.timeout_seconds,
                "max_image_side": settings.max_image_side,
                "request_extra_body": settings.request_extra_body,
            },
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    flags = {
        "image_sha256": digest,
        "review_cached": False,
        "review_budget_exhausted": False,
        "request_sent": False,
        "invalid_response_retry": False,
        "invalid_response_retry_exhausted": False,
    }
    retry_key = f"{digest}:{fingerprint}"
    recovery = False
    if digest in state["cache"] and state["cache_settings"].get(digest) == fingerprint:
        cached = MapReview.model_validate(state["cache"][digest])
        failed_contract = cached.diagnostics.get("failure_stage") in {"json", "schema"}
        already_retried = bool(state["validation_retries"].get(retry_key))
        recovery = retry_invalid_response and failed_contract and not already_retried
        if not recovery:
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
                "review_cached": True,
                "review_failure_stage": cached.diagnostics.get("failure_stage", ""),
                "invalid_response_retry_exhausted": failed_contract and already_retried,
            }
    identity = map_id or image_map_identity(path)
    entry = state["maps"].setdefault(identity, {"renders": 0, "reviews": 0})
    remaining_seconds = (
        settings.total_timeout_seconds - state["seconds"] - state["reserved_seconds"]
    )
    if (
        state["reviews"] >= settings.max_reviews_per_request
        or entry["reviews"] >= settings.max_repairs + 1
        or remaining_seconds <= 0
    ):
        return MapReview(
            status="unverified",
            summary=(
                "Visual review budget reached. Do not retry or create more previews; "
                "deliver the current map and editable project with this limitation."
            ),
            diagnostics={
                "stage": "budget",
                "outcome": "failed",
                "failure_stage": "budget",
                "request_sent": False,
            },
        ), {**flags, "review_budget_exhausted": True, "review_failure_stage": "budget"}
    if recovery:
        state["validation_retries"][retry_key] = True
        flags["invalid_response_retry"] = True
    # Reserve before yielding, so parallel tool calls cannot overspend the count.
    state["reviews"] += 1
    entry["reviews"] += 1
    timeout = min(settings.timeout_seconds, remaining_seconds)
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
        )
    finally:
        state["seconds"] += time.monotonic() - started
        state["reserved_seconds"] = max(0.0, state["reserved_seconds"] - timeout)
    # Cache failures too. Unchanged images should never trigger blind API retries.
    result.diagnostics["review_contract_version"] = REVIEW_CONTRACT_VERSION
    flags["request_sent"] = result.diagnostics.get("request_sent", True)
    flags["review_failure_stage"] = result.diagnostics.get("failure_stage", "")
    state["cache"][digest] = result.model_dump()
    state["cache_settings"][digest] = fingerprint
    return result, flags
