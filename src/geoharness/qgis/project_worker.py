"""Project operations loaded only by the standalone, native QGIS worker.

QGS XML is inspected before loading to reject external providers and unsafe links.
Projects and styles are written by QGIS itself, never synthesized as XML here.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

_VECTOR_SUFFIXES = {".gpkg", ".shp", ".geojson", ".json", ".fgb"}
_RASTER_SUFFIXES = {".tif", ".tiff", ".img", ".asc", ".dem", ".png", ".jpg", ".jpeg"}
_ASSET_SUFFIXES = {".svg", ".png", ".jpg", ".jpeg", ".webp"}
_ASSET_KEYS = {"picturepath", "svgfile", "imagefile", "name", "file", "path"}
_FONT_SUFFIXES = {".ttf", ".ttc", ".otf"}
_FONT_PROPERTIES = {"geoharness/font", "geoharness/label_font"}


def _json_digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _layout_input_digest(payload: dict[str, Any]) -> str:
    """Bind layout inputs without importing QGIS or binding random snapshot paths."""
    bound = {key: value for key, value in payload.items() if key not in {
        "action", "map_id", "output_path", "output_format", "project_format",
        "strict_layout", "_frozen_frames", "_consume_plan",
        "_creation_spec", "_layout_origin", "_layout_zoom_history",
    }}
    bound["layout"] = {key: value for key, value in (payload.get("layout") or {}).items()
                       if key != "layout_plan"}
    bindings = payload.get("_layout_sources")
    if bindings:
        if len(bindings) != len(payload.get("layers", [])):
            raise ValueError("Layout source bindings must match the original layer array")
        bound["layers"] = [{key: value for key, value in layer.items()
                            if key not in {"file_path", "layer_name"}}
                           for layer in payload.get("layers", [])]
    return _json_digest(bound)


def _runtime_identity() -> dict[str, str]:
    from qgis.core import Qgis
    from qgis.PyQt.QtCore import PYQT_VERSION_STR, QT_VERSION_STR, qVersion

    return {"qgis": Qgis.QGIS_VERSION, "qgis_version_int": str(Qgis.QGIS_VERSION_INT),
            "qt": qVersion(), "qt_build": QT_VERSION_STR, "pyqt": PYQT_VERSION_STR}


def _native_source_identities(payload: dict[str, Any], context: Any) -> list[dict[str, Any]]:
    """Bind direct native inputs and their local dependencies, without snapshot locators."""
    if payload.get("_layout_sources"):
        # The public frontend already binds guarded, self-contained frozen data.
        return []
    from osgeo import gdal

    identities = []
    for index, spec in enumerate(payload.get("layers", [])):
        path, _ = _local_source(spec["file_path"], context)
        raster = spec.get("kind", "vector") == "raster"
        dataset = gdal.OpenEx(str(path), gdal.OF_READONLY |
                             (gdal.OF_RASTER if raster else gdal.OF_VECTOR))
        if dataset is None:
            raise ValueError(f"Could not inspect native source dependencies: {path.name}")
        dependencies = {path}
        try:
            for value in dataset.GetFileList() or []:
                dependency = Path(value)
                if not dependency.is_absolute():
                    dependency = path.parent / dependency
                dependencies.add(context.read_path(str(dependency)))
        finally:
            dataset = None
        if path.suffix.lower() == ".shp":
            # Attribute/CRS/encoding sidecars are not all reported by every GDAL driver.
            sidecars = {".shx", ".dbf", ".prj", ".cpg", ".qix", ".sbn", ".sbx",
                        ".ain", ".aih", ".atx", ".ixs", ".mxs"}
            for companion in path.parent.glob(f"{path.stem}.*"):
                if companion.suffix.lower() in sidecars or (
                    companion.name.casefold() == path.name.casefold() + ".xml"
                ):
                    dependencies.add(context.read_path(str(companion)))
        if raster:
            suffix = path.suffix.lower().lstrip(".")
            world_suffix = suffix[0] + suffix[-1] + "w"
            names = {path.name.casefold() + ending for ending in (".aux.xml", ".msk", ".ovr")}
            names.update((path.stem + ending).casefold() for ending in (
                ".aux", ".prj", ".wld", "." + world_suffix, "." + suffix + "w",
                "." + world_suffix + "x"))
            for companion in path.parent.iterdir():
                if companion.name.casefold() in names and companion.is_file():
                    dependencies.add(context.read_path(str(companion)))
        files = []
        for dependency in sorted(dependencies, key=lambda item: str(item).casefold()):
            checked = context.read_path(str(dependency))
            digest = hashlib.sha256()
            with checked.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            files.append({"path": str(checked), "sha256": digest.hexdigest()})
        identities.append({"source_index": index, "files": files})
    return identities


def _native_input_specs(payload: dict[str, Any], context: Any) -> list[dict[str, Any]]:
    """Keep QGIS's automatic raster PAM caches beside private copies, not original inputs."""
    if payload.get("_layout_sources"):
        return payload.get("layers", [])
    from osgeo import gdal

    prepared = []
    for index, spec in enumerate(payload.get("layers", [])):
        if spec.get("kind", "vector") != "raster":
            prepared.append(spec)
            continue
        source, options = _local_source(spec["file_path"], context)
        if options or source.suffix.lower() not in _RASTER_SUFFIXES:
            raise ValueError("Raster sources require one local raster file")
        target = context.output_path(None, f".native-inputs/layer_{index}.tif")
        dataset = gdal.Translate(target, str(source), format="GTiff")
        if dataset is None:
            raise ValueError(f"Could not isolate native raster input: {source.name}")
        dataset.FlushCache()
        dataset = None
        prepared.append({**spec, "file_path": target, "name": spec.get("name") or source.stem})
    return prepared


