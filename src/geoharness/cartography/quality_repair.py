"""Evidence-bound presentation plans; all GIS validation stays in the native create tool."""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from geoharness.cartography.quality import MapIssue, MapReview
from geoharness.config.settings import GeoConfig


class RepairUnavailable(ValueError):
    """A review can report issues without authorizing an executable repair."""


@dataclass
class RepairPlan:
    parameters: Any
    source_bindings: list[dict]
    digest: str
    issue_ids: list[str]
    strategy: str


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verified_package(
    image: Path, receipt: dict, *, cwd: Path, config: GeoConfig, require_facts: bool = True
) -> dict:
    """Only a registered image and all unchanged receipt files authorize rebuilding."""
    from geoharness.cartography.jobs import _matching_version

    digest = _sha(image)
    version = _matching_version(receipt, image)
    if version is None:
        raise RepairUnavailable("Image has no registered package version.")
    stage = version.get("stages", {}).get("packaged", {})
    if stage.get("status") != "passed" or not stage.get("files"):
        raise RepairUnavailable("Package integrity is not verified.")
    root = Path(stage.get("project_dir", "")).resolve()
    roots = [cwd.resolve()]
    for section in (config.cartography, config.qgis, config.execution):
        if section.outputs_dir:
            directory = Path(section.outputs_dir).expanduser()
            roots.append((directory if directory.is_absolute() else cwd / directory).resolve())
    if not any(root.is_relative_to(directory) for directory in roots):
        raise RepairUnavailable("Package is outside authorized artifact roots.")
    recorded = {}
    for item in stage["files"]:
        path = Path(item["path"]).resolve()
        if not path.is_relative_to(root) or not item.get("sha256") or _sha(path) != item["sha256"]:
            raise RepairUnavailable("A packaged file changed; original candidate retained.")
        recorded[path] = item["sha256"]
    manifest_path = Path(stage.get("manifest_path", "")).resolve()
    if manifest_path not in recorded or manifest_path.stat().st_size > 1024 * 1024:
        raise RepairUnavailable("No bounded hash-bound native manifest.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("backend") != "qgis"
        or manifest.get("preview_sha256") != digest
        or (root / manifest.get("preview", "")).resolve() not in recorded
    ):
        raise RepairUnavailable("Native manifest does not describe the reviewed PNG.")
    declared = manifest.get("files", [])
    if any(
        not isinstance(name, str) or (root / name).resolve() not in recorded for name in declared
    ):
        raise RepairUnavailable("Manifest references an unregistered package file.")
    facts = manifest.get("native_facts") or {}
    if require_facts and facts.get("image_sha256") != digest:
        raise RepairUnavailable("Native geometry facts are not bound to the current PNG.")
    return {
        "manifest": manifest,
        "root": root,
        "recorded": recorded,
        "version": version,
        "image_sha256": digest,
    }


def _roi(issue: MapIssue, review: MapReview) -> dict:
    rois = review.diagnostics.get("review_rois", [])
    roi = next(
        (
            r
            for r in rois
            if r.get("roi_ref") == issue.roi_ref
            and r.get("frame_id") == issue.frame_id
            and not r.get("resampled")
        ),
        None,
    )
    if not issue.issue_id or roi is None or issue.bbox is None:
        raise RepairUnavailable("Repair needs a concrete issue ID, frame and original-pixel ROI.")
    left, top, right, bottom = issue.bbox
    if left >= right or top >= bottom:
        raise RepairUnavailable("Issue ROI rectangle has no positive area.")
    return roi


def _issue_extent(issue: MapIssue, review: MapReview, frame: dict) -> list[float]:
    roi = _roi(issue, review)
    crop = roi["pixel_bbox"]
    region = roi["region_bbox"]
    box = [
        crop[0] + issue.bbox[0] * (crop[2] - crop[0]),
        crop[1] + issue.bbox[1] * (crop[3] - crop[1]),
        crop[0] + issue.bbox[2] * (crop[2] - crop[0]),
        crop[1] + issue.bbox[3] * (crop[3] - crop[1]),
    ]
    xmin, ymin, xmax, ymax = frame["extent"]
    xscale = (xmax - xmin) / (region[2] - region[0])
    yscale = (ymax - ymin) / (region[3] - region[1])
    return [
        xmin + (box[0] - region[0]) * xscale,
        ymax - (box[3] - region[1]) * yscale,
        xmin + (box[2] - region[0]) * xscale,
        ymax - (box[1] - region[1]) * yscale,
    ]


