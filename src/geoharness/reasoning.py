"""Explicit opt-in reasoning policy shared by main, recovery and review requests."""

from typing import Any, Literal

ReasoningEffort = Literal["low", "medium", "high", "max"]


def request_extensions(body: dict[str, Any], effort: ReasoningEffort | None) -> dict[str, Any]:
    """An explicit policy wins over stale provider extensions; None preserves compatibility."""
    if effort is None:
        return dict(body)
    return {**body, "thinking": {"type": "enabled"}, "reasoning_effort": effort}