def _font_identity(font: Any, info: dict[str, Any], context: Any) -> dict[str, Any]:
    """Fingerprint the actual native face as well as its selected resource bytes."""
    from qgis.PyQt.QtGui import QRawFont

    raw = QRawFont.fromFont(font)
    if not raw.isValid():
        raise ValueError("Layout font identity requires a valid native font")
    tables = hashlib.sha256()
    for name in ("head", "name", "cmap", "OS/2", "glyf", "CFF "):
        tables.update(name.encode("ascii"))
        tables.update(bytes(raw.fontTable(name)))
    identity = {"family": raw.familyName(), "style": raw.styleName(),
                "face_sha256": tables.hexdigest(), "loaded": bool(info.get("loaded"))}
    if info.get("file"):
        path = Path(info["file"])
        if info.get("packaged"):
            path = context.read_path(str(context.run_dir / path))
        identity["resource_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        identity["resource_name"] = path.suffix.lower()
    return identity


def _font_identities(layout: Any, layers: list[Any], context: Any) -> dict[str, dict]:
    from qgis.core import QgsVectorLayer
    from qgis.PyQt.QtGui import QFont

    info = json.loads(layout.customProperty("geoharness/font", "{}"))
    result = {"layout": _font_identity(QFont(info["family"]), info, context)}
    for index, layer in enumerate(layers):
        if isinstance(layer, QgsVectorLayer) and layer.labelsEnabled() and layer.labeling():
            info = json.loads(layer.customProperty("geoharness/label_font", "{}"))
            result[f"layer_{index}"] = _font_identity(
                layer.labeling().settings().format().font(), info, context)
    return result


def _map_indices(layers):
    return [i for i, layer in enumerate(layers)
            if layer.customProperty("geoharness/role", "map") != "table_only"]


def _frame_selection(name: str, spec: dict[str, Any], count: int,
                     eligible=None) -> tuple[list[int], list[int]]:
    frame = spec.get("main_frame" if name == "main_map" else "detail_frame") or {}
    render = sorted(frame.get("render_layers") or (range(count) if eligible is None else eligible))
    focus = sorted(frame.get("focus_layers") or render)
    if any(index < 0 or index >= count for index in [*render, *focus]):
        raise ValueError("Map frame layer index is outside the original layer array")
    if not set(focus) <= set(render):
        raise ValueError("Map frame focus layers must be rendered in that frame")
    if eligible is not None and not set(render) <= set(eligible):
        raise ValueError("table_only cannot be rendered in a map frame")
    return render, focus


def _focus_bbox(project: Any, layers: list[Any], indices: list[int]) -> Any:
    from qgis.core import QgsCoordinateTransform, QgsRectangle, QgsVectorLayer

    combined = None
    for index in indices:
        layer = layers[index]
        if isinstance(layer, QgsVectorLayer) and layer.featureCount() == 0:
            continue
        rect = QgsCoordinateTransform(layer.crs(), project.crs(), project).transformBoundingBox(
            layer.extent())
        if not rect.isFinite():
            raise ValueError(f"Map focus layer has a nonfinite extent: {layer.name()}")
        if combined is None:
            combined = QgsRectangle(rect)
        else:
            combined.combineExtentWith(rect)
    if combined is None:
        raise ValueError("Map frame focus layers have no usable bounds")
    return combined


def _frame_extent(project: Any, layers: list[Any], name: str, payload: dict, extent: Any) -> Any:
    from qgis.core import QgsRectangle

    spec = payload.get("layout") or {}
    frame = spec.get("main_frame" if name == "main_map" else "detail_frame") or {}
    if name == "detail_map" and spec.get("detail_extent") is not None:
        return QgsRectangle(*(spec["detail_extent"][key]
                              for key in ("xmin", "ymin", "xmax", "ymax")))
    if name == "main_map" and not frame.get("focus_layers") and not frame.get("render_layers"):
        return extent
    if name == "main_map" and payload.get("_explicit_map_extent"):
        return extent
    _, focus = _frame_selection(name, spec, len(layers), _map_indices(layers))
    bbox = _focus_bbox(project, layers, focus)
    policy = payload.get("_extent_policy") or {}
    minimum = policy.get("minimum_span_geographic" if project.crs().isGeographic()
                         else "minimum_span_projected", 0)
    width, height = max(bbox.width(), minimum), max(bbox.height(), minimum)
    if width <= 0 or height <= 0:
        raise ValueError("Degenerate frame bounds require a configured positive minimum span")
    padding = frame.get("padding_ratio")
    if padding is None:
        padding = policy.get("extent_padding", 0)
    center = bbox.center()
    rect = QgsRectangle(center.x() - width / 2, center.y() - height / 2,
                        center.x() + width / 2, center.y() + height / 2)
    rect.scale(1 + 2 * padding)
    return rect


def _rect_values(rect: Any) -> list[float]:
    return [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()]


def _plan_frames(layout: Any, project: Any, layers: list[Any], payload: dict) -> dict[str, dict]:
    from qgis.core import QgsLayoutItemMap, QgsRectangle

    spec = payload.get("layout") or {}
    result = {}
    for name in ("main_map", "detail_map"):
        item = layout.itemById(name)
        if not isinstance(item, QgsLayoutItemMap):
            continue
        render, focus = _frame_selection(name, spec, len(layers), _map_indices(layers))
        expected_layers = [layers[index].id() for index in reversed(render)]
        if [layer.id() for layer in item.layers()] != expected_layers:
            raise ValueError(f"layout_plan frame {name} did not preserve its rendered layer order")
        bbox = _focus_bbox(project, layers, focus)
        extent = item.extent()
        center = bbox.center()
        scene = item.mapRectToScene(item.rect())
        overlap = bbox.intersect(extent)
        coverage = (overlap.area() / bbox.area() if bbox.area() > 0 else
                    float(extent.contains(bbox)))
        result[name] = {"rect_mm": [scene.x(), scene.y(), scene.width(), scene.height()],
            "extent": _rect_values(extent), "crs": item.crs().authid() or item.crs().toWkt(),
            "render_layers": render, "focus_layers": focus, "focus_bbox": _rect_values(bbox),
            "normalized_center": [(center.x() - extent.xMinimum()) / extent.width(),
                                  (center.y() - extent.yMinimum()) / extent.height()],
            "coverage": coverage, "bbox_fraction": [bbox.width() / extent.width(),
                                                       bbox.height() / extent.height()],
            "page_index": item.page()}
        frame = spec.get("main_frame" if name == "main_map" else "detail_frame") or {}
        result[name]["label_layers"] = frame.get("label_layers")
    if any((spec.get(name) or {}).get("requirements") for name in ("main_frame", "detail_frame")):
        subjects = _declared_frame_evidence(project, layers, payload, result)
        for name, facts in result.items():
            declared = spec.get("main_frame" if name == "main_map" else "detail_frame") or {}
            selected = [check for check in subjects["checks"] if check["frame_id"] == name]
            facts["subject_contracts"] = selected
            bounds = [check["selected_bounds"] for check in selected if check["selected_bounds"]]
            if bounds and not declared.get("focus_layers"):
                bbox = QgsRectangle(*bounds[0])
                for value in bounds[1:]:
                    bbox.combineExtentWith(QgsRectangle(*value))
                extent = QgsRectangle(*facts["extent"])
                center = bbox.center()
                facts.update(focus_bbox=_rect_values(bbox),
                    focus_scope="declared_frame_requirements",
                    normalized_center=[(center.x() - extent.xMinimum()) / extent.width(),
                                       (center.y() - extent.yMinimum()) / extent.height()],
                    coverage=(bbox.intersect(extent).area() / bbox.area()
                              if bbox.area() > 0 else float(extent.contains(bbox))),
                    bbox_fraction=[bbox.width() / extent.width(), bbox.height() / extent.height()])
    return result


def _frame_evidence(
    project: Any, layers: list[Any], payload: dict, frames: dict,
) -> tuple[dict, dict]:
    """Count required features over the frame union and measure only drawn point labels."""
    from qgis.core import (
        NULL,
        QgsCoordinateTransform,
        QgsFeatureRequest,
        QgsGeometry,
        QgsRectangle,
        QgsVectorLayer,
        QgsWkbTypes,
    )

    # The cartography package initializer loads the agent's renderer/config stack.
    # Load this shared NumPy-only module without exposing that stack to QGIS's ABI.
    module_spec = importlib.util.spec_from_file_location("geoharness.qgis._composition",
        Path(__file__).resolve().parents[1] / "cartography" / "composition.py")
    if module_spec is None or module_spec.loader is None:
        raise ImportError("Could not load the shared composition anchor measurement")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    composition_from_anchors = module.composition_from_anchors

    visible = [set() for _ in layers]
    counts = [{} for _ in layers]
    composition = {}
    for name, frame in frames.items():
        extent = QgsRectangle(*frame["extent"])
        anchors, labeled_count = [], 0
        for index in frame["render_layers"]:
            layer = layers[index]
            transform = QgsCoordinateTransform(layer.crs(), project.crs(), project)
            if not isinstance(layer, QgsVectorLayer):
                counts[index][name] = int(extent.intersects(
                    transform.transformBoundingBox(layer.extent())))
                if counts[index][name]:
                    visible[index].add(0)
                continue
            inverse = QgsCoordinateTransform(project.crs(), layer.crs(), project)
            request = QgsFeatureRequest().setFilterRect(inverse.transformBoundingBox(extent))
            labeling = (layer.labeling().settings()
                        if layer.labelsEnabled() and layer.labeling() else None)
            if frame.get("label_layers") is not None and index not in frame["label_layers"]:
                labeling = None
            field = (labeling.fieldName
                     if labeling and labeling.drawLabels and not labeling.isExpression else None)
            layer_count = 0
            for feature in layer.getFeatures(request):
                geometry = QgsGeometry(feature.geometry())
                if geometry.isEmpty():
                    continue
                geometry.transform(transform)
                if not geometry.intersects(extent):
                    continue
                visible[index].add(feature.id())
                layer_count += 1
                if field and geometry.type() == QgsWkbTypes.PointGeometry \
                        and not geometry.isMultipart() and feature[field] != NULL:
                    labeled_count += 1
                    if len(anchors) < 512:
                        point = geometry.asPoint()
                        style = payload.get("layers", [])[index].get("style") or {}
                        sizes = [style.get("size_mm", 3)]
                        sizes.extend(entry.get("size_mm", sizes[0]) for entry in
                                     [*style.get("categories", []), *style.get("ranges", [])])
                        anchors.append({"x": point.x(), "y": point.y(), "layer_index": index,
                            "radius_mm": max(*sizes, labeling.format().size() * 25.4 / 72)})
            counts[index][name] = layer_count
        composition[name] = composition_from_anchors(anchors,
            labeled_point_count=labeled_count, bounds=frame["extent"],
            frame_mm=frame["rect_mm"][2:])
    checks = []
    for index, (layer, source) in enumerate(zip(layers, payload.get("layers", []))):
        required = source.get("required", True) and source.get("role", "map") != "table_only"
        minimum = source.get("minimum_visible_features", 1 if required else 0)
        if source.get("role") == "table_only":
            minimum = 0
        valid = len(visible[index]) >= minimum and (bool(counts[index]) or not required)
        checks.append({"source_index": index,
            "role": source.get("role", "map"),
            "layer_key": layer.customProperty("geoharness/layer_key", ""), "required": required,
            "visible_feature_count": len(visible[index]), "minimum_visible_features": minimum,
            "frames": counts[index], "valid": valid})
    frame_checks = {"scope": "union_of_rendered_map_frames", "layers": checks,
                    "valid": all(check["valid"] for check in checks if check["required"]),
                    "warnings": [{"code": ("optional_layer_not_visible"
                        if check["visible_feature_count"] == 0 else
                        "optional_layer_below_minimum_visibility"),
                        "source_index": check["source_index"],
                        "visible_feature_count": check["visible_feature_count"],
                        "minimum_visible_features": check["minimum_visible_features"]}
                        for check in checks if check["role"] == "map" and not check["required"] and
                        (not check["valid"] or check["visible_feature_count"] == 0)]}
    if not frame_checks["valid"]:
        error = ValueError("Required layer visibility failed across the rendered frame union")
        error.diagnostics = {"frame_checks": frame_checks}
        raise error
    frame_checks["subjects"] = _declared_frame_evidence(project, layers, payload, frames)
    if frame_checks["subjects"]["checked"]:
        frame_checks["scope"] = "union_and_declared_frame_subjects"
        frame_checks["by_frame"] = {name: {
            "valid": all(check["valid"] for check in frame_checks["subjects"]["checks"]
                         if check["frame_id"] == name),
            "requirements": [check for check in frame_checks["subjects"]["checks"]
                             if check["frame_id"] == name]}
            for name in frames}
    frame_checks["valid"] = frame_checks["valid"] and frame_checks["subjects"]["valid"]
    if not frame_checks["valid"]:
        error = ValueError("Declared map-frame subjects are missing or incompletely covered")
        error.diagnostics = {"code": "frame_subject_contract_failed", "frame_checks": frame_checks}
        raise error
    return {**composition.get("main_map", {}), "map_frames": composition}, frame_checks


def _declared_frame_evidence(project: Any, layers: list[Any], payload: dict, frames: dict) -> dict:
    """Measure each declared frame's own subjects, independent of the union fallback."""
    from qgis.core import NULL, QgsCoordinateTransform, QgsGeometry, QgsRectangle, QgsVectorLayer

    from geoharness.qgis.frame_contracts import evaluate_requirements

    measurements = []
    layout_spec = payload.get("layout") or {}
    for frame_id, field in (("main_map", "main_frame"), ("detail_map", "detail_frame")):
        declared = layout_spec.get(field) or {}
        frame = frames.get(frame_id)
        for requirement in declared.get("requirements", []):
            index = requirement["layer_index"]
            base = {**requirement, "frame_id": frame_id, "purpose": declared.get("purpose", ""),
                "frame_present": frame is not None, "rendered": False, "selected_features": 0,
                "visible_features": 0, "complete_features": 0, "missing_keys": [],
                "duplicate_keys": [], "selected_bounds": None}
            if not frame or index < 0 or index >= len(layers):
                measurements.append(base)
                continue
            base["rendered"] = index in frame["render_layers"]
            layer = layers[index]
            base["layer_key"] = layer.customProperty("geoharness/layer_key", "")
            base["layer_name"] = layer.name()
            if not base["rendered"]:
                measurements.append(base)
                continue
            extent = QgsRectangle(*frame["extent"])
            transform = QgsCoordinateTransform(layer.crs(), project.crs(), project)
            if not isinstance(layer, QgsVectorLayer):
                if requirement.get("feature_keys") is not None:
                    error = ValueError("Frame feature_keys apply only to vector sources")
                    error.diagnostics = {"code": "frame_selector_kind", "frame_id": frame_id,
                                         "layer_index": index, "kind": "raster"}
                    raise error
                bounds = transform.transformBoundingBox(layer.extent())
                base.update(selected_features=1, visible_features=int(extent.intersects(bounds)),
                            complete_features=int(extent.contains(bounds)),
                            selected_bounds=_rect_values(bounds))
                measurements.append(base)
                continue
            key_field = requirement.get("key_field", "")
            keys = requirement.get("feature_keys")
            if keys is not None and (not key_field or key_field not in layer.fields().names()):
                error = ValueError(f"Frame subject key field is missing: {frame_id}.{key_field}")
                error.diagnostics = {"code": "frame_selector_field", "frame_id": frame_id,
                    "layer_index": index, "key_field": key_field,
                    "available_fields": layer.fields().names()}
                raise error
            def token(value):
                return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            wanted = {token(value): value for value in keys} if keys is not None else None
            found = {}
            for feature in layer.getFeatures():
                key = feature[key_field] if wanted is not None else None
                if wanted is not None:
                    if key == NULL or token(key) not in wanted:
                        continue
                    identity = token(key)
                    found[identity] = found.get(identity, 0) + 1
                geometry = QgsGeometry(feature.geometry())
                if geometry.isEmpty():
                    continue
                geometry.transform(transform)
                bounds = geometry.boundingBox()
                combined = QgsRectangle(*base["selected_bounds"]) if base["selected_bounds"] else (
                    QgsRectangle(bounds))
                combined.combineExtentWith(bounds)
                base["selected_bounds"] = _rect_values(combined)
                base["selected_features"] += 1
                base["visible_features"] += int(geometry.intersects(extent))
                base["complete_features"] += int(extent.contains(geometry.boundingBox()))
            if wanted is not None:
                base["missing_keys"] = [value for key, value in wanted.items() if key not in found]
                base["duplicate_keys"] = [wanted[key] for key, count in found.items() if count > 1]
            measurements.append(base)
    return evaluate_requirements(measurements)


def _freeze_layout_plan(layout: Any, project: Any, layers: list[Any], payload: dict,
                        context: Any) -> dict:
    metadata = _layout_metadata(layout)
    plan = {"schema_version": 1, "input_digest": _layout_input_digest(payload),
        "runtime_identity": _runtime_identity(),
        "source_identities": _native_source_identities(payload, context),
        "font_identities": _font_identities(layout, layers, context),
        "parameters": metadata["creation_parameters"], "regions": metadata["regions"],
        "frames": _plan_frames(layout, project, layers, payload),
        "content_metrics": metadata["bounds_check"]["content_metrics"],
        "adaptations": metadata["adaptations"]}
    plan["plan_digest"] = _json_digest(plan)
    return plan


def _same_plan_geometry(actual: Any, expected: Any, *, abs_tol: float = 1e-6) -> bool:
    """QGIS project serialization rounds native floating coordinates slightly."""
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        return math.isclose(actual, expected, rel_tol=1e-9, abs_tol=abs_tol)
    if isinstance(actual, list) and isinstance(expected, list):
        return len(actual) == len(expected) and all(
            _same_plan_geometry(a, e, abs_tol=abs_tol) for a, e in zip(actual, expected))
    if isinstance(actual, dict) and isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(
            _same_plan_geometry(actual[key], expected[key],
                abs_tol=0.001 if key in {"rect_mm", "available_mm"} else abs_tol)
            for key in actual)
    return actual == expected


def _validate_plan_layout(layout: Any, project: Any, layers: list[Any], payload: dict,
                          plan: dict, context: Any) -> None:
    metadata = _layout_metadata(layout)
    if _font_identities(layout, layers, context) != plan["font_identities"]:
        raise ValueError("layout_plan is stale: font resource or native face changed; "
                         "run preflight again")
    for key, actual in (("parameters", metadata["creation_parameters"]),
                        ("regions", metadata["regions"]),
                        ("frames", _plan_frames(layout, project, layers, payload)),
                        ("content_metrics", metadata["bounds_check"]["content_metrics"])):
        if not _same_plan_geometry(actual, plan[key]):
            error = ValueError(f"layout_plan is stale: native {key} changed; run preflight again")
            error.diagnostics = {"field": key, "planned": plan[key], "measured": actual}
            raise error


def _local_source(source: str, context: Any, *, base: Path | None = None) -> tuple[Path, str]:
    """Accept only file paths and the two non-executable OGR sublayer selectors."""
    if re.search(r"(?i)(://|/vsi|authcfg\s*=|password\s*=|dbname\s*=)", source):
        raise ValueError("QGIS projects accept local files only; remote/credential sources denied")
    path_value, *options = source.split("|")
    for option in options:
        if not re.fullmatch(r"(?:layername=[^|]+|layerid=\d+)", option):
            raise ValueError("Only layername/layerid selectors are supported for local data")
    path = Path(path_value)
    if base is not None and not path.is_absolute():
        path = base / path
    checked = context.read_path(str(path))
    if checked.suffix.lower() not in _VECTOR_SUFFIXES | _RASTER_SUFFIXES:
        raise ValueError(
            "Unsupported local source format (VRT/remote indirection is not supported)"
        )
    return checked, "|".join(options)


def _project_xml(path: Path) -> ET.Element:
    if path.suffix.lower() == ".qgz":
        with zipfile.ZipFile(path) as archive:
            members = [
                item for item in archive.infolist() if item.filename.lower().endswith(".qgs")
            ]
            if len(members) != 1 or members[0].file_size > 32 * 1024 * 1024:
                raise ValueError("QGZ must contain one bounded QGS document")
            content = archive.read(members[0])
    elif path.suffix.lower() == ".qgs":
        if path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("QGS exceeds the supported document size")
        content = path.read_bytes()
    else:
        raise ValueError("project_path must be a local .qgs or .qgz")
    if b"<!ENTITY" in content.upper():
        raise ValueError("XML entities are not supported in project files")
    return ET.fromstring(content)


def _builtin_svg_roots() -> list[Path]:
    """Use QGIS search paths, admitting only the runtime's own SVG directories."""
    from qgis.core import QgsApplication

    official = {
        (Path(value) / "svg").resolve()
        for value in (QgsApplication.prefixPath(), QgsApplication.pkgDataPath())
        if value
    }
    return list(
        dict.fromkeys(
            Path(value).resolve()
            for value in QgsApplication.svgPaths()
            if Path(value).resolve() in official
        )
    )


def _resolve_asset(value: str, project_path: Path, context: Any) -> Path:
    """Project-local resources retain normal read guards; builtins allow SVGs only."""
    if re.search(r"(?i)(://|/vsi)", value) or value.startswith(("\\\\", "//")):
        raise ValueError("Remote layout/symbol resources are not supported")
    asset = Path(value)
    local = asset if asset.is_absolute() else project_path.parent / asset
    try:
        return context.read_path(str(local))
    except (ValueError, PermissionError):
        if asset.suffix.lower() != ".svg":
            raise
        for root in _builtin_svg_roots():
            candidate = (asset if asset.is_absolute() else root / asset).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                return candidate
        raise


def _preflight_project(
    path: Path,
    context: Any,
    asset_sources: dict[str, Path] | None = None,
) -> list[Path]:
    """Check linked files before QgsProject.read can open providers or pictures."""
    root = _project_xml(path)
    if root.tag != "qgis":
        raise ValueError("Expected a QGIS project document")
    if any(item.get("embedded") == "1" for item in root.iter()):
        raise ValueError("Embedded external projects are not supported")
    for layer in root.findall("./projectlayers/maplayer"):
        provider = (layer.findtext("provider") or "").strip()
        kind = layer.get("type")
        if (kind, provider) not in {("vector", "ogr"), ("raster", "gdal")}:
            raise ValueError("Only local OGR vector and GDAL raster project layers are supported")
        _local_source(layer.findtext("datasource") or "", context, base=path.parent)
    assets: list[Path] = []
    for font in _project_font_resources(root, path, context).values():
        if font not in assets:
            assets.append(font)
    for item in root.iter():
        if item.tag.lower() in {"pythonmacros", "pythonfunctions", "pythoncode"}:
            if "".join(item.itertext()).strip():
                raise ValueError("Project Python macros/functions are not supported")
        for key, value in item.attrib.items():
            if key == "value":
                semantic_key = item.get("name", "")
            elif key == "v" and item.tag.lower() == "prop":
                semantic_key = item.get("k", "")
            elif key in {"name", "k"} and item.tag.lower() in {"option", "prop"}:
                continue  # These attributes are property keys, not resource values.
            else:
                semantic_key = key
            if semantic_key.lower() not in _ASSET_KEYS or not value:
                continue
            if re.search(r"(?i)(://|/vsi)", value):
                raise ValueError("Remote layout/symbol resources are not supported")
            if Path(value).suffix.lower() in _ASSET_SUFFIXES:
                asset = _resolve_asset(value, path, context)
                if asset_sources is not None:
                    asset_sources[value] = asset
                if asset not in assets:
                    assets.append(asset)
        if item.get("htmlState") == "1" or item.get("htmlMode") == "1":
            content = item.get("labelText", "") + "".join(item.itertext())
            if re.search(r"(?i)<\s*(?:img|iframe|object)|url\s*\(", content):
                raise ValueError("HTML labels with external resources are not supported")
    return assets


def _project_font_resources(root: ET.Element, project_path: Path, context: Any) -> dict[str, Path]:
    """Only declared packaged font resources are admitted, before native project read."""
    resources = {}
    for item in root.iter():
        if item.get("name") not in _FONT_PROPERTIES or "value" not in item.attrib:
            continue
        info = json.loads(item.attrib["value"])
        if not isinstance(info, dict) or not info.get("packaged"):
            continue
        value = info.get("file", "")
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts or (
            relative.suffix.lower() not in _FONT_SUFFIXES
        ):
            raise ValueError("Packaged font resources must be project-relative TTF, TTC or OTF")
        font = context.read_path(str(project_path.parent / relative))
        if not font.is_relative_to(project_path.parent.resolve()):
            raise PermissionError("Packaged font must remain inside its project directory")
        expected = info.get("sha256")
        if expected and hashlib.sha256(font.read_bytes()).hexdigest() != expected:
            raise ValueError("Packaged font checksum does not match its saved metadata")
        resources[value] = font
    return resources


def _register_project_fonts(project_path: Path, context: Any) -> None:
    from qgis.PyQt.QtGui import QFontDatabase

    database = QFontDatabase()
    for path in _project_font_resources(_project_xml(project_path), project_path, context).values():
        if database.addApplicationFont(str(path)) < 0:
            raise ValueError(f"QGIS could not register packaged font: {path.name}")


def _snapshot_font(path: Path, context: Any) -> tuple[Path, str]:
    """Snapshot only explicitly selected fonts; a content hash deduplicates layers/layouts."""
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    relative = f"assets/fonts/{digest}{path.suffix.lower()}"
    target = context.run_dir / relative
    if not target.exists():
        target = Path(context.output_path(None, relative))
        shutil.copy2(path, target)
    elif hashlib.sha256(context.read_path(str(target)).read_bytes()).hexdigest() != digest:
        raise ValueError("Existing packaged font does not match its resource hash")
    return target, digest


def _restore_font_metadata(
    project: Any, source_project: Path, context: Any, *, package: bool
) -> None:
    """Record actual font registration and relocate resource metadata for new packages."""
    from qgis.PyQt.QtGui import QFont, QRawFont

    owners = [*project.mapLayers().values(), *project.layoutManager().printLayouts()]
    resources = _project_font_resources(_project_xml(source_project), source_project, context)
    for owner in owners:
        for key in _FONT_PROPERTIES:
            value = owner.customProperty(key, "")
            if not value:
                continue
            info = json.loads(value)
            if not info.get("packaged"):
                continue
            path = resources[info["file"]]
            raw = QRawFont.fromFont(QFont(info["family"]))
            loaded = raw.isValid() and all(raw.supportsCharacter(char) for char in "地图说明")
            if not loaded:
                raise ValueError("Saved project's packaged Chinese font could not be restored")
            if package:
                path, digest = _snapshot_font(path, context)
                info.update(file=path.relative_to(context.run_dir).as_posix(), sha256=digest)
            info.update(loaded=loaded, registered=True)
            owner.setCustomProperty(key, json.dumps(info))


def _color(value: str) -> Any:
    from qgis.PyQt.QtGui import QColor

    color = QColor(value)
    if not color.isValid():
        raise ValueError(f"Invalid symbol color: {value}")
    return color


def _font(spec: dict[str, Any], context: Any) -> tuple[Any, dict[str, Any]]:
    """Load configured fonts, or the OS's ordinary static CJK font collection."""
    from qgis.PyQt.QtGui import QFont, QFontDatabase, QRawFont

    database = QFontDatabase()
    requested = spec.get("font_family", "Microsoft YaHei")
    configured = spec.get("font_file")
    font_file = ""
    packaged = False
    digest = ""
    if configured:
        path = context.read_path(configured)
        if path.suffix.lower() not in _FONT_SUFFIXES:
            raise ValueError("font_file must be a local TTF, TTC or OTF font")
        index = database.addApplicationFont(str(path))
        families = database.applicationFontFamilies(index) if index >= 0 else []
        if not families:
            raise ValueError(f"QGIS could not load configured font: {path.name}")
        requested = requested if requested in families else families[0]
        path, digest = _snapshot_font(path, context)
        font_file = path.relative_to(context.run_dir).as_posix()
        packaged = True
    else:
        # Native worker bootstrap registers system fonts; these bounded candidates
        # also make the selected file verifiable without recording a host path in code.
        system_root = os.environ.get("WINDIR", os.environ.get("SystemRoot", ""))
        if system_root:
            root = Path(system_root) / "Fonts"
            for filename in ("msyh.ttc", "simhei.ttf", "simsun.ttc"):
                path = root / filename
                if not path.is_file():
                    continue
                index = database.addApplicationFont(str(path))
                families = database.applicationFontFamilies(index) if index >= 0 else []
                if requested in families or requested not in database.families():
                    if families:
                        requested = requested if requested in families else families[0]
                        font_file = str(path)
                        break
    available = database.families()
    if requested not in available:
        requested = next(
            (name for name in ("Microsoft YaHei", "微软雅黑", "SimHei", "Noto Sans CJK SC")
             if name in available),
            requested,
        )
    font = QFont(requested)
    raw = QRawFont.fromFont(font)
    loaded = raw.isValid() and all(raw.supportsCharacter(char) for char in "地图说明")
    if not loaded:
        raise ValueError("QGIS requires a loaded Chinese font; configure font_file/font_family")
    info = {
        "family": font.family(), "file": font_file, "loaded": loaded,
        "packaged": packaged, "registered": True,
    }
    if digest:
        info["sha256"] = digest
    return font, info


def _text_format(font: Any, size: float, color: str = "#222222") -> Any:
    from qgis.core import Qgis, QgsTextFormat

    result = QgsTextFormat()
    result.setFont(font)
    result.setSize(size)
    result.setSizeUnit(Qgis.RenderUnit.Points)
    result.setColor(_color(color))
    return result


def _symbol(layer: Any, style: dict[str, Any], entry: dict[str, Any]) -> Any:
    from qgis.core import Qgis, QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol, QgsSymbol

    symbol = QgsSymbol.defaultSymbol(layer.geometryType())
    if symbol is None:
        raise ValueError("Project maps require a spatial vector layer")
    color = _color(entry.get("color", style.get("color", "#4c78a8")))
    if isinstance(symbol, (QgsMarkerSymbol, QgsFillSymbol)):
        color.setAlphaF(color.alphaF() * float(style.get("fill_opacity", 1.0)))
    symbol.setColor(color)
    width = entry.get("width_mm", style.get("width_mm", 0.3))
    size = entry.get("size_mm", style.get("size_mm", 3))
    if isinstance(symbol, QgsMarkerSymbol):
        symbol.setSize(size)
        symbol.setSizeUnit(Qgis.RenderUnit.Millimeters)
    elif isinstance(symbol, QgsLineSymbol):
        symbol.setWidth(width)
        symbol.setWidthUnit(Qgis.RenderUnit.Millimeters)
    for symbol_layer in symbol.symbolLayers():
        if isinstance(symbol, (QgsMarkerSymbol, QgsFillSymbol)):
            symbol_layer.setStrokeColor(_color(style.get("outline_color", "#ffffff")))
            symbol_layer.setStrokeWidth(width)
            symbol_layer.setStrokeWidthUnit(Qgis.RenderUnit.Millimeters)
    return symbol


def _symbol_metadata(symbol: Any) -> dict[str, Any]:
    from qgis.core import Qgis, QgsFillSymbol, QgsLineSymbol, QgsLineSymbolLayer, QgsMarkerSymbol

    color = symbol.color()
    result = {"color": color.name(), "color_alpha": color.alphaF(),
              "symbol_opacity": symbol.opacity(), "symbol_layer_count": symbol.symbolLayerCount()}
    result["data_defined"] = any(
        layer.hasDataDefinedProperties() for layer in symbol.symbolLayers())
    if isinstance(symbol, (QgsMarkerSymbol, QgsFillSymbol)):
        result["fill_opacity"] = color.alphaF()
    if isinstance(symbol, QgsMarkerSymbol):
        result.update(size=symbol.size(), size_unit=symbol.sizeUnit().name)
        if symbol.sizeUnit() == Qgis.RenderUnit.Millimeters and not result["data_defined"]:
            result["size_mm"] = symbol.size()
    if isinstance(symbol, QgsLineSymbol):
        # Line width units live on symbol layers. outputUnit() also includes
        # unrelated units, such as the line offset, so cannot describe width.
        width_units = [
            layer.widthUnit() if isinstance(layer, QgsLineSymbolLayer) else Qgis.RenderUnit.Unknown
            for layer in symbol.symbolLayers()
        ]
        width_unit = (width_units[0] if width_units and len(set(width_units)) == 1
                      else Qgis.RenderUnit.Unknown)
        result.update(width=symbol.width(), width_unit=width_unit.name)
        if width_unit == Qgis.RenderUnit.Millimeters and not result["data_defined"]:
            result["width_mm"] = symbol.width()
    elif not isinstance(symbol, QgsLineSymbol):
        first = symbol.symbolLayer(0)
        stroke_width = getattr(first, "strokeWidth", None)
        stroke_unit = getattr(first, "strokeWidthUnit", None)
        if callable(stroke_width) and callable(stroke_unit):
            result.update(width=stroke_width(), width_unit=stroke_unit().name)
            if stroke_unit() == Qgis.RenderUnit.Millimeters and not result["data_defined"]:
                result["width_mm"] = stroke_width()
        stroke_color = getattr(first, "strokeColor", None)
        if callable(stroke_color):
            outline = stroke_color()
            result.update(outline_color=outline.name(), outline_alpha=outline.alphaF())
        if first.layerType() == "SimpleMarker" and symbol.symbolLayerCount() == 1 and \
                not result["data_defined"]:
            result["marker_shape"] = first.properties().get("name")
    return result


def _style_vector(layer: Any, spec: dict[str, Any], context: Any) -> None:
    from qgis.core import (
        Qgis,
        QgsCategorizedSymbolRenderer,
        QgsGraduatedSymbolRenderer,
        QgsPalLayerSettings,
        QgsRendererCategory,
        QgsRendererRange,
        QgsSingleSymbolRenderer,
        QgsTextBufferSettings,
        QgsVectorLayerSimpleLabeling,
    )

    style = spec.get("style") or {}
    renderer = style.get("renderer", "single")
    if renderer in {"categorized", "graduated"}:
        field = style.get("field", "")
        if layer.fields().indexFromName(field) < 0:
            raise ValueError(f"Renderer field does not exist: {field}")
    if renderer == "categorized":
        categories = []
        for category in style.get("categories", []):
            symbol = _symbol(layer, style, category)
            value = category["value"]
            label = category.get("label") or ("NULL" if value is None else str(value))
            categories.append(QgsRendererCategory(value, symbol, label))
        if not categories:
            raise ValueError("Categorized styling requires explicit categories/colors")
        layer.setRenderer(QgsCategorizedSymbolRenderer(field, categories))
    elif renderer == "graduated":
        ranges = [
            QgsRendererRange(
                item["lower"], item["upper"], _symbol(layer, style, item),
                item.get("label") or f"{item['lower']:g} – {item['upper']:g}",
            )
            for item in style.get("ranges", [])
        ]
        if not ranges:
            raise ValueError("Graduated styling requires explicit ranges/colors")
        layer.setRenderer(QgsGraduatedSymbolRenderer(field, ranges))
    elif renderer == "single":
        layer.setRenderer(QgsSingleSymbolRenderer(_symbol(layer, style, {})))
    else:
        raise ValueError("Unsupported vector renderer")
    layer.setOpacity(float(style.get("opacity", 1.0)))
    field = spec.get("label_field")
    if field or style.get("obstacle") is True:
        if field and layer.fields().indexFromName(field) < 0:
            raise ValueError(f"Label field does not exist: {field}")
        settings = QgsPalLayerSettings()
        settings.fieldName = field or ""
        settings.drawLabels = bool(field)
        label_spec = spec.get("label_style") or {}
        font, font_info = _font(label_spec, context)
        text_format = _text_format(
            font, label_spec.get("size_pt", 9), label_spec.get("color", "#222222")
        )
        buffer = QgsTextBufferSettings()
        buffer.setEnabled(label_spec.get("buffer_size_mm", 0.8) > 0)
        buffer.setSize(label_spec.get("buffer_size_mm", 0.8))
        buffer.setSizeUnit(Qgis.RenderUnit.Millimeters)
        buffer.setColor(_color(label_spec.get("buffer_color", "#ffffff")))
        text_format.setBuffer(buffer)
        settings.setFormat(text_format)
        positions = {
            "around_point": Qgis.LabelPlacement.OrderedPositionsAroundPoint,
            "over_point": Qgis.LabelPlacement.OverPoint,
            "parallel": Qgis.LabelPlacement.Line,
            "horizontal": Qgis.LabelPlacement.Horizontal,
        }
        position = label_spec.get("placement", "auto")
        if position == "auto":
            position = {Qgis.GeometryType.Point: "around_point",
                        Qgis.GeometryType.Line: "parallel"}.get(
                            layer.geometryType(), "horizontal")
        if position in {"around_point", "over_point"} \
                and layer.geometryType() != Qgis.GeometryType.Point:
            raise ValueError("Point label placement requires point geometry")
        if position == "parallel" and layer.geometryType() != Qgis.GeometryType.Line:
            raise ValueError("parallel label placement requires line geometry")
        settings.placement = positions[position]
        settings.dist = label_spec.get("distance_mm", 1.5)
        settings.distUnits = Qgis.RenderUnit.Millimeters
        settings.xOffset = label_spec.get("offset_x_mm", 0)
        settings.yOffset = label_spec.get("offset_y_mm", 0)
        settings.offsetUnits = Qgis.RenderUnit.Millimeters
        settings.priority = int(label_spec.get("priority", 5))
        placement = settings.placementSettings()
        placement.setOverlapHandling(Qgis.LabelOverlapHandling.PreventOverlap)
        placement.setAllowDegradedPlacement(False)
        settings.setPlacementSettings(placement)
        settings.displayAll = False
        obstacle = settings.obstacleSettings()
        obstacle.setIsObstacle(style.get("obstacle") is not False)
        obstacle.setFactor(style.get("obstacle_factor", 1))
        settings.setObstacleSettings(obstacle)
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)
        layer.setCustomProperty("geoharness/label_font", json.dumps(font_info))


