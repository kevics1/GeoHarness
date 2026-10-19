"""Compatibility time target: observational only, never an execution deadline."""

import json
import time


def start_task(metadata, config):
    metadata["geo_task_timing"] = {"started_at": time.time()}
    metadata["geo_tool_call_counts"] = {}
    metadata.setdefault("geo_current_state", {}).clear()

def budget_status(metadata, config, *, now=None):
    seconds = config.agent.task_budget_seconds
    started = metadata.get("geo_task_timing", {}).get("started_at")
    if seconds is None or not isinstance(started, (int, float)):
        return {}
    elapsed = max(0.0, (time.time() if now is None else now) - started)
    remaining = max(0.0, seconds - elapsed)
    return {"target_seconds": seconds, "elapsed_seconds": round(elapsed, 1),
            "remaining_seconds": round(remaining, 1),
            "phase": "target_exceeded" if remaining == 0 else "work",
            "hard_timeout": False, "observational_only": True}

def budget_context(metadata, config):
    status = budget_status(metadata, config)
    if not status:
        return ""
    return ("\n任务耗时观测（不是截止时间或调用额度）：" + json.dumps(status) + "。"
            "优先复用有效结果，把核实后的相关修复合并到一次render(scene_ref, edits)。"
            "按变化审查页面，完成时核对全部页面；图片审查只报告问题、不重建。"
            "不因超过时间目标而提前结束、降低质量或强制交付草稿。"
            "准确说明未解决项，不能把未验证标为通过。\n")
