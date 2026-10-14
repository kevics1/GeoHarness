"""Bounded native analysis evidence tied to the layers actually being mapped.

This verifies persisted output integrity/counts and recomputes area from output
polygons. Other spatial operations are not rerun; arbitrary manifest prose is
never statistical evidence. Legacy manifests have
no original output fingerprints and therefore receive a narrower verification.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from pathlib import Path
from typing import Any

from pyproj import CRS

from geoharness._proj_fix import ensure_proj_data
from geoharness.analysis.vector import _AREA_SEMANTICS, _COUNT_SEMANTICS, polygon_area_statistics
from geoharness.cartography.values import frame_content
from geoharness.qgis.worker_context import WorkerContext

_ACTIONS = {"buffer", "clip", "intersection", "distance", "count", "impact", "area"}
_MAX_MANIFESTS = 8
_MANIFEST_BYTES = 1024 * 1024
_OUTPUT_BYTES = 64 * 1024 * 1024
_TOTAL_BYTES = 256 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_METHODS = {
    "buffer": "缓冲区",
    "clip": "裁剪",
    "intersection": "相交叠加",
    "distance": "最短平面距离",
    "count": "分区相交计数",
    "impact": "距离阈值影响分析",
    "area": "面积统计",
}


class AnalysisBindingError(ValueError):
    """A native result was supplied but none of its actual outputs is mapped."""

    def __init__(self, outputs: dict, mapped_sources: list[dict]) -> None:
        self.summary = "No native analysis output is an actual mapped layer."
        self.diagnostics = {
            "code": "analysis_output_not_mapped",
            "native_outputs": [{"name": label, "file_path": str(output["path"])}
                               for label, output in outputs.items()],
            "mapped_layers": [{"layer_index": source.get("layer_index", index),
                               "file_path": str(source.get("file_path", ""))}
                              for index, source in enumerate(mapped_sources)],
            "recovery": "Use a listed native output as a layer, optionally with "
                        "layer.derivation for verified subsets/new annotations. Do not bind "
                        "a manifest to unrelated or manually transformed Python outputs.",
        }
        super().__init__("No native analysis output is an actual mapped layer. "
                         + json.dumps(self.diagnostics, ensure_ascii=False))


def _binding_candidates(manifest: dict, outputs: dict, mapped_sources: list[dict],
                        reader: _EvidenceReader) -> list[dict]:
    """Suggest explicit replacements from persisted provenance, never layer names.

    These are user/model choices, not proof of equivalence. The ordinary output
    integrity and mapped-content checks still run on the next preflight.
    """
    provenance = manifest.get("sources")
    provenance = provenance if isinstance(provenance, dict) else {}
    candidates = []
    for label, output in outputs.items():
        origin = label if label in {"reference", "source", "zones"} else "source"
        if label == "result" and manifest.get("action") == "count":
            origin = "zones" if "zones" in provenance else "reference"
        original_source = provenance.get(origin)
        source_path = (original_source.get("file_path")
                       if isinstance(original_source, dict) else None)
        matches = []
        if source_path:
            try:
                original = reader.path(source_path)
                for index, source in enumerate(mapped_sources):
                    try:
                        if source.get("file_path") and reader.path(source["file_path"]) == original:
                            matches.append({"layer_index": source.get("layer_index", index),
                                            "file_path": source["file_path"]})
                    except ValueError:
                        continue
            except ValueError:
                pass
        suggestion = {"output": label, "file_path": str(output["path"]),
                      "provenance_role": origin, "candidate_layers": matches,
                      "match_basis": "manifest_source_path" if matches else "unresolved",
                      "automatic_application": False}
        if len(matches) == 1:
            suggestion["suggested_change"] = {
                "field": f"layers[{matches[0]['layer_index']}].file_path",
                "current": matches[0]["file_path"], "proposed": str(output["path"]),
            }
        suggestion["note"] = (
            "Choose the native output only if this layer should express that analysis; "
            "preserve its style and recheck fields/geometry. This is not an equivalence claim."
            if matches else "No unique source-path binding is available; add the native output "
            "explicitly or omit a manifest used only for a separate statistics table."
        )
        candidates.append(suggestion)
    return candidates


_GEOMETRY_SEMANTICS = {
    "buffer": "Planar buffers around source geometries in the metric CRS.",
    "clip": "Source geometries intersected with the union of reference geometries.",
    "intersection": "Pairwise source/reference intersections; output records are intersections.",
    "count": "Each source feature counts in every zone it intersects, including boundaries.",
    "distance": "Shortest planar distance between any parts of the geometries.",
    "impact": "Shortest planar distance between any parts of the geometries.",
    "area": _AREA_SEMANTICS,
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Analysis JSON has duplicate object keys.")
        result[key] = value
    return result


def _json(data: bytes) -> dict:
    try:
        value = json.loads(data.decode("utf-8-sig"), object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("Analysis evidence must be valid UTF-8 JSON.") from exc
    if not isinstance(value, dict):
        raise ValueError("Analysis evidence must contain a JSON object.")
    return value


class _EvidenceReader:
    def __init__(self, cwd: Path, read_roots: list[Path]):
        self.guard = WorkerContext(cwd, cwd, read_roots)
        self.remaining = _TOTAL_BYTES
        self.cache: dict[Path, bytes] = {}

    def path(self, value: Any) -> Path:
        if not isinstance(value, str) or not value or len(value) > 4096:
            raise ValueError("Analysis evidence requires a bounded local file path.")
        try:
            return self.guard.read_path(value)
        except (PermissionError, OSError, ValueError) as exc:
            # Do not echo a supplied URI, credential value or arbitrary file name.
            raise ValueError(
                "Analysis evidence file is unavailable or outside read roots."
            ) from exc

    def read(self, path: Path, limit: int) -> bytes:
        if path not in self.cache:
            try:
                if path.stat().st_size > min(limit, self.remaining):
                    raise ValueError("Analysis evidence exceeds the bounded file size budget.")
                with path.open("rb") as stream:
                    data = stream.read(min(limit, self.remaining) + 1)
            except OSError as exc:
                raise ValueError("Analysis evidence file could not be read.") from exc
            if len(data) > min(limit, self.remaining):
                raise ValueError("Analysis evidence exceeds the bounded file size budget.")
            self.remaining -= len(data)
            self.cache[path] = data
        data = self.cache[path]
        if len(data) > limit:
            raise ValueError("Analysis evidence exceeds the bounded file size budget.")
        return data


def _count(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Native analysis counts must be nonnegative integers.")
    return value


def _number(value: Any) -> float:
    if type(value) not in (int, float):
        raise ValueError("Native analysis distance must be a finite nonnegative number.")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Native analysis distance must be finite.") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError("Native analysis distance must be a finite nonnegative number.")
    return result


def _crs(value: Any, *, metric: bool = False) -> tuple[CRS, str]:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("Analysis and display CRS must be declared.")
    try:
        ensure_proj_data()
        crs = CRS.from_user_input(value)
    except Exception as exc:
        raise ValueError("Analysis or display CRS is invalid.") from exc
    if metric and (
        not crs.is_projected
        or len(crs.axis_info) < 2
        or any(not math.isclose(axis.unit_conversion_factor, 1) for axis in crs.axis_info[:2])
    ):
        raise ValueError("Analysis metric_crs must be projected with metre units.")
    authority = crs.to_authority()
    # Custom CRS names/WKT can contain arbitrary text; expose a stable identity.
    safe_name = (
        ":".join(authority)
        if authority
        else "CUSTOM:" + hashlib.sha256(crs.to_wkt().encode()).hexdigest()[:16]
    )
    return crs, safe_name


def _features(document: dict) -> list[dict]:
    features = document.get("features")
    if document.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise ValueError("Native analysis outputs must be GeoJSON FeatureCollections.")
    for feature in features:
        if (
            not isinstance(feature, dict)
            or feature.get("type") != "Feature"
            or not isinstance(feature.get("properties"), dict)
            or not isinstance(feature.get("geometry"), dict)
        ):
            raise ValueError("Native analysis output contains an invalid feature.")
    return features


def _content(features: list[dict]) -> list[dict]:
    # Native exports reset subset row indices. Feature IDs are not statistical data.
    return [{"geometry": item["geometry"], "properties": item["properties"]} for item in features]


def _declared_crs(document: dict) -> str:
    crs = document.get("crs")
    if crs is None:
        return "EPSG:4326"
    if not isinstance(crs, dict) or not isinstance(crs.get("properties"), dict):
        raise ValueError("GeoJSON CRS must contain an object with properties.name.")
    name = crs["properties"].get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("GeoJSON CRS properties.name must be a declared CRS string.")
    return name


def _output_labels(action: str, paths: dict) -> set[str]:
    required = {"result"}
    if action in {"clip", "intersection", "distance", "impact"}:
        required.add("reference")
    if action == "count":
        required.add("source")
    if action == "impact":
        required.add("affected")
    allowed = required | ({"zones"} if action == "impact" else set())
    if not required.issubset(paths) or not set(paths).issubset(allowed):
        raise ValueError("Manifest does not describe known native analysis outputs.")
    return required


def _read_outputs(
    manifest: dict, reader: _EvidenceReader, export_crs: CRS
) -> tuple[dict, dict, str]:
    paths = manifest["paths"]
    _output_labels(manifest["action"], paths)
    if any(not isinstance(item, dict) for item in paths.values()):
        raise ValueError("Native manifest output entries must be objects.")
    has_hashes = any("geojson_sha256" in item or "csv_sha256" in item for item in paths.values())
    outputs, fingerprints = {}, {}
    for label, entry in paths.items():
        fingerprint = {}
        for kind in ("geojson", "csv"):
            path = reader.path(entry.get(kind))
            if path.suffix.lower() != "." + kind:
                raise ValueError("Native analysis output has an unexpected file format.")
            data = reader.read(path, _OUTPUT_BYTES)
            digest = hashlib.sha256(data).hexdigest()
            expected = entry.get(kind + "_sha256")
            if has_hashes and (
                not isinstance(expected, str)
                or not _SHA256.fullmatch(expected)
                or expected != digest
            ):
                raise ValueError("Analysis output SHA256 mismatch or incomplete fingerprints.")
            fingerprint[kind] = str(path)
            fingerprint[kind + "_sha256"] = digest
            if kind == "geojson":
                document = _json(data)
                features = _features(document)
                declared = _declared_crs(document)
                actual_crs = _crs(declared or "EPSG:4326")[0]
                if actual_crs != export_crs:
                    raise ValueError("Analysis output CRS disagrees with export_crs.")
                if _count(entry.get("feature_count")) != len(features):
                    raise ValueError(
                        "Analysis output feature_count disagrees with actual features."
                    )
                fingerprint["feature_count"] = len(features)
                outputs[label] = {"path": path, "features": features,
                                  "content": _content(features), "bytes": data}
        fingerprints[label] = fingerprint
    return outputs, fingerprints, "sha256_verified" if has_hashes else "legacy_count_verified"


def _statistics(manifest: dict, outputs: dict, distance: float,
                metric_crs: CRS | None = None) -> tuple[dict, list[str]]:
    action, declared = manifest["action"], manifest["statistics"]
    result = outputs["result"]["features"]
    if _count(declared.get("feature_count")) != len(result):
        raise ValueError("Analysis statistics feature_count disagrees with actual features.")
    verified = {"feature_count": len(result)}
    source_count = _count(declared.get("source_count"))
    unverified = []
    if action in {"buffer", "distance", "impact", "count", "area"}:
        actual = len(outputs["source"]["features"]) if action == "count" else len(result)
        if source_count != actual:
            raise ValueError("Analysis source_count disagrees with actual output counts.")
        verified["source_count"] = actual
    else:
        unverified.append("source_count")
    if action == "area":
        import geopandas as gpd

        group_field = manifest["parameters"].get("group_field", "")
        if not isinstance(group_field, str) or len(group_field) > 128:
            raise ValueError("area statistics group_field must be a bounded string")
        frame = (gpd.GeoDataFrame.from_features(result, crs="EPSG:4326") if result
                 else gpd.GeoDataFrame(geometry=[], crs="EPSG:4326"))
        if not result and group_field:
            frame[group_field] = []
        frame = frame.to_crs(metric_crs)
        actual = polygon_area_statistics(frame, group_field,
            group_values=[item["properties"].get(group_field) for item in result])
        declared_areas = declared.get("area_statistics")

        def equivalent(expected, value):
            if isinstance(expected, dict):
                return (isinstance(value, dict) and set(value) == set(expected)
                        and all(equivalent(item, value[key]) for key, item in expected.items()))
            if isinstance(expected, list):
                return (isinstance(value, list) and len(value) == len(expected)
                        and all(equivalent(a, b) for a, b in zip(expected, value)))
            if isinstance(expected, float):
                return (type(value) in {int, float} and math.isfinite(value)
                        and math.isclose(expected, value, rel_tol=1e-8, abs_tol=1e-3))
            return type(value) is type(expected) and expected == value

        if not equivalent(actual, declared_areas) or any(
            not math.isclose(_number(item["properties"].get("area_m2")), area,
                             rel_tol=1e-8, abs_tol=1e-3)
            for item, area in zip(result, frame.geometry.area)
        ):
            raise ValueError("Native area statistics disagree with recomputed output polygons")
        verified["area_statistics"] = actual
    if action in {"distance", "impact"}:
        distances = [_number(item["properties"].get("distance_m")) for item in result]
        if "relation_counts" in declared:
            from collections import Counter

            import geopandas as gpd
            from shapely import union_all

            reference = outputs["reference"]["features"]
            hazards = gpd.GeoDataFrame.from_features(reference, crs=4326).to_crs(metric_crs)
            assets = (gpd.GeoDataFrame.from_features(result, crs=4326).to_crs(metric_crs) if result
                      else gpd.GeoDataFrame(geometry=[], crs=metric_crs))
            combined = union_all(hazards.geometry.array)
            relations = ["covered_by" if combined.covers(g) else
                         "intersects" if combined.intersects(g) else "outside"
                         for g in assets.geometry]
            counts = {relation: relations.count(relation)
                      for relation in ("covered_by", "intersects", "outside")}
            actual_distances = assets.geometry.distance(combined).tolist()
            if (counts != declared["relation_counts"] or relations != [
                item["properties"].get("reference_relation") for item in result
            ] or any(not math.isclose(a, b, rel_tol=1e-8, abs_tol=1e-4)
                     for a, b in zip(distances, actual_distances))):
                raise ValueError("Native reference relation/distance classification "
                                 "is inconsistent")
            verified["relation_counts"] = counts
            scope = manifest.get("reference_scope") or {}
            filters = scope.get("filters")
            if (not isinstance(filters, dict) or scope.get("combine") != "union"
                    or scope.get("feature_count") != len(reference)
                    or filters != manifest.get("sources", {}).get("reference", {}).get("filters")
                    or any(field not in feature["properties"]
                           or feature["properties"][field] != value
                           for feature in reference for field, value in filters.items())):
                raise ValueError("Reference classification scope disagrees with selected features")
            verified["reference_scope"] = {"combine": "union", "filters": filters,
                                           "feature_count": len(reference),
                                           "relation_field": "reference_relation"}
            breaks = manifest["parameters"].get("distance_breaks_m", [])
            if (not isinstance(breaks, list) or any(type(v) not in {int, float}
                or not math.isfinite(v) or v <= 0 for v in breaks)
                or any(a >= b for a, b in zip(breaks, breaks[1:]))):
                raise ValueError("Invalid distance classification thresholds")
            if breaks:
                expected_classes = []
                for relation, value in zip(relations, distances):
                    label, lower = relation, 0.0
                    if relation == "outside":
                        label = f"outside_gt_{breaks[-1]:g}m"
                        for upper in breaks:
                            if value <= upper:
                                label = f"outside_{lower:g}_{upper:g}m"
                                break
                            lower = upper
                    expected_classes.append(label)
                if (expected_classes != [f["properties"].get("distance_class") for f in result]
                        or dict(Counter(expected_classes)) != declared.get(
                            "distance_class_counts")):
                    raise ValueError("Native distance classification disagrees "
                                     "with reference facts")
                verified["distance_class_counts"] = dict(Counter(expected_classes))
        if action == "impact":
            flags = [item["properties"].get("affected") for item in result]
            if any(type(flag) is not bool for flag in flags):
                raise ValueError("Impact result affected values must be booleans.")
            if flags != [value <= distance for value in distances]:
                raise ValueError("Impact affected flags disagree with distance_m threshold.")
            affected = sum(flags)
            if (
                _count(declared.get("affected_count")) != affected
                or _count(declared.get("unaffected_count")) != len(result) - affected
                or len(outputs["affected"]["features"]) != affected
                or outputs["affected"]["content"]
                != _content([item for item, flag in zip(result, flags) if flag])
            ):
                raise ValueError(
                    "Impact affected/unaffected counts or subset disagree with outputs."
                )
            verified.update(affected_count=affected, unaffected_count=len(result) - affected)
    counts = (
        outputs["result"]["features"]
        if action == "count"
        else (outputs.get("zones", {}).get("features", []) if action == "impact" else [])
    )
    for item in counts:
        total = _count(item["properties"].get("total_count"))
        if action == "impact" and _count(item["properties"].get("affected_count")) > total:
            raise ValueError("Zone affected_count exceeds total_count.")
    return verified, unverified


def _mapped_outputs(
    outputs: dict, mapped_sources: list[dict], reader: _EvidenceReader
) -> list[dict]:
    matches = []
    for index, source in enumerate(mapped_sources):
        value = source.get("file_path")
        if not value:
            continue
        path = reader.path(value)
        exact = [label for label, output in outputs.items() if path == output["path"]]
        if source.get("derivation") is not None and not exact:
            # A declaration is a view of this verified source, never a claim that
            # unrelated hand-written data are an analytical result.
            continue
        # The renderer may already hold a cached frame. Disk evidence alone
        # cannot prove that those cached features are the manifest output.
        cached = source.get("gdf")
        if cached is not None:
            import geopandas as gpd

            if not isinstance(cached, gpd.GeoDataFrame) or cached.crs is None:
                raise ValueError("Mapped analysis evidence requires a declared GeoDataFrame CRS.")
            candidates = exact or list(outputs)
            matching = []
            for label in candidates:
                output = outputs[label]
                features = output["features"]
                if "driver_frame" not in output:
                    output["driver_frame"] = gpd.read_file(io.BytesIO(output["bytes"]))
                source_frame = output["driver_frame"]
                derivation = source.get("derivation")
                if derivation is not None:
                    from geoharness.cartography.derivation import MapDerivation, derive_frame

                    spec = MapDerivation.model_validate(derivation)
                    indices = (spec.feature_indices if spec.feature_indices is not None
                               else list(range(len(features))))
                    selected_features = [features[i] for i in indices if i < len(features)]
                    verified = derive_frame(source_frame, spec)
                else:
                    selected_features = features
                generated_ids = (
                    [f["id"] for f in selected_features]
                    if "id" in source_frame.columns and selected_features
                    and all("id" in f and "id" not in f["properties"] for f in features)
                    and source_frame["id"].tolist() == [f["id"] for f in features]
                    else None
                )
                if derivation is not None:
                    expected_content = frame_content(verified, generated_ids=generated_ids)
                elif "driver_content" not in output:
                    output["driver_content"] = frame_content(source_frame,
                                                              generated_ids=generated_ids)
                if derivation is None:
                    expected_content = output["driver_content"]
                if frame_content(cached, generated_ids=generated_ids) == expected_content:
                    matching.append(label)
            if not matching:
                if exact:
                    raise ValueError("Mapped data changed after it was loaded; rerun preflight.")
                continue
        if exact:
            if source.get("derivation") is not None:
                if cached is None:
                    raise ValueError("Derived map evidence requires actual cached features")
                matches.extend({
                    "output": label, "layer_index": source.get("layer_index", index),
                    "match": "derivation",
                    "source_feature_count": len(outputs[label]["features"]),
                    "mapped_feature_count": len(cached), "derivation": spec.model_dump(),
                } for label in exact)
                continue
            matches.extend(
                {"output": label, "layer_index": source.get("layer_index", index), "match": "path"}
                for label in exact
            )
            continue
        if path.suffix.lower() not in {".geojson", ".json"}:
            continue
        try:
            document = _json(reader.read(path, _OUTPUT_BYTES))
            content = _content(_features(document))
        except ValueError:
            continue
        declared = _declared_crs(document)
        if _crs(declared or "EPSG:4326")[0] != CRS.from_epsg(4326):
            continue
        matches.extend(
            {"output": label, "layer_index": source.get("layer_index", index), "match": "content"}
            for label, output in outputs.items()
            if content == output["content"]
        )
    if not matches:
        raise AnalysisBindingError(outputs, mapped_sources)
    return matches


def _area_note(stats: dict) -> str:
    areas = stats["area_statistics"]
    return (f"；要素面积之和 {areas['feature_area_sum_m2'] / 1e6:.2f} km²"
            f"；去重覆盖 {areas['union_area_m2'] / 1e6:.2f} km²")


def _footer(analyses: list[dict]) -> str:
    """At most three logical lines; detailed facts belong to the project report."""
    if not analyses:
        return ""
    if len(analyses) == 1:
        analysis = analyses[0]
        action, stats = analysis["action"], analysis["statistics"]
        line = f"{_METHODS[action]}：结果 {stats['feature_count']}"
        if action == "impact":
            line += f"，受影响 {stats['affected_count']}，未影响 {stats['unaffected_count']}"
        if action == "area":
            line += _area_note(stats) + "（重叠范围不可相加为覆盖）"
        if action in {"buffer", "impact"}:
            label = "缓冲距离" if action == "buffer" else "距离阈值 ≤"
            line += f"；{label} {analysis['parameters']['distance_m']:g} m"
        # Omit a whole oversized statistic rather than slicing its numeric value.
        lines = [line if len(line) <= 160 else
                 f"{_METHODS[action]}：完整统计与参数见工程事实报告"]
    else:
        methods = list(dict.fromkeys(item["action"] for item in analyses))
        labels = "、".join(_METHODS[action] for action in methods)
        lines = [f"分析 {len(analyses)} 项（{labels}）；完整统计与参数见工程事实报告"]

    def crs_note(key: str, label: str) -> str:
        values = list(dict.fromkeys(item[key] for item in analyses))
        text = "、".join(values)
        return f"{label} {text}" if len(text) <= 48 else f"{label}详见工程事实报告"

    lines.append(crs_note("metric_crs", "计量CRS") + "；"
                 + crs_note("display_crs", "显示CRS"))
    notes = []
    if any(item["action"] == "count" or "zones" in item["output_fingerprints"]
           for item in analyses):
        notes.append("按相交（含边界）逐分区计数，可重复计入，不可相加为全局去重总数")
    subsets = [match for item in analyses for match in item["mapped_outputs"]
               if match["match"] == "derivation"]
    if subsets:
        count = (f" {subsets[0]['mapped_feature_count']}/{subsets[0]['source_feature_count']}"
                 if len(subsets) == 1 else "")
        notes.append(f"制图子集{count}（统计仍为完整分析输出）")
    if any(item["verification"] == "legacy_count_verified" for item in analyses):
        notes.append("旧清单仅核对输出计数")
    if notes:
        lines.append("；".join(notes))
    return "\n".join(lines)


def infer_analysis_manifests(mapped_sources: list[dict], *, cwd: Path,
                             read_roots: list[Path]) -> list[str]:
    """Recognize only adjacent native manifests explicitly listing this local output."""
    reader = _EvidenceReader(cwd, read_roots)
    manifests = []
    for source in mapped_sources:
        try:
            path = reader.path(source.get("file_path"))
            manifest_path = reader.path(str(path.parent / "manifest.json"))
            document = _json(reader.read(manifest_path, _MANIFEST_BYTES))
            if not isinstance(document.get("action"), str) or (
                document["action"] not in _ACTIONS
            ) or not isinstance(
                document.get("paths"), dict,
            ):
                continue
            if not any(isinstance(entry, dict) and entry.get("geojson")
                       and reader.path(entry["geojson"]) == path
                       for entry in document["paths"].values()):
                continue
            if str(manifest_path) not in manifests:
                manifests.append(str(manifest_path))
        except ValueError:
            continue
    return manifests


def load_analysis_facts(
    manifests: list[str],
    *,
    cwd: Path,
    read_roots: list[Path],
    mapped_sources: list[dict],
    display_crs: str,
) -> dict[str, Any]:
    """Validate native manifests and produce safe, output-backed map facts.

    Mapped sources use original local ``file_path`` locators, including when the
    renderer uses a temporary bridge copy. Labels/free text are never evidence.
    An optional cached ``gdf`` must also match the verified disk output, so an
    earlier read cannot attach current statistics to different rendered data.
    """
    if not isinstance(manifests, list) or len(manifests) > _MAX_MANIFESTS:
        raise ValueError("Provide at most eight native analysis manifest paths.")
    if not manifests:
        return {"status": "none", "analyses": [], "footer_text": ""}
    display_name = _crs(display_crs)[1]
    reader = _EvidenceReader(Path(cwd), read_roots)
    analyses, seen = [], set()
    for value in manifests:
        path = reader.path(value)
        if path in seen:
            raise ValueError("Native analysis manifest paths must be unique.")
        seen.add(path)
        raw = reader.read(path, _MANIFEST_BYTES)
        manifest = _json(raw)
        action = manifest.get("action")
        if (
            not isinstance(action, str)
            or action not in _ACTIONS
            or any(
                not isinstance(manifest.get(key), dict)
                for key in ("parameters", "statistics", "paths")
            )
        ):
            raise ValueError("Only known geo_analysis native manifests are supported.")
        metric_crs, metric_name = _crs(manifest.get("metric_crs"), metric=True)
        export_crs, export_name = _crs(manifest.get("export_crs"))
        if export_crs != CRS.from_epsg(4326):
            raise ValueError("Native analysis export_crs must be EPSG:4326.")
        distance = _number(manifest["parameters"].get("distance_m"))
        if (action == "buffer" and distance <= 0) or (
            action not in {"buffer", "impact"} and distance != 0
        ):
            raise ValueError("Native analysis distance_m is inconsistent with action.")
        outputs, fingerprints, verification = _read_outputs(manifest, reader, export_crs)
        statistics, unverified = _statistics(manifest, outputs, distance, metric_crs)
        try:
            matches = _mapped_outputs(outputs, mapped_sources, reader)
        except AnalysisBindingError as exc:
            exc.diagnostics.update(manifest_path=str(path), action=action)
            exc.diagnostics["replacement_suggestions"] = _binding_candidates(
                manifest, outputs, mapped_sources, reader)
            exc.args = ("No native analysis output is an actual mapped layer. "
                        + json.dumps(exc.diagnostics, ensure_ascii=False),)
            raise
        analyses.append(
            {
                "manifest_path": str(path),
                "manifest_digest": hashlib.sha256(raw).hexdigest(),
                "action": action,
                "metric_crs": metric_name,
                "export_crs": export_name,
                "display_crs": display_name,
                "parameters": {"distance_m": distance,
                               **({"group_field": manifest["parameters"].get("group_field", "")}
                                  if action == "area" else {})},
                "statistics": statistics,
                "unverified_statistics": unverified,
                "count_semantics": _COUNT_SEMANTICS if action in {"count", "impact"} else "",
                "geometry_semantics": _GEOMETRY_SEMANTICS[action],
                "mapped_outputs": matches,
                "output_fingerprints": fingerprints,
                "verification": verification,
                "verification_scope": (
                    "Persisted output SHA256 integrity and output counts; "
                    + ("polygon areas recomputed from output in metric_crs; "
                       if action == "area" else "")
                    + ("reference geometry relations, distances and classes recomputed; "
                       if "relation_counts" in statistics else "")
                    + "original source data have not been reprocessed."
                    if verification == "sha256_verified"
                    else "Legacy persisted output counts only; no original output fingerprints and "
                    "no recomputation of the original spatial analysis."
                ),
            }
        )
    return {"status": "verified", "analyses": analyses, "footer_text": _footer(analyses)}