def _new_layer(spec: dict[str, Any], context: Any) -> Any:
    from qgis.core import QgsRasterLayer, QgsVectorLayer

    path, options = _local_source(spec["file_path"], context)
    kind = spec.get("kind", "vector")
    name = spec.get("name") or path.stem
    if kind == "vector":
        if path.suffix.lower() not in _VECTOR_SUFFIXES:
            raise ValueError("Vector sources require GPKG, SHP, GeoJSON or FlatGeobuf")
        layer_name = spec.get("layer_name")
        if layer_name:
            if "|" in layer_name:
                raise ValueError("layer_name cannot contain OGR URI separators")
            options = f"layername={layer_name}"
        uri = str(path) + ("|" + options if options else "")
        layer = QgsVectorLayer(uri, name, "ogr")
    else:
        if path.suffix.lower() not in _RASTER_SUFFIXES or options:
            raise ValueError("Raster sources require one local raster file")
        layer = QgsRasterLayer(str(path), name, "gdal")
    if not layer.isValid():
        raise ValueError(f"QGIS could not load local layer: {name}")
    if not layer.crs().isValid():
        raise ValueError(f"Layer has no valid declared CRS: {name}")
    if kind == "vector":
        _style_vector(layer, spec, context)
        expected = spec.get("expected_feature_count")
        if expected is not None and layer.featureCount() != expected:
            raise ValueError(f"Expected feature count does not match native layer: {name}")
    else:
        style = spec.get("raster_style") or {}
        if style.get("renderer", "default") == "hillshade":
            from qgis.core import QgsHillshadeRenderer

            band = style.get("band", 1)
            if not 1 <= band <= layer.bandCount():
                raise ValueError(f"Hillshade band {band} does not exist: {name}")
            layer.setRenderer(QgsHillshadeRenderer(
                layer.dataProvider(), band, style.get("azimuth", 315), style.get("altitude", 45),
            ))
    return layer


