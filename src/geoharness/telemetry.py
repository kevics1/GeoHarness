"""Allowlisted diagnostics; no prompts, tool arguments, exception bodies or credentials."""

import math

_NUMBERS = {
    "attempt", "started_at", "elapsed_seconds", "first_token_seconds", "input_tokens",
    "output_tokens", "reasoning_tokens", "message_count", "message_chars", "tool_schema_chars",
    "archived_results", "archived_inputs", "saved_chars", "input_chars", "output_chars",
}
_STRINGS = {"request_id", "execution_id", "model", "response_model", "reasoning_effort",
            "finish_reason", "error_type", "tool", "action"}


def safe_metrics(value, *, limit, secrets=()):
    if not isinstance(value, (list, tuple)):
        return []
    result = []
    for record in value[-limit:]:
        if not isinstance(record, dict):
            continue
        clean = {}
        for key, item in record.items():
            if key in _NUMBERS and (item is None or type(item) in (int, float)):
                try:
                    if item is None or math.isfinite(item) and item >= 0:
                        clean[key] = item
                except OverflowError:
                    pass
            elif key in _STRINGS and (item is None or isinstance(item, str)):
                if item is None or len(item) <= 256 and not any(s in item for s in secrets):
                    clean[key] = item
            elif key in {"is_error", "reused", "usage_reported"} and type(item) is bool:
                clean[key] = item
        if clean:
            result.append(clean)
    return result


def safe_call_counts(value):
    if not isinstance(value, dict):
        return {}
    return {tool: {'calls': v['calls'], 'errors': v['errors']}
            for tool, v in list(value.items())[-256:]
            if isinstance(tool, str) and tool.replace('_', '').isalnum() and len(tool) <= 128
            and isinstance(v, dict) and type(v.get('calls')) is int
            and type(v.get('errors')) is int and 0 <= v['errors'] <= v['calls'] <= 10**9}


def timing_summary(metadata, *, now):
    """Union measured intervals: parallel execution must not inflate wall-clock usage."""
    started = metadata.get('geo_task_timing', {}).get('started_at')
    if type(started) not in (int, float) or not math.isfinite(started) or started > now:
        return {}
    intervals = []
    totals = {}
    for key in ('geo_request_metrics', 'geo_tool_metrics'):
        records = [r for r in metadata.get(key, []) if isinstance(r, dict)
                   and type(r.get('started_at')) in (int, float) and r['started_at'] >= started]
        totals[key + '_count'] = len(records)
        for record in records:
            duration = record.get('elapsed_seconds')
            if type(duration) in (int, float) and math.isfinite(duration) and duration >= 0:
                left, right = record['started_at'], min(now, record['started_at'] + duration)
                if right >= left:
                    intervals.append((left, right))
    merged = []
    for left, right in sorted(intervals):
        if merged and left <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    busy = sum(right - left for left, right in merged)
    return {**totals, 'wall_elapsed_seconds': round(now - started, 3),
            'measured_busy_seconds': round(busy, 3),
            'unattributed_seconds': round(max(0, now - started - busy), 3),
            'unattributed_scope': 'context,dispatch,network_outside_requests,user_wait; '
                                  'not_reasoning'}
