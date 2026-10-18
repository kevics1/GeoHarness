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
    return ("\n任务时间目标：" + json.dumps(status) + "。"
            "把相关修复合并到一次build(spec_ref, edits)，用all_pages=true逐页审查；"
            "图片审查只报告问题、不重建。接近交付预留或时间目标时先处理事实错误，"
            "停止装饰性优化；如需继续耗时修复，询问用户继续优化还是交付当前草稿。"
            "准确说明未解决项，不能把未验证标为通过；时间目标不允许削弱检查。\n")
