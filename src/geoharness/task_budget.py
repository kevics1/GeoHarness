"""A visible task deadline target, preserving atomic tool work and truthful delivery."""

import json
import time


def start_task(metadata, config):
    metadata["geo_task_timing"] = {"started_at": time.time()}


def budget_status(metadata, config, *, now=None):
    seconds = config.agent.task_budget_seconds
    started = metadata.get("geo_task_timing", {}).get("started_at")
    if seconds is None or not isinstance(started, (int, float)):
        return {}
    elapsed = max(0.0, (time.time() if now is None else now) - started)
    remaining = max(0.0, seconds - elapsed)
    return {"target_seconds": seconds, "elapsed_seconds": round(elapsed, 1),
            "remaining_seconds": round(remaining, 1),
            "phase": "deadline" if remaining == 0 else
                     "delivery" if remaining <= config.agent.delivery_reserve_seconds else
                     "work", "hard_timeout": False}


def budget_context(metadata, config):
    status = budget_status(metadata, config)
    if not status:
        return ""
    return ("\nTask time target: " + json.dumps(status) + ". "
            "Merge related map fixes into one build(spec_ref, edits). Review all verified pages "
            "with all_pages=true; image review only reports findings and never rebuilds. "
            "During delivery/deadline, prioritize factual blockers and delivery of existing "
            "artifacts with precise unresolved status; stop cosmetic refinement. Never claim "
            "unverified work passed. This is a time target, not permission to weaken checks.\n")
