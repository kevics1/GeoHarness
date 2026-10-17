"""Immutable map specifications with small, explicit path edits.

The stored value is the existing project tool's input, not a second renderer model.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from geoharness.qgis.prepared import _directory


def compile_references(value):
    """Compile unique stable business keys to worker indices, without name guessing."""
    if not isinstance(value, dict) or not isinstance(value.get("layers"), list):
        return value
    from geoharness.qgis.layer_identity import stable_layer_key

    result = copy.deepcopy(value)
    layers = result["layers"]
    keys = {}
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict):
            if not hasattr(layer, "model_dump"):
                continue  # The canonical Pydantic layer model reports the exact invalid field.
            layer = layer.model_dump(exclude_unset=True)
            layers[index] = layer
        if not isinstance(layer.get("file_path"), str) or not layer["file_path"]:
            continue
        if "layer_key" in layer and not isinstance(layer["layer_key"], str):
            continue
        key = layer.get("layer_key") or stable_layer_key(layer.get("kind", "vector"),
            layer.get("name", ""), layer["file_path"], layer.get("layer_name", ""))
        if key in keys:
            raise ValueError("Duplicate layer_key; assign distinct stable keys to repeated sources")
        layer["layer_key"] = key
        keys[key] = index

    def resolve(reference):
        if isinstance(reference, str):
            if reference not in keys:
                raise ValueError(f"Unknown layer_key {reference!r}; "
                                 f"available layer_keys: {list(keys)}")
            return keys[reference]
        return reference

    layout = result.get("layout")
    if hasattr(layout, "model_dump"):
        layout = layout.model_dump(exclude_unset=True)
        result["layout"] = layout
    if isinstance(layout, dict):
        table = layout.get("label_table")
        if isinstance(table, dict) and "layer_index" in table:
            table["layer_index"] = resolve(table["layer_index"])
        for name in ("main_frame", "detail_frame"):
            frame = layout.get(name)
            if hasattr(frame, "model_dump"):
                frame = frame.model_dump(exclude_unset=True)
                layout[name] = frame
            if not isinstance(frame, dict):
                continue
            for field in ("focus_layers", "render_layers", "label_layers"):
                if isinstance(frame.get(field), list):
                    frame[field] = [resolve(item) for item in frame[field]]
            requirements = frame.get("requirements", [])
            for requirement in requirements if isinstance(requirements, list) else []:
                if isinstance(requirement, dict) and "layer_index" in requirement:
                    requirement["layer_index"] = resolve(requirement["layer_index"])
    return result


def canonical_spec(parameters: dict) -> dict:
    """Persist frame references as keys so reordering layers cannot retarget subjects."""
    result = compile_references(parameters)
    layers = result.get("layers", [])

    def key(index):
        if type(index) is not int or index < 0 or index >= len(layers):
            raise ValueError("Unknown layer index in map specification")
        return layers[index]["layer_key"]

    for frame in ((result.get("layout") or {}).get(name) or {}
                  for name in ("main_frame", "detail_frame")):
        for field in ("focus_layers", "render_layers", "label_layers"):
            if frame.get(field) is not None:
                frame[field] = [key(index) for index in frame[field]]
        for requirement in frame.get("requirements", []):
            requirement["layer_index"] = key(requirement["layer_index"])
    table = (result.get("layout") or {}).get("label_table")
    if table:
        table["layer_index"] = key(table["layer_index"])
    return result


def apply_edits(parameters: dict, edits: list[dict]) -> dict:
    """Small validated operations over the one canonical project input structure."""
    result = copy.deepcopy(parameters)
    for edit in edits:
        operation = edit["operation"]
        if operation == "layer":
            layer = edit["layer"]
            matches = [index for index, item in enumerate(result.setdefault("layers", []))
                       if layer.get("layer_key") and item.get("layer_key") == layer["layer_key"]]
            if matches:
                result["layers"][matches[0]].update(layer)
            else:
                result["layers"].append(layer)
        elif operation in {"style", "labels"}:
            matches = [item for item in result.get("layers", [])
                       if item.get("layer_key") == edit["layer_key"]]
            if len(matches) != 1:
                raise ValueError("Edit requires an actual unique layer_key from spec inspect")
            if operation == "style":
                matches[0]["style"] = {**(matches[0].get("style") or {}), **edit["style"]}
            else:
                matches[0]["label_field"] = edit["label_field"]
                if "label_style" in edit:
                    style = edit["label_style"]
                    matches[0]["label_style"] = ({**(matches[0].get("label_style") or {}), **style}
                                                 if style is not None else None)
        elif operation == "roster":
            layout = result.setdefault("layout", {})
            if edit.get("table") is None:
                layout.pop("label_table", None)
            else:
                layout["label_table"] = edit["table"]
        elif operation == "frame":
            layout = result.setdefault("layout", {})
            frame = edit["frame"]
            settings = {**(layout.get(frame + "_frame") or {}), **edit["settings"]}
            if not settings.get("purpose") or not settings.get("requirements"):
                raise ValueError("Typed frame edits require purpose and nonempty requirements; "
                                 "declare the actual subjects and their per-frame coverage")
            layout[frame + "_frame"] = settings
            holder = result if frame == "main" else layout
            extent_field = "map_extent" if frame == "main" else "detail_extent"
            if edit.get("extent") is not None:
                holder[extent_field] = edit["extent"]
                settings.pop("focus_layers", None)
            elif edit["settings"].get("focus_layers"):
                holder.pop(extent_field, None)
            if frame == "detail" and edit.get("minimum_zoom") is not None:
                layout["detail_minimum_zoom"] = edit["minimum_zoom"]
                layout["detail_zoom_policy"] = edit.get("zoom_policy", "advisory")
        else:
            raise ValueError(f"Unknown typed map operation: {operation}")
    return result


def contract_help(section: str) -> dict:
    """Return the actual runtime schema for the requested operation, not processing help."""
    from geoharness.tools.geo_qgis_project import (
        QgisFrameEdit,
        QgisLabelEdit,
        QgisLabelStyle,
        QgisLabelTable,
        QgisLayerEdit,
        QgisMapFrameSpec,
        QgisProjectLayer,
        QgisProjectLayout,
        QgisRosterEdit,
        QgisStyleEdit,
        QgisVectorStyle,
    )

    schemas = {"layer": QgisProjectLayer, "roster": QgisLabelTable,
               "style": QgisVectorStyle, "labels": QgisLabelStyle,
               "frame": QgisMapFrameSpec, "layout": QgisProjectLayout}
    from geoharness.invocation import action_forms, section_cautions

    result = {"section": section, "contract": "geo_qgis_project",
        "cautions": section_cautions(section),
        "workflow": ["spec once -> build(spec_ref, edits); one native validation/export job",
                     "Legacy: preflight(spec_ref) -> create(plan_ref)"],
        "rules": ["Only action+plan_ref for frozen create.",
                  "Frame references accept exact layer_key or 0-based index, never guessed names.",
                  "Explicit extent and focus_layers are mutually exclusive per frame.",
                  "dpi/map_extent/title are project fields; style.field/renderer are nested.",
                  "Visual zoom suggestions use advisory; preserve declared frame requirements."]}
    if section == 'workflow':
        result['action_forms'] = action_forms()
        result['ledger'] = {'inspect': {'action': 'inspect', 'map_id': 'returned-map-id'},
                            'recover': {'action': 'recover', 'map_id': 'returned-map-id'},
                            'note': 'Recovery migrates with backup and verification; '
                                    'health inspection is read-only. Use key_field for stable IDs.'}
    if section in schemas:
        result["schema"] = schemas[section].model_json_schema()
    elif section == "edits":
        result["schemas"] = [model.model_json_schema() for model in (
            QgisStyleEdit, QgisLabelEdit, QgisFrameEdit, QgisLayerEdit, QgisRosterEdit)]
    result["examples"] = {
        "style": {"operation": "style", "layer_key": "returned-key", "style": {
            "renderer": "categorized", "field": "actual_field", "categories": [
                {"value": "actual_value", "label": "显示名称", "color": "#d73027"}]}},
        "frame": {"operation": "frame", "frame": "detail", "settings": {
            "purpose": "火场详图", "focus_layers": ["returned-key"], "requirements": [
                {"layer_index": "returned-key", "coverage": "complete"}]},
            "minimum_zoom": 2, "zoom_policy": "advisory"},
    } if section == "workflow" else {}
    return result


def recent_specs(*, cwd, settings, limit=4) -> list[dict]:
    """Discover hash-verified drafts after continuation/restart; no parallel mutable state."""
    import heapq

    try:
        directory = _directory(cwd, settings) / "specifications"
        paths = heapq.nlargest(64, directory.glob("*.json"),
                              key=lambda path: path.stat().st_mtime_ns)
    except (OSError, ValueError):
        return []
    result = []
    for path in paths:
        reference = "qgis-spec:" + path.stem
        try:
            parameters = load_spec(reference, cwd=cwd, settings=settings)
        except (OSError, ValueError):
            continue
        result.append({"spec_ref": reference, "map_id": parameters.get("map_id", ""),
            "layer_count": len(parameters.get("layers", [])),
            "layer_keys": [layer.get("layer_key", "")
                           for layer in parameters.get("layers", [])[:8]],
            "inspect_call": {"action": "inspect", "spec_ref": reference}})
        if len(result) >= limit:
            break
    return result


def describe_layers(parameters: dict, *, context, config) -> list[dict]:
    """Attach already measured source facts, without treating missing profiles as known."""
    from geoharness.data.task_context import data_ref_snapshot

    references = data_ref_snapshot(context.metadata, cwd=Path(context.cwd), config=config)
    result = []
    for index, layer in enumerate(parameters.get("layers", [])):
        locator = layer["file_path"]
        record = references.get(locator, {})
        if not record and not locator.startswith("data:"):
            path = Path(locator.split("|", 1)[0])
            path = (path if path.is_absolute() else Path(context.cwd) / path).resolve()
            candidates = [item for item in references.values() if item.get("path") == str(path)]
            if len(candidates) == 1:
                record = candidates[0]
        result.append({"layer_index": index, "layer_key": layer.get("layer_key", ""),
            "name": layer.get("name", ""), "file_path": locator,
            "source_metadata": {key: record[key] for key in (
                "ref", "version", "producer", "state", "schema", "crs", "geometry_type",
                "feature_count", "encoding") if key in record},
            "metadata_status": "available" if record else "not_registered; inspect source"})
    return result


def save_spec(parameters: dict, *, cwd, settings) -> tuple[str, str]:
    parameters = canonical_spec(parameters)
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
    if path.resolve() != path:
        raise ValueError("Map specification is a link; use a direct workspace specification")
    if not path.is_file():
        raise ValueError("Map specification not found; copy the exact returned spec_ref")
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("Map specification exceeds the 10 MiB size limit")
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
