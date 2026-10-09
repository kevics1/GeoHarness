"""Bounded native analysis evidence tied to the layers actually being mapped.

This verifies persisted output integrity and counts, never reruns analysis or
turns arbitrary manifest prose into statistical evidence. Legacy manifests have
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
from geoharness.analysis.vector import _COUNT_SEMANTICS
from geoharness.cartography.values import frame_content
from geoharness.qgis.worker_context import WorkerContext

_ACTIONS = {"buffer", "clip", "intersection", "distance", "count", "impact"}
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
}
_GEOMETRY_SEMANTICS = {
    "buffer": "Planar buffers around source geometries in the metric CRS.",
    "clip": "Source geometries intersected with the union of reference geometries.",
    "intersection": "Pairwise source/reference intersections; output records are intersections.",
    "count": "Each source feature counts in every zone it intersects, including boundaries.",
    "distance": "Shortest planar distance between any parts of the geometries.",
    "impact": "Shortest planar distance between any parts of the geometries.",
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


def _statistics(manifest: dict, outputs: dict, distance: float) -> tuple[dict, list[str]]:
    action, declared = manifest["action"], manifest["statistics"]
    result = outputs["result"]["features"]
    if _count(declared.get("feature_count")) != len(result):
        raise ValueError("Analysis statistics feature_count disagrees with actual features.")
    verified = {"feature_count": len(result)}
    source_count = _count(declared.get("source_count"))
    unverified = []
    if action in {"buffer", "distance", "impact", "count"}:
        actual = len(outputs["source"]["features"]) if action == "count" else len(result)
        if source_count != actual:
            raise ValueError("Analysis source_count disagrees with actual output counts.")
        verified["source_count"] = actual
    else:
        unverified.append("source_count")
    if action in {"distance", "impact"}:
        distances = [_number(item["properties"].get("distance_m")) for item in result]
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
        raise ValueError("No native analysis output is an actual mapped layer.")
    return matches


def _footer(analyses: list[dict]) -> str:
    lines = []
    for analysis in analyses:
        action, stats = analysis["action"], analysis["statistics"]
        line = f"{_METHODS[action]}：结果 {stats['feature_count']}"
        if action == "impact":
            line += f"，受影响 {stats['affected_count']}，未影响 {stats['unaffected_count']}"
        if action in {"buffer", "impact"}:
            label = "缓冲距离" if action == "buffer" else "距离阈值 ≤"
            line += f"；{label} {analysis['parameters']['distance_m']:g} m"
        line += f"；计量CRS {analysis['metric_crs']}；显示CRS {analysis['display_crs']}"
        for match in analysis["mapped_outputs"]:
            if match["match"] == "derivation":
                line += (f"；制图子集 {match['mapped_feature_count']}/"
                         f"{match['source_feature_count']}（统计仍为完整分析输出）")
        if action == "count" or "zones" in analysis["output_fingerprints"]:
            line += "；按相交（含边界）逐分区计数，可重复计入，不可相加为全局去重总数"
        if analysis["verification"] == "legacy_count_verified":
            line += "；旧清单仅核对输出计数"
        lines.append(line)
    # Eight analyses cannot fit a small footer: retain each method/count summary.
    if len("\n".join(lines)) > 600:
        lines = [
            f"{_METHODS[item['action']]}：结果 {item['statistics']['feature_count']}"
            + (
                f"，受影响 {item['statistics']['affected_count']}，"
                f"未影响 {item['statistics']['unaffected_count']}"
                if item["action"] == "impact"
                else ""
            )
            for item in analyses
        ]
        lines.append("完整参数、计量CRS、显示CRS及验证范围见分析事实报告。")
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
        metric_name = _crs(manifest.get("metric_crs"), metric=True)[1]
        export_crs, export_name = _crs(manifest.get("export_crs"))
        if export_crs != CRS.from_epsg(4326):
            raise ValueError("Native analysis export_crs must be EPSG:4326.")
        distance = _number(manifest["parameters"].get("distance_m"))
        if (action == "buffer" and distance <= 0) or (
            action not in {"buffer", "impact"} and distance != 0
        ):
            raise ValueError("Native analysis distance_m is inconsistent with action.")
        outputs, fingerprints, verification = _read_outputs(manifest, reader, export_crs)
        statistics, unverified = _statistics(manifest, outputs, distance)
        matches = _mapped_outputs(outputs, mapped_sources, reader)
        analyses.append(
            {
                "manifest_path": str(path),
                "manifest_digest": hashlib.sha256(raw).hexdigest(),
                "action": action,
                "metric_crs": metric_name,
                "export_crs": export_name,
                "display_crs": display_name,
                "parameters": {"distance_m": distance},
                "statistics": statistics,
                "unverified_statistics": unverified,
                "count_semantics": _COUNT_SEMANTICS if action in {"count", "impact"} else "",
                "geometry_semantics": _GEOMETRY_SEMANTICS[action],
                "mapped_outputs": matches,
                "output_fingerprints": fingerprints,
                "verification": verification,
                "verification_scope": (
                    "Persisted output SHA256 integrity and output counts; "
                    "original spatial analysis has not been recomputed."
                    if verification == "sha256_verified"
                    else "Legacy persisted output counts only; no original output fingerprints and "
                    "no recomputation of the original spatial analysis."
                ),
            }
        )
    return {"status": "verified", "analyses": analyses, "footer_text": _footer(analyses)}
