"""Allowlisted diagnostics; no prompts, tool arguments, exception bodies or credentials."""

import math

_NUMBERS = {
    "attempt", "started_at", "elapsed_seconds", "first_token_seconds", "input_tokens",
    "output_tokens", "reasoning_tokens", "message_count", "message_chars", "tool_schema_chars",
    "archived_results", "saved_chars", "input_chars", "output_chars",
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
            elif key == "is_error" and type(item) is bool:
                clean[key] = item
        if clean:
            result.append(clean)
    return result