def _snapshot_layer(layer: Any, index: int, project: Any, context: Any) -> dict[str, Any]:
    from qgis.core import QgsMapLayerStyle, QgsRasterLayer, QgsVectorFileWriter, QgsVectorLayer

    saved_style = QgsMapLayerStyle()
    saved_style.readFromLayer(layer)
    if isinstance(layer, QgsVectorLayer):
        target = context.output_path(None, f"data/layer_{index}.gpkg")
        options = QgsVectorFileWriter.SaveVectorOptions()
        options.driverName = "GPKG"
        options.layerName = f"layer_{index}"
        options.fileEncoding = "UTF-8"
        primary_key = layer.customProperty("geoharness/snapshot_primary_key", "")
        business_fields = layer.customProperty("geoharness/business_fields", []) or []
        if primary_key:
            if primary_key in business_fields:
                raise ValueError("Internal snapshot key collides with a business field")
            options.attributes = [field_index for field_index, field in enumerate(layer.fields())
                                  if field.name() != primary_key]
            options.skipAttributeCreation = not options.attributes
            geometry_name = "_geoharness_geometry"
            occupied = {field.casefold() for field in business_fields} | {primary_key.casefold()}
            while geometry_name.casefold() in occupied:
                geometry_name += "_"
            options.layerOptions = ["FID=" + primary_key, "GEOMETRY_NAME=" + geometry_name]
        result = QgsVectorFileWriter.writeAsVectorFormatV3(
            layer,
            target,
            project.transformContext(),
            options,
        )
        if result[0] != QgsVectorFileWriter.NoError:
            raise ValueError(f"Could not snapshot vector layer {layer.name()}: {result[1]}")
        layer.setDataSource(target + f"|layername=layer_{index}", layer.name(), "ogr", False)
    elif isinstance(layer, QgsRasterLayer):
        from osgeo import gdal

        target = context.output_path(None, f"data/layer_{index}.tif")
        source, _ = _local_source(layer.source(), context)
        dataset = gdal.Translate(target, str(source), format="GTiff")
        if dataset is None:
            raise ValueError(f"Could not snapshot raster layer: {layer.name()}")
        dataset.FlushCache()
        dataset = None
        layer.setDataSource(target, layer.name(), "gdal", False)
    else:
        raise ValueError("Only vector and raster snapshots are supported")
    if not layer.isValid():
        raise ValueError(f"Packaged layer is invalid: {layer.name()}")
    saved_style.writeToLayer(layer)
    style_path = context.output_path(None, f"styles/layer_{index}.qml")
    message, success = layer.saveNamedStyle(style_path)
    if not success:
        raise ValueError(f"Could not save layer style: {message}")
    return {
        **_layer_metadata(layer),
        "file": Path(target).relative_to(context.run_dir).as_posix(),
        "style_file": Path(style_path).relative_to(context.run_dir).as_posix(),
    }


def _extent(project: Any, layers: list[Any], requested: list[float] | None) -> Any:
    from qgis.core import QgsCoordinateTransform, QgsRectangle

    if requested is not None:
        return QgsRectangle(*requested)
    combined = QgsRectangle()
    for layer in layers:
        if layer.customProperty("geoharness/role", "map") == "table_only":
            continue
        rect = QgsCoordinateTransform(
            layer.crs(),
            project.crs(),
            project,
        ).transformBoundingBox(layer.extent())
        if rect.isFinite():
            combined.combineExtentWith(rect)
    if combined.isNull() or not combined.isFinite():
        raise ValueError("Layers have no usable extent; provide a positive map_extent")
    if combined.width() == 0 or combined.height() == 0:
        raise ValueError("Point/line extent is degenerate; provide a positive map_extent")
    combined.scale(1.05)
    return combined


def _layout_regions(spec: dict[str, Any], title: str) -> dict[str, list[float]]:
    """Allocate millimetre rectangles from the measured, effective layout parameters."""
    width, height = spec.get("width_mm", 297), spec.get("height_mm", 210)
    margin, gap = spec.get("margin_mm", 12), spec.get("gap_mm", 5)
    inner_width = width - 2 * margin
    title_height = spec.get("title_height_mm", 16) if title else 0
    footer_height = spec.get("footer_height_mm", 18)
    top = margin + title_height + (gap if title else 0)
    footer_y = height - margin - footer_height
    map_height = footer_y - gap - top
    table = spec.get("label_table")
    if table and table.get("placement") != "pages":
        map_height -= table.get("height_mm", 50) + gap
    has_detail = spec.get("detail_extent") is not None or spec.get("detail_frame") is not None
    separate_detail = has_detail and spec.get("detail_placement", "sidebar") == "page"
    sidebar = spec.get("legend", True) or (has_detail and not separate_detail)
    sidebar_width = spec.get("sidebar_width_mm", 60) if sidebar else 0
    map_width = inner_width - sidebar_width - (gap if sidebar else 0)
    if map_width < 40 or map_height < 40:
        raise ValueError("Structured layout requires at least 40mm for main map width/height")
    regions = {
        "main_map": [margin, top, map_width, map_height],
        "footer": [margin, footer_y, inner_width, footer_height],
    }
    if title:
        regions["title"] = [margin, margin, inner_width, title_height]
    if table and table.get("placement") != "pages":
        count = table.get("columns", 3)
        column_width = (inner_width - gap * (count - 1)) / count
        for index in range(count):
            regions[f"label_table_{index}"] = [margin + index * (column_width + gap),
                footer_y - gap - table.get("height_mm", 50), column_width,
                table.get("height_mm", 50)]
    sidebar_x = width - margin - sidebar_width
    if separate_detail:
        detail_spec = {**spec, "detail_extent": None, "detail_frame": None, "label_table": None}
        detail_title = spec.get("detail_title") or (title + " · 详图" if title else "详图")
        regions["detail_map"] = _layout_regions(detail_spec, detail_title)["main_map"]
    elif has_detail:
        detail_height = (map_height * spec.get("detail_height_ratio", 0.4)
                         if spec.get("legend", True) else map_height)
        caption = spec.get("title_height_mm", 16) if spec.get("detail_title") else 0
        if caption:
            regions["detail_title"] = [sidebar_x, top, sidebar_width, caption]
        regions["detail_map"] = [sidebar_x, top + caption + (gap if caption else 0),
                                 sidebar_width, detail_height - caption - (gap if caption else 0)]
        top += detail_height + gap
    if spec.get("legend", True):
        legend_height = regions["main_map"][1] + map_height - top
        if legend_height < 20:
            raise ValueError("Structured layout requires at least 20mm for the legend")
        regions["legend"] = [sidebar_x, top, sidebar_width, legend_height]
    return regions


def _place_item(layout: Any, item: Any, name: str, rect: list[float]) -> None:
    from qgis.core import QgsLayoutPoint, QgsLayoutSize

    item.setId(name)
    layout.addLayoutItem(item)
    item.attemptMove(QgsLayoutPoint(rect[0], rect[1]))
    item.attemptResize(QgsLayoutSize(rect[2], rect[3]))


