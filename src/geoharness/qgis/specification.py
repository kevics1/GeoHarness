"""Immutable map specifications with small, explicit path edits.

The stored value is the existing project tool's input, not a second renderer model.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re

from geoharness.qgis.prepared import _directory


def save_spec(parameters: dict, *, cwd, settings) -> tuple[str, str]:
    content = json.dumps(parameters, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    digest = hashlib.sha256(content).hexdigest()
    directory = _directory(cwd, settings) / "specifications"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + ".json")
    if path.resolve() != path:
        raise ValueError("Map specification cannot be a link")
    path.write_bytes(content)
    return "qgis-spec:" + digest, str(path)


def load_spec(reference: str, *, cwd, settings) -> dict:
    if not re.fullmatch(r"qgis-spec:[0-9a-f]{64}", reference):
        raise ValueError("spec_ref must be an actual returned qgis-spec reference")
    digest = reference.split(":", 1)[1]
    path = _directory(cwd, settings) / "specifications" / (digest + ".json")
    if path.resolve() != path or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Map specification is missing, linked or oversized")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Map specification changed; save a new specification")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Map specification must be an input object")
    return result


def patch_spec(parameters: dict, patches: list[dict]) -> dict:
    """Set/remove existing fields; append only at the exact end of a list.

    Paths are arrays of keys/indices, so punctuation in field names is unambiguous.
    Lists are never implicitly replaced or shifted by another patch operation.
    """
    result = copy.deepcopy(parameters)
    for patch in patches:
        path = patch["path"]
        if not path or path[0] in {"action", "plan_ref", "spec_ref", "patches"}:
            raise ValueError("patch path must target a project input field")
        parent = result
        for key in path[:-1]:
            if isinstance(parent, dict) and isinstance(key, str) and key not in parent:
                parent[key] = {}
            try:
                parent = parent[key]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError(f"Unknown specification path: {path}") from exc
        key = path[-1]
        remove = patch.get("operation", "set") == "remove"
        if isinstance(parent, list):
            if not isinstance(key, int) or isinstance(key, bool) or key < 0:
                raise ValueError("List paths require non-negative integer indices")
            if remove:
                raise ValueError("Remove an entire list explicitly; "
                                 "list-index removal shifts layers")
            if key == len(parent):
                parent.append(copy.deepcopy(patch.get("value")))
            elif key < len(parent):
                parent[key] = copy.deepcopy(patch.get("value"))
            else:
                raise ValueError("A list patch cannot skip indices")
        elif isinstance(parent, dict) and isinstance(key, str):
            if remove:
                parent.pop(key, None)
            else:
                parent[key] = copy.deepcopy(patch.get("value"))
        else:
            raise ValueError(f"Invalid specification path: {path}")
    return result
