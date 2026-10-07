"""Shared budgets for map versions and image reviews within one user request."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import re
import time
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from geoharness.cartography.quality import MapReview, review_map
from geoharness.config.settings import GeoConfig

# Runtime synchronization belongs outside JSON-persisted tool metadata. Weak
# references release locks after active calls finish; cache entries stay in state.
_REVIEW_LOCKS: WeakValueDictionary[tuple[int, str], asyncio.Lock] = WeakValueDictionary()


def reset_map_workflow(metadata: dict[str, Any]) -> None:
    state = metadata.setdefault("map_workflow_state", {})
    state.clear()
    state.update(reviews=0, seconds=0.0, maps={}, cache={})


def _state(metadata: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata.get("map_workflow_state"), dict):
        metadata["map_workflow_state"] = {}
    state = metadata["map_workflow_state"]
    state.setdefault("reviews", 0)
    state.setdefault("seconds", 0.0)
    state.setdefault("maps", {})
    state.setdefault("cache", {})
    return state


def map_identity(name: str) -> str:
    name = re.sub(r"-\d{8}T\d{6}Z-[0-9a-f]+$", "", name)
    return re.sub(r"(?:[-_](?:v\d+|final|preview))+$", "", name, flags=re.I) or "map"


def image_map_identity(path: Path) -> str:
    for directory in (path.parent, *list(path.parents)[1:4]):
        if (directory / "manifest.json").is_file():
            return map_identity(directory.name)
    return map_identity(path.stem)


def reserve_map_render(
    name: str,
    metadata: dict[str, Any],
    config: GeoConfig,
) -> str | None:
    state = _state(metadata)
    entry = state["maps"].setdefault(name, {"renders": 0, "reviews": 0})
    limit = 1 + config.map_review.max_repairs
    if entry["renders"] >= limit:
        return (
            f"Map '{name}' reached its {limit}-version budget for this request. "
            "Stop automatic refinements and deliver the latest map, editable project and "
            "remaining issues. A new user request can authorize further edits."
        )
    entry["renders"] += 1
    return None


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
                    if alias.name in {"render_map", "render_static", "render_layers"}
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
                and node.func.attr in {"render_map", "render_static", "render_layers"}
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
) -> tuple[MapReview, dict[str, Any]]:
    state = _state(metadata)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
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
            map_id=map_id,
            client=client,
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
) -> tuple[MapReview, dict[str, Any]]:
    if digest in state["cache"]:
        return MapReview.model_validate(state["cache"][digest]), {
            "review_cached": True,
            "review_budget_exhausted": False,
        }
    identity = map_id or image_map_identity(path)
    entry = state["maps"].setdefault(identity, {"renders": 0, "reviews": 0})
    settings = config.map_review
    remaining_seconds = settings.total_timeout_seconds - state["seconds"]
    if "started_at" in state:
        remaining_seconds = min(
            remaining_seconds,
            settings.total_timeout_seconds - (time.time() - state["started_at"]),
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
        ), {"review_cached": False, "review_budget_exhausted": True}
    # Reserve before yielding, so parallel tool calls cannot overspend the count.
    state["reviews"] += 1
    entry["reviews"] += 1
    state.setdefault("started_at", time.time())
    bounded = settings.model_copy(
        update={
            "timeout_seconds": min(settings.timeout_seconds, remaining_seconds),
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
    # Cache failures too. Unchanged images should never trigger blind API retries.
    state["cache"][digest] = result.model_dump()
    return result, {"review_cached": False, "review_budget_exhausted": False}