def _create_layout(
    project: Any, layers: list[Any], extent: Any, payload: dict[str, Any], context: Any,
    *, _fit_attempt: int = 0, _adaptations: list[dict[str, Any]] | None = None,
    _fit_states: frozenset[str] = frozenset(),
) -> Any:
    from qgis.core import (
        Qgis,
        QgsBasicNumericFormat,
        QgsLayoutItemAttributeTable,
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemMap,
        QgsLayoutItemPage,
        QgsLayoutItemScaleBar,
        QgsLayoutSize,
        QgsLegendRenderer,
        QgsPrintLayout,
        QgsRectangle,
    )
    from qgis.PyQt.QtCore import Qt

    spec = {key: value for key, value in (payload.get("layout") or {}).items()
            if key != "layout_plan"}
    payload = {**payload, "_layout_origin": payload.get("_layout_origin") or spec}
    from geoharness.qgis.frame_contracts import paper_limits

    limits = paper_limits(payload["_layout_origin"], spec)
    if not limits["within_limits"]:
        error = ValueError("Joint layout exceeds its declared automatic paper expansion limits")
        error.diagnostics = {"code": "paper_expansion_limit", "limits": limits,
            "proposed_mm": [spec.get("width_mm", 297), spec.get("height_mm", 210)],
            "remedy": "Keep complete subject coverage; use detail_placement='page' or "
                      "label_table.placement='pages', or explicitly choose a larger page."}
        raise error
    state = _json_digest(spec)
    if state in _fit_states or _fit_attempt > 12:
        raise ValueError("Native layout sizing did not converge; retain subject coverage and "
                         "use a larger or separate detail map")
    fit_states = _fit_states | {state}
    width, height = spec.get("width_mm", 297), spec.get("height_mm", 210)
    title = payload.get("title", "")
    regions = _layout_regions(spec, title)
    font, font_info = _font(spec, context)
    body_format = _text_format(font, spec.get("body_font_size_pt", 9))
    layout = QgsPrintLayout(project)
    layout.initializeDefaults()
    layout.setName(payload.get("layout_name") or spec.get("name", "Map"))
    layout.pageCollection().page(0).setPageSize(QgsLayoutSize(width, height))
    if spec.get("detail_placement", "sidebar") == "page" and "detail_map" in regions:
        page = QgsLayoutItemPage(layout)
        page.setPageSize(QgsLayoutSize(width, height))
        layout.pageCollection().addPage(page)
    layout.renderContext().setDpi(payload.get("dpi", 150))
    layout.setCustomProperty("geoharness/template", "structured-v1")
    layout.setCustomProperty("geoharness/regions", json.dumps(regions))
    layout.setCustomProperty("geoharness/font", json.dumps(font_info))
    layout.setCustomProperty("geoharness/effective_parameters", json.dumps(spec))
    adaptations = list(_adaptations or [])
    layout.setCustomProperty("geoharness/adaptations", json.dumps(adaptations))

    def add_map(name: str, rect: Any) -> Any:
        item = QgsLayoutItemMap(layout)
        frozen = (payload.get("_frozen_frames") or {}).get(name)
        _place_item(layout, item, name, frozen["rect_mm"] if frozen else regions[name])
        if name == "detail_map" and spec.get("detail_placement", "sidebar") == "page":
            from qgis.core import QgsLayoutPoint

            item.attemptMove(QgsLayoutPoint(*regions[name][:2]), False, False, 1)
        item.setCrs(project.crs())
        render, _ = _frame_selection(name, spec, len(layers), _map_indices(layers))
        item.setLayers([layers[index] for index in reversed(render)])
        item.setKeepLayerSet(True)
        frame = spec.get("main_frame" if name == "main_map" else "detail_frame") or {}
        if frame.get("label_layers") is not None:
            from qgis.core import QgsMapLayerStyle, QgsVectorLayer

            overrides = {}
            for index in render:
                layer = layers[index]
                if isinstance(layer, QgsVectorLayer) and index not in frame["label_layers"]:
                    original = QgsMapLayerStyle()
                    original.readFromLayer(layer)
                    layer.setLabelsEnabled(False)
                    hidden = QgsMapLayerStyle()
                    hidden.readFromLayer(layer)
                    overrides[layer.id()] = hidden.xmlData()
                    original.writeToLayer(layer)
            item.setLayerStyleOverrides(overrides)
            item.setKeepLayerStyles(True)
        item.zoomToExtent(QgsRectangle(*frozen["extent"]) if frozen else
                          _frame_extent(project, layers, name, payload, rect))
        item.setFrameEnabled(True)
        return item

    map_item = add_map("main_map", extent)
    layout.setReferenceMap(map_item)
    if "detail_map" in regions:
        add_map("detail_map", extent)
    if spec.get("label_table"):
        from geoharness.qgis.layout_table import add_label_tables

        add_label_tables(layout, layers, spec["label_table"], regions,
                         _text_format(font, spec["label_table"].get("font_size_pt", 8)),
                         page_spec=spec, context=context)
    def furniture(target_map, target_regions, text, *, prefix="", page_index=0):
        def place(item, name, rectangle):
            _place_item(layout, item, prefix + name, rectangle)
            if page_index:
                from qgis.core import QgsLayoutPoint

                item.attemptMove(QgsLayoutPoint(*rectangle[:2]), False, False, page_index)

        if text:
            label = QgsLayoutItemLabel(layout)
            label.setText(text)
            label.setTextFormat(_text_format(font, spec.get("title_font_size_pt", 18)))
            label.setMargin(0)
            label.setVAlign(Qt.AlignVCenter)
            place(label, "title", target_regions["title"])
        if spec.get("legend", True):
            legend = QgsLayoutItemLegend(layout)
            legend.setTitle("图例")
            legend.setLinkedMap(target_map)
            legend.setResizeToContents(False)
            legend.setAutoUpdateModel(False)
            root = legend.model().rootGroup()
            root.removeAllChildren()
            for layer in target_map.layers():
                root.addLayer(layer)
            legend.setWrapString("\n")
            legend_format = _text_format(font, spec.get("legend_font_size_pt")
                                        or spec.get("body_font_size_pt", 9))
            for component in (
                Qgis.LegendComponent.Title, Qgis.LegendComponent.Group,
                Qgis.LegendComponent.Subgroup, Qgis.LegendComponent.SymbolLabel,
            ):
                style = legend.style(component)
                style.setTextFormat(legend_format)
                legend.setStyle(component, style)
            place(legend, "legend", target_regions["legend"])
        footer = target_regions["footer"]
        text_x = footer[0]
        required_height = footer[3]
        if spec.get("scale_bar", True):
            scale = QgsLayoutItemScaleBar(layout)
            scale.setStyle("Single Box")
            scale.setLinkedMap(target_map)
            scale.setUnits(Qgis.DistanceUnit.Kilometers)
            scale.setNumberOfSegments(2)
            scale.setNumberOfSegmentsLeft(0)
            scale.applyDefaultSize(Qgis.DistanceUnit.Kilometers)
            scale.setTextFormat(body_format)
            target = target_map.scale() * 0.02 / 1000
            magnitude = 10 ** math.floor(math.log10(target))
            distance = max(value for value in (1, 2, 5) if value * magnitude <= target) * magnitude
            scale.setUnitsPerSegment(distance)
            numeric = QgsBasicNumericFormat()
            numeric.setNumberDecimalPlaces(max(0, -math.floor(math.log10(distance))))
            numeric.setShowTrailingZeros(False)
            scale.setNumericFormat(numeric)
            scale.setUnitLabel("km")
            place(scale, "scale_bar", [footer[0], footer[1], 1, 1])
            text_x += scale.rect().width() + spec.get("gap_mm", 5)
            required_height = max(required_height, scale.rect().height() + 0.5)
        if spec.get("footer_text"):
            text_width = footer[0] + footer[2] - text_x
            if text_width < 20:
                raise ValueError("Scale bar leaves less than 20mm for footer_text")
            label = QgsLayoutItemLabel(layout)
            label.setTextFormat(body_format)
            label.setMargin(0)
            label.setVAlign(Qt.AlignTop)
            label.setText(_wrap_label_text(label, spec["footer_text"], text_width))
            required_height = max(required_height, label.sizeForText().height() + 0.5)
            place(label, "footer_text", [text_x, footer[1], text_width, footer[3]])
        return required_height

    required_height = furniture(map_item, regions, title)
    footer = regions["footer"]
    requirements = {"0": ["main_map"]}
    requirements["0"].extend(name for name, enabled in (
        ("title", bool(title)), ("legend", spec.get("legend", True)),
        ("scale_bar", spec.get("scale_bar", True))) if enabled)
    if spec.get("detail_placement") == "page" and "detail_map" in regions:
        detail_title = spec.get("detail_title") or (title + " · 详图" if title else "详图")
        detail_spec = {**spec, "detail_extent": None, "detail_frame": None, "label_table": None}
        detail_regions = _layout_regions(detail_spec, detail_title)
        layout.setCustomProperty("geoharness/detail_regions", json.dumps(detail_regions))
        required_height = max(required_height, furniture(layout.itemById("detail_map"),
            detail_regions, detail_title, prefix="detail_", page_index=1))
        requirements["1"] = ["detail_map", "detail_title"]
        requirements["1"].extend(name for name, enabled in (
            ("detail_legend", spec.get("legend", True)),
            ("detail_scale_bar", spec.get("scale_bar", True))) if enabled)
    elif "detail_title" in regions:
        label = QgsLayoutItemLabel(layout)
        label.setText(spec["detail_title"])
        label.setTextFormat(body_format)
        label.setMargin(0)
        _place_item(layout, label, "detail_title", regions["detail_title"])
        requirements["0"].append("detail_title")
    layout.setCustomProperty("geoharness/page_requirements", json.dumps(requirements))
    page_regions = {"0": regions}
    if "1" in requirements:
        page_regions["1"] = detail_regions
    layout.setCustomProperty("geoharness/page_regions", json.dumps(page_regions))
    if required_height > footer[3]:
        if payload.get("_consume_plan"):
            raise ValueError("layout_plan is stale: measured footer changed; run preflight again")
        if (_fit_attempt >= 12 or required_height > 100
                or not spec.get("legend_auto_fit", True)
                or height + required_height - footer[3] > 2000):
            raise ValueError("footer_text exceeds its reserved rectangle; "
                             "shorten notes or increase page size")
        resized = {**spec, "footer_height_mm": required_height,
                   "height_mm": height + required_height - footer[3]}
        _layout_regions(resized, title)  # Fail before exporting or packaging any map.
        return _create_layout(project, layers, extent, {**payload, "layout": resized},
                              context, _fit_attempt=_fit_attempt + 1, _fit_states=fit_states,
                              _adaptations=[*adaptations, {
                                  "parameter": "footer_height_mm", "before": footer[3],
                                  "after": required_height, "reason": "measured footer text",
                              }])
    if spec.get("legend", True) and spec.get("legend_auto_fit", True):
        resized = dict(spec)
        for page_index, page_rects in page_regions.items():
            legend = layout.itemById("legend" if page_index == "0" else "detail_legend")
            minimum = QgsLegendRenderer(legend.model(), legend.legendSettings()).minimumSize()
            available = page_rects["legend"]
            if minimum.width() > available[2] + 0.05 and minimum.width() + 0.5 <= 500:
                sidebar = max(resized.get("sidebar_width_mm", 0), minimum.width() + 0.5)
                resized["sidebar_width_mm"] = sidebar
                # Fit both pages without taking paper area from either map.
                resized["width_mm"] = max(resized.get("width_mm", width),
                                           width + sidebar - available[2])
            if minimum.height() > available[3] + 0.05:
                ratio = (spec.get("detail_height_ratio", 0.4)
                         if page_index == "0" and spec.get("detail_placement") != "page"
                         and "detail_map" in regions else 0)
                extra = (minimum.height() + 0.5 - available[3]) / (1 - ratio)
                if height + extra <= 2000:
                    resized["height_mm"] = max(resized.get("height_mm", height), height + extra)
        if resized != spec and payload.get("_consume_plan"):
            raise ValueError("layout_plan is stale: measured legend changed; run preflight again")
        if resized != spec and _fit_attempt < 12 and resized.get("width_mm", width) <= 2000:
            try:
                _layout_regions(resized, title)
            except ValueError:
                pass  # Report measured content and dimensions below; never drop entries.
            else:
                changes = [{"parameter": key, "before": spec.get(key), "after": value,
                            "reason": "measured legend content"}
                           for key, value in resized.items() if spec.get(key) != value]
                return _create_layout(project, layers, extent, {**payload, "layout": resized},
                    context, _fit_attempt=_fit_attempt + 1, _fit_states=fit_states,
                    _adaptations=[*adaptations, *changes])
    from geoharness.qgis.layout_table import label_table_facts

    tables = label_table_facts(layout)
    if any(table["visible_rows"] < table["rows"] for table in tables):
        if (spec["label_table"].get("placement", "auto") == "auto"
                and not payload.get("_consume_plan")):
            resized = {**spec, "label_table": {**spec["label_table"], "placement": "pages"}}
            return _create_layout(project, layers, extent, {**payload, "layout": resized},
                context, _fit_attempt=_fit_attempt + 1, _fit_states=fit_states,
                _adaptations=[*adaptations, {"parameter": "label_table.placement",
                    "before": "auto", "after": "pages", "reason": "measured roster overflow",
                    "source_tables": tables}])
        if (_fit_attempt >= 12 or payload.get("_consume_plan")
                or not spec.get("legend_auto_fit", True)
                or spec["label_table"].get("placement") == "inline"):
            error = ValueError("label_table does not fit; use label_table.placement='pages' "
                               "or an explicit top_n+sort_by selection")
            error.diagnostics = {"code": "roster_overflow", "tables": tables,
                "page_mm": [width, height],
                "font_size_pt": spec["label_table"].get("font_size_pt", 8),
                "remedies": [{"path": ["layout", "label_table", "placement"], "value": "pages"}]}
            raise error
        required = max(table.totalSize().height() for table in layout.multiFrames()
                       if isinstance(table, QgsLayoutItemAttributeTable)) + 0.5
        resized = {**spec, "label_table": {**spec["label_table"],
                   "height_mm": max(spec["label_table"].get("height_mm", 50) + 1, required)}}
        extra = resized["label_table"]["height_mm"] - spec["label_table"].get("height_mm", 50)
        resized["height_mm"] = height + extra
        if resized["label_table"]["height_mm"] > 400 or resized["height_mm"] > 2000:
            error = ValueError("label_table exceeds supported dimensions; "
                               "set label_table.placement='pages'")
            error.diagnostics = {"code": "roster_overflow", "tables": tables,
                "required_table_height_mm": required,
                "proposed_page_height_mm": resized["height_mm"],
                "limits": {"table_height_mm": 400, "page_height_mm": 2000}}
            raise error
        return _create_layout(project, layers, extent, {**payload, "layout": resized},
            context, _fit_attempt=_fit_attempt + 1, _fit_states=fit_states,
            _adaptations=[*adaptations, {
                "parameter": "label_table.height_mm", "reason": "measured table rows",
                "after": resized["label_table"]["height_mm"]}])
    minimum_zoom = spec.get("detail_minimum_zoom")
    detail = layout.itemById("detail_map")
    if any((spec.get(name) or {}).get("requirements") for name in ("main_frame", "detail_frame")):
        subject_facts = _declared_frame_evidence(
            project, layers, payload, _plan_frames(layout, project, layers, payload))
        if not subject_facts["valid"]:
            error = ValueError("Declared map-frame subjects are missing or incompletely covered")
            error.diagnostics = {"code": "frame_subject_contract_failed", "subjects": subject_facts}
            raise error
    if minimum_zoom is not None and detail is not None:
        zoom = map_item.scale() / detail.scale()
        if not _detail_zoom_satisfied(zoom, minimum_zoom):
            requested = _frame_extent(project, layers, "detail_map", payload, extent)
            measured = detail.extent()
            fractions = (requested.width() / measured.width(),
                         requested.height() / measured.height())
            diagnostics = _detail_zoom_diagnostics(map_item, detail, minimum_zoom,
                                                   spec=spec, regions=regions,
                                                   original=payload["_layout_origin"])
            if spec.get("detail_zoom_policy", "hard") == "advisory":
                diagnostics.update(severity="warning", policy="advisory",
                    note="Preferred magnification is unmet. Subject coverage is preserved; "
                         "paper was not enlarged solely to meet this preference.")
                layout.setCustomProperty("geoharness/layout_warnings", json.dumps([diagnostics]))
            else:
                from geoharness.qgis.frame_contracts import joint_paper_proposal

                resized = joint_paper_proposal(spec, regions, zoom, minimum_zoom, fractions,
                                               original=payload["_layout_origin"])
                history = payload.get("_layout_zoom_history", [])
                progress = not history or zoom > history[-1] * 1.005
                if (resized is not None and progress and not payload.get("_consume_plan")
                    and spec.get("legend_auto_fit", True) and _fit_attempt < 12):
                    changes = [{"parameter": key, "before": spec.get(key), "after": value,
                            "reason": "joint measured detail zoom/legend sizing; coverage retained"}
                           for key, value in resized.items() if spec.get(key) != value]
                    return _create_layout(project, layers, extent, {**payload, "layout": resized,
                        "_layout_zoom_history": [*history, zoom]}, context,
                        _fit_attempt=_fit_attempt + 1, _fit_states=fit_states,
                        _adaptations=[*adaptations, *changes])
                diagnostics["coupled_fit_progress"] = {
                    "previous_zoom": history[-1] if history else None,
                    "improved": progress}
                error = ValueError(f"detail frame is not magnified enough (zoom={zoom:.8f}, "
                               f"required={minimum_zoom}); use the measured frame/scale "
                               "diagnostics; preserve all required subjects")
                error.diagnostics = diagnostics
                raise error
    _validate_layout_bounds(layout)
    project.layoutManager().addLayout(layout)
    return layout


def _detail_zoom_satisfied(actual: float, required: float) -> bool:
    """Allow coordinate serialization noise, not visibly insufficient magnification."""
    return actual >= required or math.isclose(actual, required, rel_tol=5e-6, abs_tol=1e-5)


def _fit_detail_paper(
    spec: dict, regions: dict, zoom: float, required: float,
    content_fraction: tuple[float, float] = (1.0, 1.0),
) -> dict | None:
    """Compatibility entry for the pure bounded joint-paper planner."""
    from geoharness.qgis.frame_contracts import joint_paper_proposal

    return joint_paper_proposal(spec, regions, zoom, required, content_fraction)


def _detail_zoom_diagnostics(main: Any, detail: Any, required_zoom: float, *,
                             spec=None, regions=None, original=None) -> dict[str, Any]:
    """Describe a failed paper-scale constraint without changing either map's coverage."""
    def facts(item: Any) -> dict[str, Any]:
        rect, extent = item.rect(), item.extent()
        return {
            "scale_denominator": item.scale(),
            "frame_mm": {"width": rect.width(), "height": rect.height()},
            "extent": [extent.xMinimum(), extent.yMinimum(),
                       extent.xMaximum(), extent.yMaximum()],
            "crs": item.crs().authid() or item.crs().toWkt(),
        }

    main_facts, detail_facts = facts(main), facts(detail)
    zoom = main.scale() / detail.scale()
    factor = required_zoom * 1.01 / zoom
    bounds = detail_facts["extent"]
    center_x, center_y = (bounds[0] + bounds[2]) / 2, (bounds[1] + bounds[3]) / 2
    half_width = (bounds[2] - bounds[0]) / factor / 2
    half_height = (bounds[3] - bounds[1]) / factor / 2
    from geoharness.qgis.frame_contracts import joint_paper_proposal, paper_limits

    spec = spec or {}
    proposal = (joint_paper_proposal(spec, regions, zoom, required_zoom, original=original)
                if regions else None)
    dimensions = {key: value for key, value in (proposal or {}).items()
                  if key in {"width_mm", "height_mm", "sidebar_width_mm", "detail_height_ratio"}}
    return {
        "code": "detail_zoom_insufficient",
        "actual_zoom": zoom,
        "required_zoom": required_zoom,
        "suggestion_target_zoom": required_zoom * 1.01,
        "target_detail_scale_denominator": main.scale() / required_zoom,
        "frames": {"main_map": main_facts, "detail_map": detail_facts},
        "public_limits": paper_limits(original or spec, spec),
        "suggestions": [
            {
                "strategy": "enlarge_detail_frame",
                "detail_frame_mm": {
                    "width": detail.rect().width() * factor,
                    "height": detail.rect().height() * factor,
                },
                "feasible_with_public_dimensions": proposal is not None,
                "patches": [{"path": ["layout", key], "value": value}
                            for key, value in dimensions.items() if spec.get(key) != value],
                "requires_native_preflight": True,
                "assumptions": "Keep the measured main frame and both extents unchanged; "
                               "the raw needed size is a measurement, not a new API field. "
                               "Only patches within public_limits are executable candidates; "
                               "coupled scales must be measured again.",
            },
            {
                "strategy": "smaller_detail_extent_if_subjects_fit",
                "parameter": "layout.detail_extent",
                "candidate_extent": {
                    "xmin": center_x - half_width, "ymin": center_y - half_height,
                    "xmax": center_x + half_width, "ymax": center_y + half_height,
                },
                "crs": detail_facts["crs"],
                "automatic_application": False,
                "also_clear_if_present": "layout.detail_frame.focus_layers",
                "required_check": "Candidate assumes unchanged measured frames/main scale. "
                                  "Verify every required subject remains covered before using; "
                                  "do not crop subjects to satisfy zoom. Preflight again.",
            },
            {
                "strategy": "separate_detail_map",
                "patches": [{"path": ["layout", "detail_placement"], "value": "page"}],
                "requires_native_preflight": True,
                "instruction": "If required subjects do not fit, retain their coverage and "
                               "use the real separate detail page. This does not promise the "
                               "requested zoom when the subject covers the whole overview.",
            },
        ],
    }


def _wrap_label_text(label: Any, text: str, width: float) -> str:
    """Measure the actual QgsLayoutItemLabel font, including CJK and explicit lines."""
    if len(text) > 20000:
        raise ValueError("footer_text exceeds the bounded layout text limit")
    lines = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
        while paragraph:
            low, high = 0, len(paragraph)
            while low < high:
                middle = (low + high + 1) // 2
                label.setText(paragraph[:middle])
                if label.sizeForText().width() <= width - 0.5:
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                raise ValueError("footer_text font is too large for the available width")
            boundary = paragraph.rfind(" ", 0, low)
            if low < len(paragraph) and boundary > low // 2:
                low = boundary + 1
            lines.append(paragraph[:low])
            paragraph = paragraph[low:]
    return "\n".join(lines)


