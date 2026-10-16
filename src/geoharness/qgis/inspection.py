"""Bounded views of verified in-memory contracts; never grant directory access."""

from __future__ import annotations

import json


def view(value, *, call: dict, path=(), offset=0, limit=8, max_chars=2400) -> dict:
    """Page dictionary/list/string values; large children carry exact read calls."""
    selected = value
    for key in path:
        if isinstance(selected, dict) and isinstance(key, str) and key in selected:
            selected = selected[key]
        elif isinstance(selected, list) and type(key) is int and 0 <= key < len(selected):
            selected = selected[key]
        else:
            raise ValueError(f"Unknown diagnostic JSON path: {list(path)}")

    def read(child_path, start=0):
        return {**call, "detail_path": list(child_path), "offset": start, "limit": limit}

    def child(item, child_path):
        if len(json.dumps(item, ensure_ascii=False)) <= max_chars // (limit + 2):
            return item
        return {"kind": type(item).__name__,
                "items": len(item) if isinstance(item, (dict, list, str)) else None,
                "read_call": read(child_path)}

    result = {"detail_path": list(path), "offset": offset}
    if isinstance(selected, (dict, list)):
        entries = (list(selected.items()) if isinstance(selected, dict)
                   else list(enumerate(selected)))
        chosen = entries[offset:offset + limit]
        values = {key: child(item, [*path, key]) for key, item in chosen}
        result.update(total=len(entries), value=values if isinstance(selected, dict)
                      else list(values.values()))
        if offset + len(chosen) < len(entries):
            result["next_call"] = read(path, offset + len(chosen))
    elif isinstance(selected, str):
        # JSON escaping can expand individual characters, so size is checked below.
        result.update(total=len(selected), value=selected[offset:offset + max_chars // 3])
        if offset + len(result["value"]) < len(selected):
            result["next_call"] = read(path, offset + len(result["value"]))
    else:
        result["value"] = selected
    if len(json.dumps(result, ensure_ascii=False)) > max_chars:
        if limit > 1:
            return view(value, call=call, path=path, offset=offset,
                        limit=max(1, limit // 2), max_chars=max_chars)
        raise ValueError("Diagnostic selection exceeds the response limit; select a child path")
    return result


def help_output(payload: dict, *, call: dict, max_chars: int) -> dict:
    """Keep familiar schema properties, with full definitions accessible by JSON path."""
    import copy

    result = copy.deepcopy(payload)
    for schema in [result.get("schema", {}), *result.get("schemas", [])]:
        if not schema:
            continue
        schema.pop("$defs", None)
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            for field in ("description", "title", "default"):
                prop.pop(field, None)
    result["detail_call"] = {**call, "detail_path": ["schema"]
                             if "schema" in payload else ["schemas"]}
    if len(json.dumps(result, ensure_ascii=False)) <= max_chars:
        return result
    return {"section": payload["section"], "workflow": payload["workflow"],
            **view(payload, call=call, max_chars=max_chars - 250)}