def _covering_pair(manifest: dict, issue: MapIssue, review: MapReview) -> tuple[int, int] | None:
    """Prove a local opaque single circle covers an actual classified point circle."""
    frame = manifest["native_facts"]["frames"].get(issue.frame_id, {})
    order = frame.get("source_indices", [])  # actual native top-to-bottom order
    metadata = {
        layer.get("source_index", i): layer for i, layer in enumerate(manifest.get("layers", []))
    }
    bounds = _issue_extent(issue, review, frame)
    points = [
        p
        for p in frame.get("points", [])
        if p.get("layer_index") in issue.source_indices
        and bounds[0] <= p.get("x", math.inf) <= bounds[2]
        and bounds[1] <= p.get("y", math.inf) <= bounds[3]
    ]
    for lower in points:
        base = lower["layer_index"]
        if metadata.get(base, {}).get("renderer") not in {"graduatedSymbol", "categorizedSymbol"}:
            continue
        for upper in points:
            overlay = upper["layer_index"]
            if (
                metadata.get(overlay, {}).get("renderer") != "singleSymbol"
                or base not in order
                or overlay not in order
                or order.index(overlay) >= order.index(base)
                or lower["x"] != upper["x"]
                or lower["y"] != upper["y"]
            ):
                continue
            small, large = lower.get("symbol") or {}, upper.get("symbol") or {}
            if (
                str(small.get("marker_shape", "")).lower() == "circle"
                and str(large.get("marker_shape", "")).lower() == "circle"
                and small.get("symbol_layer_count") == 1
                and small.get("symbol_layer_type") == "SimpleMarker"
                and small.get("data_defined") is False
                and large.get("symbol_layer_count") == 1
                and large.get("symbol_layer_type") == "SimpleMarker"
                and large.get("data_defined") is False
                and large.get("color_alpha", 0)
                * large.get("layer_opacity", 0)
                * large.get("symbol_opacity", 0)
                == 1
                and large.get("outline_alpha", 0) > 0
                and large.get("width_mm", 0) > 0
                and large.get("size_mm", 0) >= small.get("size_mm", math.inf)
            ):
                return base, overlay
    return None


def previous_conflicts(receipt: dict, image_sha256: str) -> list[dict]:
    """Keep bounded structured conflicts, without provider text or arbitrary history."""
    result = []
    for version in receipt.get("versions", [])[-3:]:
        if version.get("image_sha256") == image_sha256:
            continue
        visual = version.get("stages", {}).get("visual_checked", {})
        if visual.get("status") != "issues":
            continue
        for issue in visual.get("report", {}).get("issues", [])[:5]:
            if issue.get("issue_id") and issue.get("roi_ref") and issue.get("source_indices"):
                result.append(
                    {
                        k: issue.get(k)
                        for k in ("issue_code", "frame_id", "source_indices", "bbox", "roi_ref")
                    }
                )
    return result[-10:]


def _repeat(issue: MapIssue, receipt: dict, digest: str) -> bool:
    return any(
        old["issue_code"] == issue.issue_code
        and old["frame_id"] == issue.frame_id
        and old["source_indices"] == issue.source_indices
        and old["bbox"] == issue.bbox
        and old["roi_ref"] == issue.roi_ref
        for old in previous_conflicts(receipt, digest)
    )


def _nullable_dict(container: dict, key: str) -> dict:
    value = container.get(key)
    if value is None:
        value = container[key] = {}
    if not isinstance(value, dict):
        raise RepairUnavailable(f"Creation spec {key} must be an object or null.")
    return value