def _layer_metadata(layer: Any) -> dict[str, Any]:
    from qgis.core import (
        QgsCategorizedSymbolRenderer,
        QgsGraduatedSymbolRenderer,
        QgsHillshadeRenderer,
        QgsVectorLayer,
    )

    metadata = {
        "id": layer.id(),
        "role": layer.customProperty("geoharness/role", "map"),
        "name": layer.name(),
        "provider": layer.providerType(),
        "crs": layer.crs().authid() or layer.crs().toWkt(),
        "valid": layer.isValid(),
    }
    source_index = layer.customProperty("geoharness/source_index", None)
    if source_index is not None:
        metadata["source_index"] = int(source_index)
    layer_key = layer.customProperty("geoharness/layer_key", "")
    if layer_key:
        metadata["layer_key"] = layer_key
    if isinstance(layer, QgsVectorLayer):
        renderer = layer.renderer()
        metadata.update(
            kind="vector",
            geometry_type=layer.geometryType().name,
            feature_count=layer.featureCount(),
            fields=[field.name() for field in layer.fields()],
            renderer=renderer.type() if renderer is not None else None,
            labels_enabled=layer.labelsEnabled(),
        )
        if isinstance(renderer, QgsCategorizedSymbolRenderer):
            metadata["category_field"] = renderer.classAttribute()
            metadata["categories"] = [
                {
                    "value": category.value(),
                    "label": category.label(),
                    **_symbol_metadata(category.symbol()),
                }
                for category in renderer.categories()
            ]
        elif isinstance(renderer, QgsGraduatedSymbolRenderer):
            metadata["range_field"] = renderer.classAttribute()
            metadata["ranges"] = [
                {
                    "lower": item.lowerValue(), "upper": item.upperValue(),
                    "label": item.label(), **_symbol_metadata(item.symbol()),
                }
                for item in renderer.ranges()
            ]
        elif renderer is not None and renderer.type() == "singleSymbol":
            metadata["symbol"] = _symbol_metadata(renderer.symbol())
        if layer.labelsEnabled() and layer.labeling() is not None:
            settings = layer.labeling().settings()
            metadata["labels_enabled"] = settings.drawLabels
            metadata["obstacle_enabled"] = settings.obstacleSettings().isObstacle()
            metadata["obstacle_factor"] = settings.obstacleSettings().factor()
            text_format = settings.format()
            metadata["label_style"] = {
                "field": settings.fieldName,
                "font": json.loads(layer.customProperty("geoharness/label_font", "{}")),
                "size_pt": text_format.size(),
                "buffer_enabled": text_format.buffer().enabled(),
                "buffer_size_mm": text_format.buffer().size(),
                "overlap_handling": settings.placementSettings().overlapHandling().name,
                "placement": settings.placement.name,
                "distance_mm": settings.dist,
                "offset_x_mm": settings.xOffset, "offset_y_mm": settings.yOffset,
                "priority": settings.priority,
                "offset_unit": settings.offsetUnits.name, "distance_unit": settings.distUnits.name,
            }
    else:
        metadata["kind"] = "raster"
        renderer = layer.renderer()
        metadata["renderer"] = renderer.type() if renderer is not None else None
        metadata["band_count"] = layer.bandCount()
        if isinstance(renderer, QgsHillshadeRenderer):
            metadata["raster_style"] = {
                "renderer": "hillshade", "band": renderer.inputBand(),
                "azimuth": renderer.azimuth(), "altitude": renderer.altitude(),
            }
    return metadata


def _rectangle_problems(
    rectangles: dict[str, list[float]], width: float, height: float
) -> list[str]:
    problems = []
    tolerance = 0.05
    for name, (x, y, w, h) in rectangles.items():
        if not all(math.isfinite(value) for value in (x, y, w, h)) or w <= 0 or h <= 0:
            problems.append(f"{name}: invalid rectangle")
        elif x < -tolerance or y < -tolerance or x + w > width + tolerance or (
            y + h > height + tolerance
        ):
            problems.append(f"{name}: rectangle exceeds page bounds")
    entries = list(rectangles.items())
    for index, (name, (x, y, w, h)) in enumerate(entries):
        for other, (ox, oy, ow, oh) in entries[index + 1:]:
            if min(x + w, ox + ow) - max(x, ox) > tolerance and (
                min(y + h, oy + oh) - max(y, oy) > tolerance
            ):
                problems.append(f"{name}/{other}: rectangles overlap")
    return problems


def _layout_bounds(layout: Any) -> dict[str, Any]:
    from qgis.core import (
        Qgis,
        QgsLayoutItem,
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemPage,
        QgsLegendRenderer,
    )

    if layout.customProperty("geoharness/template", "") != "structured-v1":
        return {"checked": False, "valid": None, "problems": []}
    if layout.units() != Qgis.LayoutUnit.Millimeters:
        return {"checked": False, "valid": None, "problems": [],
                "reason": "Bounds check requires a millimetre scene; saved layout preserved"}
    page = layout.renderContext().measurementConverter().convert(
        layout.pageCollection().page(0).pageSize(), Qgis.LayoutUnit.Millimeters)
    regions = json.loads(layout.customProperty("geoharness/regions", "{}"))
    rectangles = {}
    user_items = {}
    problems = []
    content_metrics = {}
    roster_pages = {}
    for item in layout.items():
        if not isinstance(item, QgsLayoutItem) or isinstance(item, QgsLayoutItemPage):
            continue
        name = item.id() or item.uuid()
        rect = item.mapRectToScene(item.rect())
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        secondary = item.page() > 0 and (name == "detail_map" or name.startswith("detail_"))
        if (name.startswith("label_table_") or secondary) and item.page() > 0:
            roster_page = layout.pageCollection().page(item.page())
            local = [x - roster_page.pos().x(), y - roster_page.pos().y(), w, h]
            roster_pages.setdefault(item.page(), {})[name] = local
            if not secondary:
                continue
            x, y = local[:2]
        if not secondary and name not in {*regions, "scale_bar", "footer_text"}:
            # User annotations, north arrows and inset decorations legitimately
            # overlap maps. Preserve them; pixel review assesses their placement.
            user_items[name] = [x, y, w, h]
            continue
        if not secondary:
            rectangles[name] = [x, y, w, h]
        own_regions = (json.loads(layout.customProperty("geoharness/detail_regions", "{}"))
                       if secondary else regions)
        own_name = name.removeprefix("detail_") if secondary else name
        if secondary and name == "detail_map":
            own_name = "main_map"
        owner = (own_regions.get("footer") if own_name in {"scale_bar", "footer_text"}
                 else own_regions.get(own_name))
        if owner is not None:
            ox, oy, width, height = owner
            if x < ox - 0.05 or y < oy - 0.05 or (
                x + w > ox + width + 0.05 or y + h > oy + height + 0.05
            ):
                problems.append(f"{name}: item exceeds its reserved rectangle")
        if isinstance(item, QgsLayoutItemLegend):
            minimum = QgsLegendRenderer(item.model(), item.legendSettings()).minimumSize()
            content_metrics[name] = {
                "required_mm": [minimum.width(), minimum.height()],
                "available_mm": [w, h],
            }
            if minimum.width() > w + 0.05 or (
                minimum.height() > h + 0.05
            ):
                problems.append(f"{name}: legend content exceeds its reserved rectangle")
        elif isinstance(item, QgsLayoutItemLabel):
            minimum = item.sizeForText()  # QSizeF in millimetres, unlike pageSize/sizeWithUnits.
            content_metrics[name] = {"required_mm": [minimum.width(), minimum.height()],
                                     "available_mm": [w, h]}
            if minimum.width() > w + 0.05 or (
                minimum.height() > h + 0.05
            ):
                problems.append(f"{name}: label text exceeds its reserved rectangle")
    problems.extend(_rectangle_problems(rectangles, page.width(), page.height()))
    for number, items in roster_pages.items():
        roster_page = layout.pageCollection().page(number)
        size = roster_page.pageSize()
        problems.extend(f"page {number + 1}: {problem}" for problem in
                        _rectangle_problems(items, size.width(), size.height()))
    problems.extend(_page_completeness(layout)["problems"])
    from geoharness.qgis.layout_table import roster_selection_facts

    selection = roster_selection_facts(layout)
    if selection and selection["membership_check"]["valid"] is False:
        problems.append("label_table: actual roster membership differs from selected identities")
    return {
        "checked": True, "valid": not problems, "problems": problems,
        "items": rectangles, "user_items": user_items, "content_metrics": content_metrics,
        "roster_pages": roster_pages,
    }


def _page_completeness(layout):
    from geoharness.qgis.page_completeness import page_completeness

    return page_completeness(layout)


def _validate_layout_bounds(layout: Any) -> None:
    result = _layout_bounds(layout)
    if result["problems"]:
        error = ValueError("Structured layout validation failed: " + "; ".join(result["problems"])
                           + "; use the measured required_mm/available_mm, effective parameters "
                           "and loaded font evidence to adjust page/sidebar dimensions")
        error.diagnostics = _layout_metadata(layout)
        raise error


def _legend_node_facts(legend: Any, layout: Any) -> dict[str, Any]:
    """The current legend node model, independent of pixel readability."""
    from qgis.core import Qgis, QgsSymbolLegendNode
    from qgis.PyQt.QtCore import Qt

    rows, layers = [], []
    truncated = False
    model = legend.model()
    for tree_layer in model.rootGroup().findLayers():
        layer = tree_layer.layer()
        index = layer.customProperty("geoharness/source_index", None) if layer else None
        index = int(index) if index is not None else None
        symbol_count, unknown_count = 0, 0
        nodes = model.layerLegendNodes(tree_layer)
        for node in nodes:
            if len(rows) >= 256:
                truncated = True
                break
            row = {"layer_index": index, "text": str(node.data(Qt.DisplayRole) or ""),
                   "node_type": type(node).__name__}
            if isinstance(node, QgsSymbolLegendNode) and node.symbol() is not None:
                row["symbol"] = _symbol_metadata(node.symbol())
                symbol_count += 1
            else:
                unknown_count += 1
            rows.append(row)
        layers.append({"layer_index": index, "layer_label": tree_layer.name(),
            "symbolnode_count": symbol_count, "unknown_node_count": unknown_count,
            "count_complete": not truncated})
        if truncated:
            break
    rect = legend.mapRectToScene(legend.rect())
    scene = [rect.x(), rect.y(), rect.width(), rect.height()]
    page = layout.pageCollection().page(legend.page())
    local = [rect.x() - page.pos().x(), rect.y() - page.pos().y(), rect.width(), rect.height()]
    return {"scope": "actual_legend_node_model; not_pixel_legibility", "rows": rows,
            "row_count": len(rows), "layers": layers, "sample_limit": 256,
            "truncated": truncated, "rect_scene": scene, "layout_unit": layout.units().name,
            "page_index": legend.page(),
            "rect_page_mm": local if layout.units() == Qgis.LayoutUnit.Millimeters else None,
            "rect_mm": scene if layout.units() == Qgis.LayoutUnit.Millimeters else None}


def _layout_metadata(layout: Any) -> dict[str, Any]:
    from qgis.core import (
        Qgis,
        QgsLayoutItemLabel,
        QgsLayoutItemLegend,
        QgsLayoutItemMap,
        QgsLayoutItemScaleBar,
    )

    maps = []
    scale_bars = []
    legends = {}
    page = layout.pageCollection().page(0)
    converter = layout.renderContext().measurementConverter()
    page_size = converter.convert(page.pageSize(), Qgis.LayoutUnit.Millimeters) if page else None
    effective = ({"width_mm": page_size.width(), "height_mm": page_size.height()}
                 if page_size else {})

    def record_font(key: str, text_format: Any) -> None:
        effective[key] = {"value": text_format.size(), "unit": text_format.sizeUnit().name}
        if text_format.sizeUnit() == Qgis.RenderUnit.Points:
            effective[key + "_pt"] = text_format.size()
        else:
            effective.pop(key + "_pt", None)
    for item in layout.items():
        if isinstance(item, QgsLayoutItemMap):
            extent = item.extent()
            maps.append(
                {
                    "id": item.id(),
                    "page_index": item.page(),
                    "crs": item.crs().authid(),
                    "crs_valid": item.crs().isValid(),
                    "scale": item.scale(),
                    "extent": [
                        extent.xMinimum(),
                        extent.yMinimum(),
                        extent.xMaximum(),
                        extent.yMaximum(),
                    ],
                    "layers": [layer.id() for layer in item.layers()],
                }
            )
            main = layout.itemById("main_map")
            if (item.id() == "detail_map" and main and main.rect().height() > 0
                    and item.page() == main.page()):
                effective["detail_height_ratio"] = (
                    item.rect().height() / main.rect().height())
            elif item.id() == "detail_map" and main and item.page() != main.page():
                effective["detail_placement"] = "page"
        elif isinstance(item, QgsLayoutItemLegend):
            legends[item.id() or item.uuid()] = _legend_node_facts(item, layout)
            effective["sidebar_width_mm"] = converter.convert(
                item.sizeWithUnits(), Qgis.LayoutUnit.Millimeters).width()
            record_font("legend_font_size", item.style(
                Qgis.LegendComponent.SymbolLabel).textFormat())
        elif isinstance(item, QgsLayoutItemLabel):
            key = {"title": "title_font_size_pt", "footer_text": "body_font_size_pt"}.get(
                item.id())
            if key:
                record_font(key.removesuffix("_pt"), item.textFormat())
        elif isinstance(item, QgsLayoutItemScaleBar):
            if "body_font_size" not in effective:
                record_font("body_font_size", item.textFormat())
            scale_bars.append({
                "id": item.id(), "units": item.units().name,
                "units_per_segment": item.unitsPerSegment(), "segments": item.numberOfSegments(),
            })
    from geoharness.qgis.layout_table import label_table_facts, roster_selection_facts

    return {
        "name": layout.name(), "pages": layout.pageCollection().pageCount(), "map_frames": maps,
        "label_tables": label_table_facts(layout),
        "roster_selection": roster_selection_facts(layout),
        "page_completeness": _page_completeness(layout),
        "warnings": json.loads(layout.customProperty("geoharness/layout_warnings", "[]")),
        "map_extents": {item["id"]: item["extent"] for item in maps},
        "scale_bars": scale_bars,
        "legends": legends,
        "template": layout.customProperty("geoharness/template", ""),
        "regions": json.loads(layout.customProperty("geoharness/regions", "{}")),
        "page_regions": json.loads(layout.customProperty("geoharness/page_regions", "{}")),
        "font": json.loads(layout.customProperty("geoharness/font", "{}")),
        "effective_parameters": effective,
        "creation_parameters": json.loads(layout.customProperty(
            "geoharness/effective_parameters", "{}")),
        "adaptations": json.loads(layout.customProperty("geoharness/adaptations", "[]")),
        "bounds_check": _layout_bounds(layout),
        "footer_text": (layout.itemById("footer_text").text()
                        if isinstance(layout.itemById("footer_text"), QgsLayoutItemLabel) else ""),
        "layout_plan": json.loads(layout.customProperty("geoharness/layout_plan", "null")),
    }


def _validate_project_coordinates(
    project: Any, layouts: list[Any], *, strict_layout: bool = False
) -> None:
    """A readable project with valid layers can still have projection disabled."""
    if not project.crs().isValid():
        raise ValueError(
            "Project CRS is invalid; verify projection is enabled in the saved project"
        )
    for layout in layouts:
        if strict_layout:
            _validate_layout_bounds(layout)
        for frame in _layout_metadata(layout)["map_frames"]:
            if not frame["crs_valid"]:
                raise ValueError(f"Layout map {frame['id']} has an invalid CRS")
            xmin, ymin, xmax, ymax = frame["extent"]
            if not all(math.isfinite(value) for value in frame["extent"]):
                raise ValueError(f"Layout map {frame['id']} has a nonfinite extent")
            if xmin >= xmax or ymin >= ymax:
                raise ValueError(f"Layout map {frame['id']} has a zero or reversed extent")
            if not math.isfinite(frame["scale"]) or frame["scale"] <= 0:
                raise ValueError(f"Layout map {frame['id']} has an invalid scale")


def _export_label_facts(layout: Any, exporter: Any) -> dict[str, list[dict[str, Any]]]:
    """Read PAL results owned by the exporter immediately after this PNG export."""
    from qgis.core import QgsLayoutItemMap

    results = exporter.labelingResults()
    labels = {}
    for item in layout.items():
        if not isinstance(item, QgsLayoutItemMap):
            continue
        indices = {layer.id(): layer.customProperty("geoharness/source_index", None)
                   for layer in layout.project().mapLayers().values()}
        result = results.get(item.uuid())
        positions = result.allLabels() if result is not None else []
        labels[item.id() or item.uuid()] = [{
            "layer_index": (int(indices[position.layerID])
                            if indices.get(position.layerID) is not None else None),
            "feature_id": int(position.featureId), "text": position.labelText,
            "bbox": _rect_values(position.labelRect), "is_unplaced": position.isUnplaced,
        } for position in positions[:512]]
    return labels


def _export_geometry_facts(layout: Any, labels: dict, dpi: float) -> dict[str, Any]:
    """Bounded geometry and real renderer symbols from the saved layout being exported."""
    from qgis.core import (
        Qgis,
        QgsCoordinateTransform,
        QgsFeatureRequest,
        QgsGeometry,
        QgsLayoutItemMap,
        QgsRectangle,
        QgsRenderContext,
        QgsVectorLayer,
    )
    from qgis.PyQt.QtCore import QSizeF

    frames = {}
    for item in layout.items():
        if not isinstance(item, QgsLayoutItemMap):
            continue
        name = item.id() or item.uuid()
        extent = item.extent()
        rect = item.mapRectToScene(item.rect())
        millimeters = layout.units() == Qgis.LayoutUnit.Millimeters
        physical = layout.renderContext().measurementConverter().convert(
            item.sizeWithUnits(), Qgis.LayoutUnit.Millimeters)
        settings = item.mapSettings(extent,
            QSizeF(physical.width() * dpi / 25.4, physical.height() * dpi / 25.4), dpi, True)
        points, bboxes = [], []
        source_indices = []
        for layer in settings.layers():
            source_index = layer.customProperty("geoharness/source_index", None)
            if source_index is not None:
                source_index = int(source_index)
                source_indices.append(source_index)
            if not isinstance(layer, QgsVectorLayer) or layer.renderer() is None:
                continue
            transform = QgsCoordinateTransform(layer.crs(), item.crs(), layout.project())
            inverse = QgsCoordinateTransform(item.crs(), layer.crs(), layout.project())
            request = QgsFeatureRequest().setFilterRect(inverse.transformBoundingBox(extent))
            context = QgsRenderContext.fromMapSettings(settings)
            context.setCoordinateTransform(transform)
            renderer = layer.renderer()
            renderer.startRender(context, layer.fields())
            bbox, minimum, sampled = None, None, 0
            try:
                for feature in layer.getFeatures(request):
                    geometry = QgsGeometry(feature.geometry())
                    if geometry.isEmpty():
                        continue
                    geometry.transform(transform)
                    if not geometry.intersects(extent):
                        continue
                    context.expressionContext().setFeature(feature)
                    symbol = renderer.symbolForFeature(feature, context)
                    if symbol is None:
                        continue
                    if geometry.type() == Qgis.GeometryType.Point and len(points) < 512:
                        positions = geometry.asMultiPoint() if geometry.isMultipart() else [
                            geometry.asPoint()]
                        symbol_info = _symbol_metadata(symbol)
                        symbol_info["layer_opacity"] = layer.opacity()
                        for point in positions:
                            if extent.contains(point) and len(points) < 512:
                                points.append({"layer_index": source_index,
                                    "feature_id": int(feature.id()), "x": point.x(), "y": point.y(),
                                    "symbol": symbol_info})
                    elif geometry.type() == Qgis.GeometryType.Polygon:
                        visible = geometry.intersection(QgsGeometry.fromRect(extent)).boundingBox()
                        if visible.isFinite() and not visible.isEmpty():
                            sampled += 1
                            bbox = QgsRectangle(visible) if bbox is None else bbox
                            bbox.combineExtentWith(visible)
                            if millimeters:
                                span = min(visible.width() / extent.width() * rect.width(),
                                           visible.height() / extent.height() * rect.height())
                                minimum = span if minimum is None else min(minimum, span)
                            if sampled >= 512:
                                break
                    if geometry.type() == Qgis.GeometryType.Point and len(points) >= 512:
                        break
            finally:
                renderer.stopRender(context)
            if bbox is not None:
                bboxes.append({"layer_index": source_index, "bbox": _rect_values(bbox),
                    "minimum_span_mm": minimum, "sample_size": sampled, "sample_limit": 512,
                    "scope": "sampled_visible_polygon_feature_bboxes"})
        frames[name] = {"extent": _rect_values(extent), "render_layers": sorted(source_indices),
            "page_index": item.page(),
            "rect_page_mm": [rect.x() - layout.pageCollection().page(item.page()).pos().x(),
                             rect.y() - layout.pageCollection().page(item.page()).pos().y(),
                             rect.width(), rect.height()] if millimeters else None,
            "crs": item.crs().authid() or item.crs().toWkt(),
            "rect_mm": ([rect.x(), rect.y(), rect.width(), rect.height()] if millimeters else None),
            "rect_scene": [rect.x(), rect.y(), rect.width(), rect.height()],
            "layout_unit": layout.units().name,
            "source_indices": source_indices, "layer_order": "top_to_bottom",
            "points": points, "labels": labels.get(name, []), "layer_bboxes": bboxes[:512],
            "sample_limit": 512}
    return {"frames": frames,
        "scope": "actual_export_geometry_and_native_symbol_state; not_pixel_acceptance"}


def _packaged_creation_spec(spec: Any, layers: list[dict], layout: dict) -> dict | None:
    """Rebuild only from a supplied creation contract and the delivered package assets."""
    if not isinstance(spec, dict) or not isinstance(spec.get("parameters"), dict):
        return None
    result = json.loads(json.dumps(spec))
    parameters = result["parameters"]
    selected = parameters.get("layers", [])
    if len(selected) != len(layers):
        raise ValueError("Creation parameters must match the packaged source layer array")
    for index, (source, packaged) in enumerate(zip(selected, layers)):
        source["file_path"] = packaged["file"]
        source.pop("derivation", None)
        source.pop("layer_name", None)
        if packaged["kind"] == "vector":
            source["layer_name"] = f"layer_{index}"
        label = source.get("label_style")
        if isinstance(label, dict):
            font = (packaged.get("label_style") or {}).get("font") or {}
            if font.get("packaged"):
                label["font_file"] = font["file"]
            else:
                label.pop("font_file", None)
    planned = parameters.get("layout")
    if isinstance(planned, dict):
        planned.pop("layout_plan", None)
        font = layout.get("font") or {}
        if font.get("packaged"):
            planned["font_file"] = font["file"]
        else:
            planned.pop("font_file", None)
    return result


def _export_layout(
    layout: Any, payload: dict[str, Any], context: Any,
) -> tuple[Path, Path, dict[str, Any]]:
    from qgis.core import QgsLayoutExporter

    exporter = QgsLayoutExporter(layout)
    if payload.get("strict_layout", False):
        _validate_layout_bounds(layout)
    preview = Path(context.output_path(None, "preview.png"))
    image_settings = QgsLayoutExporter.ImageExportSettings()
    image_settings.dpi = payload.get("dpi", 150)
    image_settings.pages = [0]
    status = exporter.exportToImage(str(preview), image_settings)
    if status != QgsLayoutExporter.Success:
        raise ValueError(f"QGIS PNG export failed: {exporter.errorMessage()}")
    if not preview.is_file() and preview.with_stem("preview_1").is_file():
        preview.with_stem("preview_1").rename(preview)
    if not preview.is_file():
        raise ValueError("QGIS did not write the layout preview")
    labels = _export_label_facts(layout, exporter)
    from geoharness.qgis.layout_table import label_table_facts

    tables = label_table_facts(layout)
    roster_previews, page_previews = [], [{"page_index": 0, "path": str(preview),
        "frames": [name for name in ("main_map", "detail_map")
                   if layout.itemById(name) is not None and layout.itemById(name).page() == 0]}]
    for index in range(1, layout.pageCollection().pageCount()):
        frame_ids = [name for name in ("main_map", "detail_map")
                     if layout.itemById(name) is not None and layout.itemById(name).page() == index]
        prefix = "detail_page" if frame_ids else "roster_page"
        path = Path(context.output_path(None, f"{prefix}_{index + 1}.png"))
        image_settings.pages = [index]
        status = exporter.exportToImage(str(path), image_settings)
        if status != QgsLayoutExporter.Success:
            raise ValueError(f"QGIS roster-page PNG export failed: {exporter.errorMessage()}")
        numbered = path.with_stem(path.stem + f"_{index + 1}")
        if not path.is_file() and numbered.is_file():
            numbered.rename(path)
        if not path.is_file():
            raise ValueError("QGIS did not write the roster-page preview")
        labels.update({key: value for key, value in _export_label_facts(layout, exporter).items()
                       if value})
        page_previews.append({"page_index": index, "path": str(path), "frames": frame_ids})
        if any(table["page"] == index for table in tables):
            roster_previews.append(str(path))
    native_facts = _export_geometry_facts(layout, labels, image_settings.dpi)
    native_facts.update(roster_previews=roster_previews, page_previews=page_previews)
    if payload.get("strict_layout", False):
        _validate_layout_bounds(layout)
    output_format = payload.get("output_format", "png")
    if output_format == "png":
        requested = payload.get("output_path")
        output = preview
        if requested:
            candidate = Path(requested)
            candidate = candidate if candidate.is_absolute() else context.run_dir / candidate
            if candidate.resolve() != preview:
                output = Path(context.output_path(requested, "preview.png"))
        if output.suffix.lower() != ".png":
            raise ValueError("PNG output_path must end with .png")
        if output != preview:
            shutil.copy2(preview, output)
    elif output_format == "pdf":
        output = Path(context.output_path(payload.get("output_path") or None, "map.pdf"))
        if output.suffix.lower() != ".pdf":
            raise ValueError("PDF output_path must end with .pdf")
        pdf_settings = QgsLayoutExporter.PdfExportSettings()
        pdf_settings.dpi = payload.get("dpi", 150)
        status = exporter.exportToPdf(str(output), pdf_settings)
        if status != QgsLayoutExporter.Success or not output.is_file():
            raise ValueError(f"QGIS PDF export failed: {exporter.errorMessage()}")
    else:
        raise ValueError("Only PNG/PDF layout export is supported")
    return preview, output, native_facts