def build_repair_plan(
    image: Path, review: MapReview, receipt: dict, *, config: GeoConfig, cwd: Path
) -> RepairPlan:
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput

    if review.status != "issues":
        raise RepairUnavailable("No complete visual issues review authorizes repair.")
    package = verified_package(image, receipt, cwd=cwd, config=config)
    manifest, root = package["manifest"], package["root"]
    spec = manifest.get("creation_spec")
    if not isinstance(spec, dict):
        raise RepairUnavailable("Saved or edited project has no reproducible creation spec.")
    raw = copy.deepcopy(spec.get("parameters"))
    sources = copy.deepcopy(spec.get("source_bindings"))
    if not isinstance(raw, dict) or not isinstance(raw.get("layers"), list):
        raise RepairUnavailable("Creation parameters need a complete native layer array.")
    if not isinstance(sources, list) or not all(isinstance(s, dict) for s in sources):
        raise RepairUnavailable("Creation spec needs complete source bindings.")
    raw.update(action="create", map_id=receipt["job_id"], output_path="")
    raw.pop("project_path", None)
    layout = _nullable_dict(raw, "layout")
    layout.pop("layout_plan", None)
    if len(sources) != len(raw["layers"]):
        raise RepairUnavailable("Creation source bindings do not match the complete layer array.")
    for layer in raw["layers"]:
        if not isinstance(layer, dict) or not isinstance(layer.get("file_path"), str):
            raise RepairUnavailable("Creation layers need packaged file paths.")
        if layer.get("kind", "vector") == "vector":
            _nullable_dict(layer, "style")
        path = (root / layer["file_path"]).resolve()
        if path not in package["recorded"]:
            raise RepairUnavailable("Creation spec data must be registered packaged snapshots.")
        layer["file_path"] = str(path)
        layer.pop("derivation", None)  # Packaged data already contains the verified derivation.
        labels = _nullable_dict(layer, "label_style") if layer.get("kind", "vector") == "vector" \
            else layer.get("label_style") or {}
        if labels.get("font_file"):
            font = (root / labels["font_file"]).resolve()
            if font not in package["recorded"]:
                raise RepairUnavailable("Label font must be a registered package resource.")
            labels["font_file"] = str(font)
    if layout.get("font_file"):
        font = (root / layout["font_file"]).resolve()
        if font not in package["recorded"]:
            raise RepairUnavailable("Creation font is not a verified package resource.")
        layout["font_file"] = str(font)
    raw["analysis_manifests"] = list(spec.get("analysis_manifests", []))
    issues = {i.issue_id: i for i in review.issues if i.severity in {"warning", "error"}}
    if len(issues) != len([i for i in review.issues if i.severity in {"warning", "error"}]):
        raise RepairUnavailable("Actionable issues require unique identifiers.")
    patch, strategy = review.qgis_patch, review.next_strategy
    identifiers = patch.issue_ids if patch is not None else strategy.issue_ids if strategy else []
    if not identifiers or any(i not in issues for i in identifiers):
        raise RepairUnavailable("Presentation proposal is not bound to concrete issues.")
    selected = [issues[i] for i in identifiers]
    frames = manifest["native_facts"].get("frames", {})
    for issue in selected:
        _roi(issue, review)
        if issue.native_assertion != "none":
            raise RepairUnavailable("A native assertion alone cannot authorize visual repair.")
        if any(i >= len(raw["layers"]) for i in issue.source_indices):
            raise RepairUnavailable("Issue references unknown original source indices.")
        if any(type(i) is not int or i < 0 for i in issue.source_indices):
            raise RepairUnavailable("Source references must be actual nonnegative integers.")
        if issue.frame_id in frames:
            if not set(issue.source_indices) <= set(
                frames[issue.frame_id].get("source_indices", [])
            ):
                raise RepairUnavailable("Issue sources are not drawn in the native frame.")
        elif issue.frame_id == "legend":
            rows = (manifest.get("layout", {}).get("legends", {}).get("legend", {}).get("rows", []))
            if not set(issue.source_indices) <= {row.get("layer_index") for row in rows}:
                raise RepairUnavailable("Legend issue sources have no actual legend nodes.")
        elif issue.frame_id != "footer" or issue.source_indices:
            raise RepairUnavailable("Non-map ROI only supports its measured layout presentation.")
    mode = strategy.strategy if strategy and set(strategy.issue_ids) == set(identifiers) else None
    if any(_repeat(i, receipt, package["image_sha256"]) for i in selected):
        mode = (
            "split_maps"
            if any(_covering_pair(manifest, i, review) for i in selected)
            else "detail_map"
        )
    if mode in {"detail_map", "split_maps"}:
        issue = selected[0]
        if issue.frame_id not in frames or not issue.source_indices:
            raise RepairUnavailable("Detail/split needs a native frame and measured subject.")
        extent = _issue_extent(issue, review, frames[issue.frame_id])
        actual = frames[issue.frame_id]
        points = [
            p
            for p in actual.get("points", [])
            if p.get("layer_index") in issue.source_indices
            and extent[0] <= p.get("x", math.inf) <= extent[2]
            and extent[1] <= p.get("y", math.inf) <= extent[3]
        ]
        polygons = [
            b
            for b in actual.get("layer_bboxes", [])
            if b.get("layer_index") in issue.source_indices and b.get("bbox")
            and b.get("sample_size") == 1
            and b["bbox"][0] <= extent[2] and b["bbox"][2] >= extent[0]
            and b["bbox"][1] <= extent[3] and b["bbox"][3] >= extent[1]
        ]
        if not points and not polygons:
            raise RepairUnavailable("ROI has no measured native feature for a detail subject.")
        layout.pop("detail_extent", None)
        original = list(range(len(raw["layers"])))
        main = _nullable_dict(layout, "main_frame")
        if main.get("render_layers") is None:
            main["render_layers"] = original
        detail = {
            "render_layers": list(main["render_layers"]),
        }
        if mode == "split_maps":
            pair = _covering_pair(manifest, issue, review)
            if pair is None:
                raise RepairUnavailable(
                    "Split requires actual coincident classified/overlay points."
                )
            base, overlay = pair
            main["render_layers"] = [i for i in main["render_layers"] if i != overlay]
            if main.get("focus_layers"):
                main["focus_layers"] = [i for i in main["focus_layers"] if i != overlay] or [base]
            detail["render_layers"] = [i for i in original if i != base]
            detail["focus_layers"] = [overlay]
        else:
            # Crop only the actual subject implicated by the original-pixel ROI.
            # Aggregate polygon boxes with multiple features do not prove a local subject.
            bounds = [[p["x"], p["y"], p["x"], p["y"]] for p in points]
            bounds.extend(b["bbox"] for b in polygons)
            west, south = min(b[0] for b in bounds), min(b[1] for b in bounds)
            east, north = max(b[2] for b in bounds), max(b[3] for b in bounds)
            pad = max(config.cartography.extent_padding, 0.05)
            dx = max((east - west) * pad, (extent[2] - extent[0]) * 0.05)
            dy = max((north - south) * pad, (extent[3] - extent[1]) * 0.05)
            layout["detail_extent"] = {"xmin": west - dx, "ymin": south - dy,
                                       "xmax": east + dx, "ymax": north + dy}
        layout["detail_frame"] = detail
        retained = set(main["render_layers"]) | set(detail["render_layers"])
        if any(
            layer.get("required", True) and i not in retained
            for i, layer in enumerate(raw["layers"])
        ):
            raise RepairUnavailable("Detail/split would omit required layer content.")
    elif patch is not None:
        mode = "adjust_presentation"
        if patch.layout:
            layout.update(patch.layout.model_dump(exclude_none=True))
        if patch.dpi is not None:
            raw["dpi"] = patch.dpi
        for suggestion in patch.layers:
            index = suggestion.layer_index
            related = [i for i in selected if index in i.source_indices and i.frame_id in frames]
            if index >= len(raw["layers"]) or not related:
                raise RepairUnavailable("Layer repair has no matching native frame/ROI issue.")
            layer = raw["layers"][index]
            if suggestion.style:
                updates = suggestion.style.model_dump(exclude_none=True)
                if updates.get("fill_opacity", 1) < 1:
                    pair = next(
                        (
                            _covering_pair(manifest, i, review)
                            for i in related
                            if _covering_pair(manifest, i, review)
                        ),
                        None,
                    )
                    if pair is None or pair[1] != index or updates["fill_opacity"] != 0:
                        raise RepairUnavailable(
                            "Hollow fill needs an actual covering single circle."
                        )
                    from PIL import ImageColor

                    stroke = updates.get("outline_color", layer.get("style", {}).get("color"))
                    try:
                        opaque = (
                            int(stroke[1:3], 16) == 255
                            if isinstance(stroke, str)
                            and stroke.startswith("#")
                            and len(stroke) == 9
                            else ImageColor.getcolor(stroke, "RGBA")[3] == 255
                        )
                    except (ValueError, TypeError):
                        opaque = False
                    width = updates.get("width_mm", layer.get("style", {}).get("width_mm", 0.3))
                    size = updates.get("size_mm", layer.get("style", {}).get("size_mm", 3))
                    lower_sizes = [
                        p.get("symbol", {}).get("size_mm", math.inf)
                        for i in related
                        for p in frames[i.frame_id].get("points", [])
                        if p.get("layer_index") == pair[0]
                    ]
                    if not opaque or width <= 0 or size - 2 * width < max(lower_sizes):
                        raise RepairUnavailable(
                            "Hollow ring must remain visible around the capacity symbol."
                        )
                    updates["outline_color"] = stroke
                if "size_mm" in updates and layer.get("style", {}).get("renderer") in {
                    "categorized",
                    "graduated",
                }:
                    current_size = layer.get("style", {}).get("size_mm", 3)
                    if updates["size_mm"] > current_size:
                        raise RepairUnavailable("Do not increase all classified point symbols.")
                if "width_mm" in updates:
                    spans = [
                        b.get("minimum_span_mm")
                        for i in related
                        for b in frames[i.frame_id].get("layer_bboxes", [])
                        if b.get("layer_index") == index
                    ]
                    spans = [s for s in spans if isinstance(s, (int, float)) and s > 0]
                    if not spans or 2 * updates["width_mm"] / min(spans) > getattr(
                        config.map_review, "max_border_fraction", 0.08
                    ):
                        raise RepairUnavailable("Stroke lacks safe measured paper-span evidence.")
                if updates.get("opacity", 1) < 1 and layer.get("style", {}).get("renderer") in {
                    "categorized",
                    "graduated",
                }:
                    raise RepairUnavailable("Repair must preserve classified point encoding.")
                layer.setdefault("style", {}).update(updates)
            if suggestion.label_style:
                layer.setdefault("label_style", {}).update(
                    suggestion.label_style.model_dump(exclude_none=True)
                )
    else:
        raise RepairUnavailable("Review contains no executable presentation proposal.")
    parameters = GeoQgisProjectInput.model_validate(raw)
    stable_parameters = parameters.model_dump()
    for layer in stable_parameters["layers"]:
        layer["file_path"] = "sha256:" + package["recorded"][Path(layer["file_path"]).resolve()]
        labels = layer.get("label_style") or {}
        if labels.get("font_file"):
            labels["font_file"] = "sha256:" + package["recorded"][Path(labels["font_file"])]
    stable_layout = stable_parameters.get("layout") or {}
    if stable_layout.get("font_file"):
        stable_layout["font_file"] = (
            "sha256:" + package["recorded"][Path(stable_layout["font_file"])]
        )
    material = {
        "image": package["image_sha256"],
        "parameters": stable_parameters,
        "source_bindings": sources,
        "issue_ids": identifiers,
        "strategy": mode,
    }
    digest = hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()
    return RepairPlan(parameters, sources, digest, identifiers, mode)