def _save_package(
    project: Any,
    layout: Any,
    layers: list[Any],
    payload: dict[str, Any],
    context: Any,
) -> dict[str, Any]:
    from qgis.core import Qgis

    layer_info = [
        _snapshot_layer(layer, index, project, context) for index, layer in enumerate(layers)
    ]
    layout_plan = json.loads(layout.customProperty("geoharness/layout_plan", "null"))
    composition = json.loads(layout.customProperty("geoharness/composition", "null"))
    frame_checks = json.loads(layout.customProperty("geoharness/frame_checks", "null"))
    if payload.get("_consume_plan") and layout_plan is not None and \
            _native_source_identities(payload, context) != layout_plan["source_identities"]:
        raise ValueError("layout_plan is stale: data source changed while snapshotting; "
                         "run preflight again")
    facts = payload.get("analysis_facts")
    if facts is None:
        # Preserve the original provenance on user-edited exports as historical evidence.
        # This does not certify edited layers or refresh their old statistics.
        recorded, _ = project.readEntry("geoharness", "analysis_facts", "")
        if recorded:
            try:
                facts = json.loads(recorded)
                if isinstance(facts, dict):
                    facts = {**facts,
                        "original_status": facts.get("original_status", facts.get("status")),
                        "status": "historical_unverified", "current_export_verified": False}
            except (TypeError, ValueError):
                facts = None
    if isinstance(facts, dict):
        facts_path = Path(context.output_path(None, "analysis/verified-facts.json"))
        facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
        project.writeEntry("geoharness", "analysis_facts", json.dumps(facts, ensure_ascii=False))
    if payload.get("map_id"):
        project.writeEntry("geoharness", "map_job_id", payload["map_id"])
    engine_settings = project.labelingEngineSettings()
    engine_settings.setFlag(Qgis.LabelingFlag.CollectUnplacedLabels, True)
    project.setLabelingEngineSettings(engine_settings)
    project_path = Path(context.output_path(None, "map." + payload.get("project_format", "qgz")))
    project.setPresetHomePath("")
    project.setFilePathStorage(Qgis.FilePathType.Relative)
    project.setFileName(str(project_path))
    if not project.write():
        raise ValueError(f"QGIS could not save the packaged project: {project.error()}")
    from qgis.core import QgsProject

    reopened = QgsProject()
    try:
        _register_project_fonts(project_path, context)
        if not reopened.read(str(project_path)):
            raise ValueError(f"QGIS could not reopen its packaged project: {reopened.error()}")
        if not all(layer.isValid() for layer in reopened.mapLayers().values()):
            raise ValueError("Packaged project contains invalid data references")
        _restore_font_metadata(reopened, project_path, context, package=False)
        _validate_project_coordinates(
            reopened, reopened.layoutManager().printLayouts(),
            strict_layout=payload.get("strict_layout", False),
        )
        saved_layout = next(
            (item for item in reopened.layoutManager().printLayouts()
             if item.name() == layout.name()),
            None,
        )
        if saved_layout is None:
            raise ValueError("Packaged project did not preserve the selected print layout")
        if payload.get("_consume_plan") and layout_plan is not None:
            saved_layers = sorted(reopened.mapLayers().values(), key=lambda item:
                int(item.customProperty("geoharness/source_index", -1)))
            expected = [(index, layer.id()) for index, layer in enumerate(layers)]
            actual = [(int(layer.customProperty("geoharness/source_index", -1)), layer.id())
                      for layer in saved_layers]
            if actual != expected:
                raise ValueError("Saved project did not preserve source_index to layer ID bindings")
            for original, saved in zip(layers, saved_layers):
                if _layer_metadata(original).get("feature_count") != \
                        _layer_metadata(saved).get("feature_count"):
                    raise ValueError("Saved project did not preserve native feature counts")
            recorded = json.loads(saved_layout.customProperty("geoharness/layout_plan", "null"))
            if recorded != layout_plan:
                raise ValueError("Saved project did not preserve its frozen layout_plan")
            _validate_plan_layout(saved_layout, reopened, saved_layers, payload,
                                  layout_plan, context)
        # Native save/read normalizes floating coordinates and scale-bar metrics.
        # Export the saved layout so preview pixels correspond to the delivered QGS.
        preview, output, native_facts = _export_layout(saved_layout, payload, context)
        from geoharness.qgis.layout_table import export_full_roster

        export_full_roster(saved_layout, context)
        layout_info = _layout_metadata(saved_layout)
        if payload.get("_consume_plan") and layout_plan is not None:
            for name, frame in native_facts["frames"].items():
                planned = layout_plan["frames"].get(name)
                if planned is not None:
                    frame.update({key: planned[key] for key in (
                        "focus_bbox", "normalized_center", "bbox_fraction", "coverage")})
                    frame["focus_scope"] = planned.get("focus_scope",
                                                       "validated_current_layout_plan")
                    frame["subject_contracts"] = planned.get("subject_contracts", [])
    finally:
        reopened.clear()
    digest = hashlib.sha256(preview.read_bytes()).hexdigest()
    native_facts["image_sha256"] = digest
    creation_spec = _packaged_creation_spec(payload.get("_creation_spec"), layer_info, layout_info)
    readme = Path(context.output_path(None, "README.md"))
    readme.write_text(
        "# Editable QGIS map\n\nOpen `" + project_path.name + "` in QGIS. Packaged data and "
        "styles are local snapshots; source files were not changed. Edit the saved print "
        "layout, save it, then use geo_qgis_project export to create a new package.\n\n"
        "`preview.png` is the first page exported from that same layout. This is an "
        "unverified draft; successful QGIS export does not certify visual or analytical "
        "quality.\n\n"
        "Explicit custom fonts, when present, are copied into `assets/fonts/` and registered "
        "by GeoHarness before re-export. QGIS desktop does not automatically register font "
        "files from GeoHarness custom properties. If a custom font is absent on another "
        "computer, select an installed font in QGIS or install the supplied font yourself. "
        "GeoHarness does not install fonts into the operating system.\n",
        encoding="utf-8",
    )
    manifest_path = Path(context.output_path(None, "project.json"))
    files = [project_path, preview, output, readme, manifest_path]
    roster = context.run_dir / 'roster_full.csv'
    if roster.is_file():
        files.append(roster)
    files.extend(Path(path) for path in native_facts.get('roster_previews', []))
    files.extend(Path(item["path"]) for item in native_facts.get("page_previews", []))
    for directory in (
        context.run_dir / "data",
        context.run_dir / "styles",
        context.run_dir / "assets",
        context.run_dir / "analysis",
    ):
        if directory.is_dir():
            files.extend(path for path in directory.rglob("*") if path.is_file())
    manifest = {
        "backend": "qgis",
        "project_fidelity": "saved_layout",
        "project_file": project_path.name,
        "preview": "preview.png",
        "preview_sha256": digest,
        "quality_status": "draft",
        "visual_review_status": "unverified",
        "layers": layer_info,
        "layout": layout_info,
        "map_id": payload.get("map_id", ""),
        "analysis": facts,
        "layout_plan": layout_plan,
        "composition": composition,
        "frame_checks": frame_checks,
        "native_facts": {**native_facts, "page_previews": [
            {**item, "path": Path(item["path"]).relative_to(context.run_dir).as_posix()}
            for item in native_facts.get("page_previews", [])]},
        "creation_spec": creation_spec,
        "automatic_repair_eligible": creation_spec is not None,
        "files": sorted({path.relative_to(context.run_dir).as_posix() for path in files}),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "backend": "qgis",
        "project_fidelity": "saved_layout",
        "project_dir": str(context.run_dir),
        "project_file": str(project_path),
        "project_manifest": str(manifest_path),
        "output_path": str(output),
        "preview_path": str(preview),
        "image_sha256": digest,
        "quality_status": "draft",
        "visual_review_status": "unverified",
        "primary_map": str(preview),
        "layers": layer_info,
        "layout": manifest["layout"],
        "analysis": facts,
        "layout_plan": layout_plan,
        "composition": composition,
        "frame_checks": frame_checks,
        "native_facts": native_facts,
        "creation_spec": creation_spec,
        "automatic_repair_eligible": creation_spec is not None,
        "render_executed": True,
    }


def _project_layers(project: Any) -> list[Any]:
    """Include non-tree dependencies without changing saved drawing order or visibility."""
    layers = list(reversed(project.layerTreeRoot().layerOrder()))
    tree_ids = {layer.id() for layer in layers}
    layers.extend(layer for layer in project.mapLayers().values() if layer.id() not in tree_ids)
    return layers


def _relocate_symbol_assets(symbol: Any, relocate: Any) -> None:
    """Set native resource properties, including nested fill/marker symbols."""
    if symbol is None:
        return
    for symbol_layer in symbol.symbolLayers():
        for getter, setter in (("path", "setPath"), ("svgFilePath", "setSvgFilePath")):
            read = getattr(symbol_layer, getter, None)
            write = getattr(symbol_layer, setter, None)
            if callable(read) and callable(write):
                original = read()
                replacement = relocate(original)
                if replacement != original:
                    write(replacement)
        _relocate_symbol_assets(symbol_layer.subSymbol(), relocate)


def _relocate_project_assets(project: Any, relocate: Any) -> None:
    """Builtin SVG resolution can bypass QgsPathResolver; update live QGIS objects."""
    from qgis.core import (
        QgsLayoutItemMap,
        QgsLayoutItemPicture,
        QgsMapLayerStyle,
        QgsRenderContext,
        QgsVectorLayer,
    )

    def relocate_layer(layer: Any) -> None:
        if isinstance(layer, QgsVectorLayer) and layer.renderer() is not None:
            for symbol in layer.renderer().symbols(QgsRenderContext()):
                _relocate_symbol_assets(symbol, relocate)

    for layer in project.mapLayers().values():
        relocate_layer(layer)
    for layout in project.layoutManager().printLayouts():
        for item in layout.items():
            if isinstance(item, QgsLayoutItemPicture):
                original = item.picturePath()
                replacement = relocate(original)
                if replacement != original:
                    item.setPicturePath(replacement)
            elif isinstance(item, QgsLayoutItemMap):
                overrides = item.layerStyleOverrides()
                for layer_id, xml in overrides.items():
                    layer = project.mapLayer(layer_id)
                    if layer is None:
                        continue
                    current = QgsMapLayerStyle()
                    current.readFromLayer(layer)
                    try:
                        QgsMapLayerStyle(xml).writeToLayer(layer)
                        relocate_layer(layer)
                        packaged = QgsMapLayerStyle()
                        packaged.readFromLayer(layer)
                        overrides[layer_id] = packaged.xmlData()
                    finally:
                        current.writeToLayer(layer)
                if overrides:
                    item.setLayerStyleOverrides(overrides)


def dispatch(action: str, payload: dict[str, Any], context: Any) -> dict[str, Any]:
    from qgis.core import (
        QgsCoordinateReferenceSystem,
        QgsPathResolver,
        QgsProject,
        QgsReferencedRectangle,
    )

    project = QgsProject.instance()
    project.clear()
    payload = {**payload, "strict_layout": action in {"project_create", "project_preflight"}}
    preprocessor_id = None
    try:
        if action in {"project_create", "project_preflight"}:
            supplied_plan = (payload.get("layout") or {}).get("layout_plan")
            if supplied_plan is not None:
                if supplied_plan.get("schema_version") != 1 or supplied_plan.get("plan_digest") != \
                        _json_digest({key: value for key, value in supplied_plan.items()
                                      if key != "plan_digest"}):
                    raise ValueError("layout_plan digest is invalid; run preflight again")
                if supplied_plan.get("input_digest") != _layout_input_digest(payload):
                    raise ValueError("layout_plan is stale: layout inputs changed; "
                                     "run preflight again")
                if supplied_plan.get("runtime_identity") != _runtime_identity():
                    raise ValueError("layout_plan is stale: QGIS/Qt runtime changed; "
                                     "run preflight again")
            source_identities = _native_source_identities(payload, context)
            if supplied_plan is not None:
                if supplied_plan.get("source_identities") != source_identities:
                    raise ValueError("layout_plan is stale: data source changed; "
                                     "run preflight again")
            layers = [_new_layer(spec, context) for spec in _native_input_specs(payload, context)]
            if not layers:
                raise ValueError("create requires at least one local vector/raster layer")
            crs = (
                QgsCoordinateReferenceSystem(payload["crs"])
                if payload.get("crs")
                else layers[_map_indices(layers)[0]].crs()
            )
            if not crs.isValid():
                raise ValueError("Invalid project CRS")
            project.setCrs(crs)
            project.setTitle(payload.get("title", ""))
            for index, layer in enumerate(layers):
                layer.setCustomProperty("geoharness/source_index", index)
                layer.setCustomProperty("geoharness/role",
                                        payload["layers"][index].get("role", "map"))
                layer_key = payload["layers"][index].get("layer_key", "")
                if not layer_key:
                    from geoharness.qgis.layer_identity import stable_layer_key

                    source = payload["layers"][index]
                    path, _ = _local_source(source["file_path"], context)
                    layer_key = stable_layer_key(source.get("kind", "vector"), layer.name(),
                                                 str(path), source.get("layer_name", ""))
                layer.setCustomProperty("geoharness/layer_key", layer_key)
                bindings = (payload.get("_creation_spec") or {}).get("source_bindings", [])
                if index < len(bindings) and bindings[index].get("snapshot_primary_key"):
                    layer.setCustomProperty("geoharness/snapshot_primary_key",
                                            bindings[index]["snapshot_primary_key"])
                    layer.setCustomProperty("geoharness/business_fields",
                                            bindings[index]["business_fields"])
                project.addMapLayer(layer)
                if layer.customProperty("geoharness/role", "map") == "table_only":
                    project.layerTreeRoot().findLayer(layer.id()).setItemVisibilityChecked(False)
            project.layerTreeRoot().setHasCustomLayerOrder(True)
            project.layerTreeRoot().setCustomLayerOrder(list(reversed(layers)))
            extent = _extent(project, layers, payload.get("map_extent"))
            project.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(extent, crs))
            layout = _create_layout(project, layers, extent, payload, context)
            layout_plan = _freeze_layout_plan(layout, project, layers, payload, context)
            if layout_plan["source_identities"] != source_identities:
                raise ValueError("layout_plan is stale: data source changed while measuring; "
                                 "run preflight again")
            from qgis.core import QgsRectangle

            project.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(
                QgsRectangle(*layout_plan["frames"]["main_map"]["extent"]), crs))
            if supplied_plan is not None:
                _validate_plan_layout(layout, project, layers, payload, supplied_plan, context)
                if layout_plan["adaptations"] != supplied_plan["adaptations"]:
                    raise ValueError("layout_plan is stale: native adaptations changed; "
                                     "run preflight again")
                layout_plan = supplied_plan
            composition, frame_checks = _frame_evidence(
                project, layers, payload, layout_plan["frames"])
            if action == "project_create":
                project.layoutManager().removeLayout(layout)
                payload = {**payload, "layout": layout_plan["parameters"],
                           "_frozen_frames": layout_plan["frames"], "_consume_plan": True}
                layout = _create_layout(project, layers, extent, payload, context,
                                        _adaptations=layout_plan["adaptations"])
                _validate_plan_layout(layout, project, layers, payload, layout_plan, context)
            for key, value in (("layout_plan", layout_plan), ("composition", composition),
                               ("frame_checks", frame_checks)):
                layout.setCustomProperty("geoharness/" + key, json.dumps(value, ensure_ascii=False))
            if action == "project_preflight":
                return {"layout": _layout_metadata(layout), "layout_plan": layout_plan,
                        "composition": composition, "frame_checks": frame_checks,
                        "render_executed": False}
            return _save_package(project, layout, layers, payload, context)
        if action not in {"project_inspect", "project_export"}:
            raise ValueError(f"Unsupported QGIS project action: {action}")
        source_project = context.read_path(payload["project_path"])
        asset_sources: dict[str, Path] = {}
        assets = _preflight_project(source_project, context, asset_sources)
        _register_project_fonts(source_project, context)
        if action == "project_export" and assets:
            replacements = {}
            for index, asset in enumerate(assets):
                if asset.suffix.lower() in _FONT_SUFFIXES:
                    target, _ = _snapshot_font(asset, context)
                else:
                    target = Path(
                        context.output_path(None, f"assets/{index}{asset.suffix.lower()}")
                    )
                    shutil.copy2(asset, target)
                replacements[str(asset)] = str(target)
            aliases = {name: replacements[str(asset)] for name, asset in asset_sources.items()}

            def relocate_asset(value: str) -> str:
                if value in aliases:
                    return aliases[value]
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = source_project.parent / candidate
                return replacements.get(str(candidate.resolve()), value)

            preprocessor_id = QgsPathResolver.setPathPreprocessor(relocate_asset)
        if not project.read(str(source_project)):
            raise ValueError(f"QGIS could not read project: {project.error()}")
        _restore_font_metadata(
            project, source_project, context, package=action == "project_export"
        )
        if action == "project_export" and assets:
            _relocate_project_assets(project, relocate_asset)
        layers = _project_layers(project)
        if not layers or not all(layer.isValid() for layer in layers):
            raise ValueError("Project has invalid or missing local layers")
        layouts = project.layoutManager().printLayouts()
        _validate_project_coordinates(project, layouts)
        if action == "project_inspect":
            return {
                "backend": "qgis",
                "project_file": str(source_project),
                "crs": project.crs().authid(),
                "crs_valid": project.crs().isValid(),
                "layers": [_layer_metadata(layer) for layer in layers],
                "layouts": [_layout_metadata(layout) for layout in layouts],
                "quality_status": "unverified",
                "visual_review_status": "unverified",
            }
        requested = payload.get("layout_name")
        if requested:
            layout = next((item for item in layouts if item.name() == requested), None)
            if layout is None:
                raise ValueError(f"Saved print layout was not found: {requested}")
        elif len(layouts) == 1:
            layout = layouts[0]
        else:
            raise ValueError("Export requires one saved layout or an explicit layout_name")
        return _save_package(project, layout, layers, payload, context)
    finally:
        if preprocessor_id is not None:
            QgsPathResolver.removePathPreprocessor(preprocessor_id)
        project.clear()
