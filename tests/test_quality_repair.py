"""Presentation repairs preserve hash-bound snapshots and original analysis provenance."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from geoharness.cartography.jobs import ensure_map_job, record_map_version
from geoharness.cartography.quality import MapReview
from geoharness.config.settings import GeoConfig


def package(tmp_path, metadata=None, *, name="initial", color="white", map_id="",
            project_content=b"saved layout"):
    metadata = metadata if metadata is not None else {}
    root = tmp_path / name
    root.mkdir()
    image = root / "preview.png"
    Image.new("RGB", (100, 100), color).save(image)
    (root / "data.gpkg").write_bytes(b"frozen subset")
    (root / "map.qgz").write_bytes(project_content)
    layers = [
        {
            "file_path": "data.gpkg",
            "name": "Capacity",
            "kind": "vector",
            "layer_name": "capacity",
            "style": {
                "renderer": "graduated",
                "field": "cap",
                "ranges": [
                    {"lower": 0, "upper": 30000, "label": "medium", "color": "blue"},
                    {"lower": 30000, "upper": 50000, "label": "high", "color": "navy"},
                ],
            },
        },
        {
            "file_path": "data.gpkg",
            "name": "Major",
            "kind": "vector",
            "layer_name": "major",
            "style": {"color": "navy", "size_mm": 5},
        },
    ]
    layout = {
        "width_mm": 100,
        "height_mm": 100,
        "margin_mm": 5,
        "sidebar_width_mm": 20,
        "gap_mm": 1,
        "footer_text": "source note",
    }
    bindings = [
        {
            "file_path": str(tmp_path / "original.geojson"),
            "derivation": {"feature_indices": [1, 3]},
            "temporal_fields": [],
        },
        {"file_path": str(tmp_path / "major.geojson"), "temporal_fields": []},
    ]
    manifest = {
        "backend": "qgis",
        "preview": image.name,
        "project_file": "map.qgz",
        "preview_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        "layout": {
            "bounds_check": {"checked": True, "valid": True, "problems": []},
            "regions": {"main_map": [10, 10, 80, 70]},
            "effective_parameters": {"width_mm": 100, "height_mm": 100},
        },
        "layers": [
            {
                "source_index": 0,
                "kind": "vector",
                "geometry_type": "Point",
                "renderer": "graduatedSymbol",
                "ranges": layers[0]["style"]["ranges"],
            },
            {
                "source_index": 1,
                "kind": "vector",
                "geometry_type": "Point",
                "renderer": "singleSymbol",
            },
        ],
        "creation_spec": {
            "parameters": {
                "layers": layers,
                "layout": layout,
                "crs": "EPSG:32647",
                "output_format": "pdf",
                "project_format": "qgz",
            },
            "source_bindings": bindings,
            "analysis_manifests": ["analysis.json"],
        },
        "native_facts": {
            "frames": {
                "main_map": {
                    "extent": [0, 0, 100, 100],
                    "rect_mm": [10, 10, 80, 70],
                    "source_indices": [1, 0],
                    "points": [
                        {
                            "layer_index": 0,
                            "feature_id": 1,
                            "x": 50,
                            "y": 50,
                            "symbol": {
                                "size_mm": 3,
                                "width_mm": 0.3,
                                "color_alpha": 1,
                                "outline_alpha": 1,
                                "marker_shape": "circle",
                                "symbol_layer_count": 1,
                                "symbol_layer_type": "SimpleMarker",
                                "layer_opacity": 1,
                                "symbol_opacity": 1,
                                "data_defined": False,
                            },
                        },
                        {
                            "layer_index": 1,
                            "feature_id": 3,
                            "x": 50,
                            "y": 50,
                            "symbol": {
                                "size_mm": 5,
                                "width_mm": 0.3,
                                "color_alpha": 1,
                                "outline_alpha": 1,
                                "marker_shape": "circle",
                                "symbol_layer_count": 1,
                                "symbol_layer_type": "SimpleMarker",
                                "layer_opacity": 1,
                                "symbol_opacity": 1,
                                "data_defined": False,
                            },
                        },
                    ],
                }
            },
            "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        },
        "files": ["data.gpkg", "map.qgz", "preview.png", "project.json"],
    }
    (root / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    config = GeoConfig()
    identity = ensure_map_job(
        metadata, cwd=tmp_path, config=config, artifact_path=image, map_id=map_id
    )
    receipt = record_map_version(
        identity,
        metadata,
        cwd=tmp_path,
        config=config,
        image_path=image,
        preflight=None,
        layout=manifest["layout"],
        project_dir=root,
    )
    return image, receipt, metadata, manifest


def overlapping_review():
    return MapReview.model_validate(
        {
            "status": "issues",
            "summary": "Capacity covered",
            "issues": [
                {
                    "issue_id": "capacity-covered",
                    "severity": "warning",
                    "description": "Solid major marker covers the capacity color",
                    "issue_code": "symbol_overlap",
                    "frame_id": "main_map",
                    "roi_ref": "main_map",
                    "bbox": [0.2, 0.2, 0.8, 0.8],
                    "source_indices": [0, 1],
                    "evidence_kind": "semantic",
                }
            ],
            "qgis_patch": {
                "issue_ids": ["capacity-covered"],
                "layers": [
                    {"layer_index": 1, "style": {"fill_opacity": 0, "outline_color": "navy"}}
                ],
            },
            "diagnostics": {
                "review_rois": [
                    {
                        "roi_ref": "main_map",
                        "frame_id": "main_map",
                        "pixel_bbox": [10, 10, 90, 80],
                        "region_bbox": [10, 10, 90, 80],
                        "original_size": [100, 100],
                        "resampled": False,
                    }
                ]
            },
        }
    )


def test_hollow_plan_preserves_snapshot_analysis_and_raw_notes(tmp_path):
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, _, manifest = package(tmp_path)
    plan = build_repair_plan(image, overlapping_review(), receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.parameters.map_id == receipt["job_id"]
    assert plan.parameters.layers[1].style.fill_opacity == 0
    assert plan.parameters.layers[0].style.field == "cap"
    assert len(plan.parameters.layers[0].style.ranges) == 2
    assert plan.parameters.layers[0].derivation is None
    assert plan.parameters.layers[0].file_path == str((image.parent / "data.gpkg").resolve())
    assert plan.source_bindings == manifest["creation_spec"]["source_bindings"]
    assert plan.parameters.analysis_manifests == ["analysis.json"]
    assert plan.parameters.layout.footer_text == "source note"
    assert plan.parameters.output_format == "pdf"


def test_info_reference_does_not_discard_an_independent_valid_patch(tmp_path):
    from geoharness.cartography.quality import MapIssue
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, _, _ = package(tmp_path)
    review = overlapping_review()
    review.issues.append(MapIssue(issue_id="note", severity="info", description="Optional"))
    review.qgis_patch.issue_ids.append("note")
    plan = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.parameters.layers[1].style.fill_opacity == 0
    assert plan.issue_ids == ["capacity-covered"]


def test_bad_footer_binding_preserves_independent_layer_fixes_but_not_page_changes(tmp_path):
    from geoharness.cartography.quality import MapIssue, QgisPresentationPatch
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, _, _ = package(tmp_path)
    review = overlapping_review()
    first = review.issues[0]
    first.source_indices = [1]
    second = first.model_copy(update={"issue_id": "I4", "source_indices": [0]})
    review.issues.extend([second, MapIssue(issue_id="I5", severity="warning",
        description="Footer density", frame_id="whole", roi_ref="footer", bbox=[0, 0, 1, 1])])
    review.diagnostics["review_rois"].append({
        "roi_ref": "footer", "frame_id": "footer", "pixel_bbox": [0, 90, 100, 100],
        "region_bbox": [0, 90, 100, 100], "original_size": [100, 100], "resampled": False})
    review.qgis_patch = QgisPresentationPatch.model_validate({
        "issue_ids": [first.issue_id, "I4", "I5"], "layout": {"footer_height_mm": 30},
        "layers": [{"layer_index": 1, "label_style": {"size_pt": 9}},
                   {"layer_index": 0, "label_style": {"priority": 8}}]})
    plan = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.parameters.layers[1].label_style.size_pt == 9
    assert plan.parameters.layers[0].label_style.priority == 8
    assert plan.parameters.layout.footer_height_mm != 30
    assert plan.issue_ids == [first.issue_id, "I4"]
    assert any(item.get("issue_id") == "I5" for item in plan.rejected)


def test_coupled_layer_and_page_changes_rollback_together(tmp_path):
    from geoharness.cartography.quality import QgisPresentationPatch
    from geoharness.cartography.quality_repair import RepairUnavailable, build_repair_plan

    image, receipt, _, _ = package(tmp_path)
    review = overlapping_review()
    review.qgis_patch = QgisPresentationPatch.model_validate({
        "issue_ids": ["capacity-covered"], "layout": {"width_mm": 120},
        "layers": [{"layer_index": 1, "style": {"size_mm": 4}},
                   {"layer_index": 0, "style": {"size_mm": 10}}]})
    with pytest.raises(RepairUnavailable, match="classified"):
        build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)


@pytest.mark.parametrize("key,value", [("image_sha256", "old"), ("version", 99),
                                        ("source_bindings_sha256", "different")])
def test_repair_cannot_retarget_a_review_to_another_map_or_source(tmp_path, key, value):
    from geoharness.cartography.quality_repair import RepairUnavailable, build_repair_plan

    image, receipt, _, _ = package(tmp_path)
    review = overlapping_review()
    review.diagnostics["review_binding"] = {key: value}
    with pytest.raises(RepairUnavailable, match="different map version or source"):
        build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)


@pytest.mark.parametrize("hard", [False, True, "legacy"])
def test_review_zoom_is_advisory_and_keeps_explicit_user_hard_requirement(tmp_path, hard):
    from geoharness.cartography.quality import QgisPresentationPatch
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, metadata, manifest = package(tmp_path)
    layout = manifest["creation_spec"]["parameters"]["layout"]
    layout.update(detail_extent={"xmin": 40, "ymin": 40, "xmax": 60, "ymax": 60})
    if hard:
        layout["detail_minimum_zoom"] = 2
        if hard is True:
            layout["detail_zoom_policy"] = "hard"
    (image.parent / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt = record_map_version(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=image, preflight=None, layout=manifest["layout"], project_dir=image.parent)
    review = overlapping_review()
    review.qgis_patch = QgisPresentationPatch.model_validate({"issue_ids": ["capacity-covered"],
        "layout": {"detail_minimum_zoom": 10, "width_mm": 120}})
    plan = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.parameters.layout.detail_minimum_zoom == (2 if hard else 10)
    assert plan.parameters.layout.detail_zoom_policy == ("hard" if hard else "advisory")


async def test_registered_detail_page_uses_its_pixels_and_does_not_create_a_map_version(tmp_path):
    import copy

    from openharness.tools.base import ToolExecutionContext
    from test_map_review_evidence import Reviewer

    from geoharness.tools.geo_map_review import (
        GeoMapReviewInput,
        GeoMapReviewTool,
        _verified_render_evidence,
    )

    image, receipt, metadata, manifest = package(tmp_path)
    detail = image.parent / "detail_page_2.png"
    Image.new("RGB", (100, 100), "blue").save(detail)
    frame = copy.deepcopy(manifest["native_facts"]["frames"]["main_map"])
    frame.update(page_index=1, rect_mm=[10, 210, 80, 70], rect_page_mm=[10, 10, 80, 70])
    manifest["native_facts"]["frames"]["detail_map"] = frame
    manifest["native_facts"]["page_previews"] = [
        {"page_index": 0, "path": str(image), "frames": ["main_map"]},
        {"page_index": 1, "path": str(detail), "frames": ["detail_map"]}]
    manifest["files"].append(detail.name)
    (image.parent / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt = record_map_version(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=image, preflight=None, layout=manifest["layout"], project_dir=image.parent)
    reviewer = Reviewer()
    metadata.update(geoharness_config=GeoConfig(), map_review_client=reviewer)
    context = ToolExecutionContext(cwd=tmp_path, metadata=metadata)
    first = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(image), map_id=receipt["job_id"], task="Check actual detail"), context)
    assert first.metadata["review_status"] == "unverified"
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(detail), map_id=receipt["job_id"], task="Check actual detail"),
        context)
    assert result.metadata["review_page_index"] == 1
    assert len(result.metadata["map_receipt"]["versions"]) == 1
    assert result.metadata["map_receipt"]["stages"]["visual_checked"]["status"] == "passed"
    assert result.metadata["map_receipt"]["stages"]["visual_checked"]["scope"] == (
        "all_native_map_pages")
    report = json.loads(Path(result.metadata["review_path"]).read_text(encoding="utf-8"))
    diagnostics = report["diagnostics"]
    assert diagnostics["page_visual_status"] == "passed"
    assert diagnostics["review_binding"]["page_index"] == 1
    assert diagnostics["review_binding"]["primary_image_sha256"] == manifest["preview_sha256"]
    assert [roi["roi_ref"] for roi in diagnostics["review_rois"]] == ["whole", "detail_map"]
    assert diagnostics["review_rois"][1]["pixel_bbox"] == [10, 10, 90, 80]
    Image.new("RGB", (100, 100), "red").save(detail)
    assert _verified_render_evidence(detail, hashlib.sha256(detail.read_bytes()).hexdigest(),
        receipt, [tmp_path])["backend"] == "unknown"


@pytest.mark.parametrize("status", ["passed", "issues"])
async def test_only_clean_cached_pixels_can_rebind_to_a_new_native_package(
    tmp_path, monkeypatch, status,
):
    from openharness.tools.base import ToolExecutionContext

    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    first, receipt, metadata, _ = package(tmp_path)
    image, _, _, _ = package(tmp_path, metadata, name="new-package", map_id=receipt["job_id"],
                              project_content=b"changed saved project with equal preview pixels")
    assert first.read_bytes() == image.read_bytes()
    review = (MapReview(status="passed", summary="Readable") if status == "passed"
              else overlapping_review())
    review.diagnostics["review_binding"] = {"version": 1, "manifest_sha256": "old-package"}
    async def cached(*args, **kwargs):
        return review, {"review_cached": True, "review_budget_exhausted": False,
                       "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                       "visual_review_status": status}
    monkeypatch.setattr("geoharness.tools.geo_map_review.review_with_budget", cached)
    metadata["geoharness_config"] = GeoConfig()
    result = await GeoMapReviewTool()._review_one(GeoMapReviewInput(
        file_path=str(image), map_id=receipt["job_id"], task="Check"),
        ToolExecutionContext(cwd=tmp_path, metadata=metadata))
    report = json.loads(Path(result.metadata["review_path"]).read_text(encoding="utf-8"))
    if status == "passed":
        assert report["diagnostics"]["review_binding"]["version"] == 2
        assert report["diagnostics"]["pixel_pass_rebound_to_current_package"] is True
    else:
        assert report["diagnostics"]["review_binding"]["version"] == 1
        assert "qgis_patch" not in report
        assert report["diagnostics"]["repair_binding_status"] == "stale_cached_package"


def test_strategy_uses_its_own_issues_instead_of_first_whitespace_patch(tmp_path):
    from geoharness.cartography.quality import MapIssue, ReviewStrategy
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, _, _ = package(tmp_path)
    review = overlapping_review()
    review.issues.insert(0, MapIssue(issue_id="whitespace", severity="warning",
        description="Layout whitespace", issue_code="extent_whitespace", frame_id="main_map",
        roi_ref="main_map", bbox=[0, 0, 1, 1], source_indices=[0]))
    review.qgis_patch.issue_ids = ["whitespace", "capacity-covered"]
    review.next_strategy = ReviewStrategy(strategy="detail_map", reason="Show local overlap",
                                          issue_ids=["capacity-covered"])
    plan = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.strategy == "detail_map"
    assert plan.parameters.layout.detail_extent.xmax < 90


def test_repeated_conflict_uses_stable_keys_and_overlapping_roi_after_reorder():
    from geoharness.cartography.quality import MapIssue
    from geoharness.cartography.quality_repair import _repeat

    issue = MapIssue(issue_id="new", severity="warning", description="Labels still overlap",
        frame_id="main_map", roi_ref="new_crop", bbox=[0.21, 0.2, 0.81, 0.8],
        source_indices=[1], issue_code="label_overlap")
    receipt = {"versions": [{"image_sha256": "old", "stages": {
        "data_checked": {"report": {"frame_checks": {"layers": [
            {"source_index": 0, "layer_key": "sites"}]}}},
        "visual_checked": {"status": "issues", "report": {"issues": [
            {"issue_id": "old", "frame_id": "main_map", "roi_ref": "old_crop",
             "bbox": [0.2, 0.2, 0.8, 0.8], "source_indices": [0],
             "issue_code": "label_overlap"}]}}}}]}
    assert _repeat(issue, receipt, "new", ["sites"])
    assert not _repeat(issue, receipt, "new", ["event"])
    issue.bbox = [0, 0, 0.1, 0.1]
    assert not _repeat(issue, receipt, "new", ["sites"])


def test_native_numbering_is_a_source_bound_derivation_and_cannot_overwrite():
    import geopandas as gpd
    from shapely.geometry import Point

    from geoharness.cartography.derivation import derive_frame

    source = gpd.GeoDataFrame({"name": ["A", "B", "C"]},
                              geometry=[Point(i, i) for i in range(3)], crs=3857)
    numbered = derive_frame(source, {"feature_indices": [2, 0], "number_field": "number"})
    assert numbered["number"].tolist() == [1, 2]
    assert numbered["name"].tolist() == ["C", "A"]
    assert "number" not in source.columns
    with pytest.raises(ValueError, match="overwrite"):
        derive_frame(source, {"number_field": "name"})


def test_copying_an_older_selected_package_does_not_create_a_render_version(tmp_path):
    from geoharness.cartography.jobs import map_job_receipt, record_map_selection

    image, receipt, metadata, _ = package(tmp_path)
    _, _, _, _ = package(tmp_path, metadata, name="later", color="red", map_id=receipt["job_id"])
    copied = tmp_path / "selected"
    shutil.copytree(image.parent, copied)
    selected = record_map_selection(
        receipt["job_id"],
        metadata,
        cwd=tmp_path,
        config=GeoConfig(),
        image_path=copied / "preview.png",
        project_dir=copied,
    )
    assert len(selected["versions"]) == 2
    assert selected["selected_version"] == 1
    assert selected["latest_version"] == 2
    recovered = map_job_receipt(
        {}, cwd=tmp_path, config=GeoConfig(), artifact_path=copied / "preview.png"
    )
    assert recovered["job_id"] == receipt["job_id"]
    (copied / "data.gpkg").write_bytes(b"changed")
    with pytest.raises(ValueError):
        record_map_selection(
            receipt["job_id"],
            metadata,
            cwd=tmp_path,
            config=GeoConfig(),
            image_path=copied / "preview.png",
            project_dir=copied,
        )


def test_split_plan_separates_real_coincident_point_layers_and_preserves_union(tmp_path):
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, _, _ = package(tmp_path)
    review = overlapping_review()
    from geoharness.cartography.quality import ReviewStrategy

    review.next_strategy = ReviewStrategy(
        strategy="split_maps", reason="Separate identity", issue_ids=["capacity-covered"]
    )
    plan = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.strategy == "split_maps"
    assert plan.parameters.layout.main_frame.render_layers == [0]
    assert plan.parameters.layout.detail_frame.render_layers == [1]
    assert plan.parameters.layout.detail_extent is None
    assert plan.parameters.layout.detail_frame.focus_layers == [1]
    assert len(plan.parameters.layers) == 2


@pytest.mark.parametrize("change", ["snapshot", "missing_roi", "wrong_order", "stroke"])
def test_unproven_or_regressive_repairs_are_rejected(tmp_path, change):
    from geoharness.cartography.quality import QgisSymbolPatch
    from geoharness.cartography.quality_repair import RepairUnavailable, build_repair_plan

    image, receipt, metadata, manifest = package(tmp_path)
    review = overlapping_review()
    if change == "snapshot":
        (image.parent / "data.gpkg").write_bytes(b"unreviewed replacement")
    elif change == "missing_roi":
        review.issues[0].roi_ref = "not-sent"
    elif change == "stroke":
        review.qgis_patch.layers[0].style = QgisSymbolPatch(width_mm=2.8)
    else:
        manifest["native_facts"]["frames"]["main_map"]["source_indices"] = [0, 1]
        (image.parent / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
        receipt = record_map_version(
            receipt["job_id"],
            metadata,
            cwd=tmp_path,
            config=GeoConfig(),
            image_path=image,
            preflight=None,
            layout=manifest["layout"],
            project_dir=image.parent,
        )
    with pytest.raises(RepairUnavailable):
        build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)


def test_packaged_label_font_is_hash_checked_and_resolved_for_rebuild(tmp_path):
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, metadata, manifest = package(tmp_path)
    font = image.parent / "assets" / "fonts" / "label.ttf"
    font.parent.mkdir(parents=True)
    font.write_bytes(b"registered font resource")
    manifest["files"].append("assets/fonts/label.ttf")
    manifest["creation_spec"]["parameters"]["layers"][0]["label_style"] = {
        "font_file": "assets/fonts/label.ttf"}
    (image.parent / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt = record_map_version(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=image, preflight=None, layout=manifest["layout"], project_dir=image.parent)
    plan = build_repair_plan(image, overlapping_review(), receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.parameters.layers[0].label_style.font_file == str(font.resolve())


def test_same_pixel_versions_keep_the_selected_package_and_report_identity(tmp_path):
    from geoharness.cartography.jobs import map_job_receipt, record_map_review, record_map_selection
    from geoharness.cartography.quality_repair import verified_package

    image, receipt, metadata, _ = package(tmp_path, project_content=b"package A")
    record_map_review(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=image, review={"status": "passed", "summary": "Checked A",
                                 "image_sha256": receipt["image_sha256"]})
    package(tmp_path, metadata, name="B", color="red", map_id=receipt["job_id"])
    later, latest, _, _ = package(tmp_path, metadata, name="C", map_id=receipt["job_id"],
                                  project_content=b"package C")
    assert latest["delivery_candidate"]["version"] == 1
    selected = map_job_receipt(metadata, cwd=tmp_path, config=GeoConfig(), artifact_path=image)
    assert selected["selected_version"] == 1
    assert selected["stages"]["visual_checked"]["status"] == "passed"
    bound = verified_package(image, selected, cwd=tmp_path, config=GeoConfig())
    assert bound["root"] == image.parent
    copied = tmp_path / "copyA"
    shutil.copytree(image.parent, copied)
    moved = record_map_selection(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=copied / "preview.png", project_dir=copied, selected_version=1)
    assert moved["selected_version"] == 1 and len(moved["versions"]) == 3
    assert map_job_receipt(metadata, cwd=tmp_path, config=GeoConfig(),
                          artifact_path=later)["selected_version"] == 3


def test_detail_uses_only_native_features_inside_roi_without_trimming_main(tmp_path):
    from geoharness.cartography.quality import ReviewStrategy
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, metadata, manifest = package(tmp_path)
    manifest["native_facts"]["frames"]["main_map"]["points"].append(
        {"layer_index": 1, "feature_id": 99, "x": 95, "y": 95, "symbol": {}})
    manifest["creation_spec"]["parameters"]["layout"]["detail_frame"] = {
        "purpose": "Local capacity subjects", "requirements": [{"layer_index": 0,
            "coverage": "complete", "key_field": "cap", "feature_keys": [40000]}]}
    (image.parent / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt = record_map_version(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=image, preflight=None, layout=manifest["layout"], project_dir=image.parent)
    review = overlapping_review()
    review.next_strategy = ReviewStrategy(
        strategy="detail_map", reason="Show local points", issue_ids=["capacity-covered"])
    plan = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.parameters.layout.main_frame.render_layers == [0, 1]
    assert plan.parameters.map_extent is None
    detail = plan.parameters.layout.detail_extent
    assert detail.xmin < 50 < detail.xmax < 95
    assert detail.ymin < 50 < detail.ymax < 95
    assert plan.parameters.layout.detail_frame.focus_layers is None
    assert plan.parameters.layout.detail_frame.render_layers == [0, 1]
    assert plan.parameters.layout.detail_frame.purpose == "Local capacity subjects"
    assert plan.parameters.layout.detail_frame.requirements[0].feature_keys == [40000]
    assert plan.parameters.layout.detail_frame.requirements[0].coverage == "complete"


def test_equal_pixel_copy_requires_explicit_version_and_actual_same_package(tmp_path):
    from geoharness.cartography.jobs import map_job_receipt, record_map_selection
    from geoharness.cartography.quality_repair import RepairUnavailable, verified_package

    image, receipt, metadata, _ = package(tmp_path, project_content=b"project A")
    package(tmp_path, metadata, name="B", color="red", map_id=receipt["job_id"])
    package(tmp_path, metadata, name="C", map_id=receipt["job_id"], project_content=b"project C")
    copied = tmp_path / "ambiguous"
    shutil.copytree(image.parent, copied)
    unknown = map_job_receipt(metadata, cwd=tmp_path, config=GeoConfig(),
                             artifact_path=copied / "preview.png")
    assert not unknown["artifact_registered"]
    assert unknown["quality_status"] == "draft"
    with pytest.raises(RepairUnavailable):
        verified_package(copied / "preview.png", unknown, cwd=tmp_path, config=GeoConfig())
    with pytest.raises(ValueError):
        record_map_selection(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
            image_path=copied / "preview.png", project_dir=copied)
    with pytest.raises(ValueError):
        record_map_selection(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
            image_path=copied / "preview.png", project_dir=copied, selected_version=3)


def test_identical_package_relocation_keeps_repair_replay_identity(tmp_path):
    from geoharness.cartography.jobs import record_map_selection
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, metadata, _ = package(tmp_path)
    original = build_repair_plan(image, overlapping_review(), receipt,
                                 config=GeoConfig(), cwd=tmp_path)
    relocated = tmp_path / "relocated"
    shutil.copytree(image.parent, relocated)
    selected = record_map_selection(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=relocated / "preview.png", project_dir=relocated, selected_version=1)
    same = build_repair_plan(relocated / "preview.png", overlapping_review(), selected,
                            config=GeoConfig(), cwd=tmp_path)
    assert same.digest == original.digest


@pytest.mark.parametrize("change", ["alpha", "complex", "triangle", "data_defined"])
def test_hollow_repair_requires_actual_opaque_single_circle_symbol(tmp_path, change):
    from geoharness.cartography.quality_repair import RepairUnavailable, build_repair_plan

    image, receipt, metadata, manifest = package(tmp_path)
    symbol = manifest["native_facts"]["frames"]["main_map"]["points"][1]["symbol"]
    if change == "alpha":
        symbol["color_alpha"] = 0.5
    elif change == "complex":
        symbol["symbol_layer_count"] = 2
    elif change == "triangle":
        symbol["marker_shape"] = "triangle"
    else:
        symbol["data_defined"] = True
    (image.parent / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt = record_map_version(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=image, preflight=None, layout=manifest["layout"], project_dir=image.parent)
    with pytest.raises(RepairUnavailable):
        build_repair_plan(image, overlapping_review(), receipt, config=GeoConfig(), cwd=tmp_path)


def test_nullable_creation_containers_remain_legal_for_automatic_rebuild(tmp_path):
    from geoharness.cartography.quality import QgisLayoutPatch, QgisPresentationPatch
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, metadata, manifest = package(tmp_path)
    raw = manifest["creation_spec"]["parameters"]
    raw["layout"] = None
    for layer in raw["layers"]:
        layer.update(style=None, label_style=None)
    (image.parent / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt = record_map_version(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=image, preflight=None, layout=manifest["layout"], project_dir=image.parent)
    review = overlapping_review()
    review.qgis_patch = QgisPresentationPatch(issue_ids=["capacity-covered"],
        layout=QgisLayoutPatch(legend_font_size_pt=12))
    plan = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.parameters.layout.legend_font_size_pt == 12
    assert plan.parameters.layers[0].style.renderer == "single"
    assert plan.parameters.layers[0].label_style is not None


@pytest.mark.parametrize("container", ["layout", "style", "label_style"])
def test_invalid_creation_containers_fail_with_repair_unavailable(tmp_path, container):
    from geoharness.cartography.quality_repair import RepairUnavailable, build_repair_plan

    image, receipt, metadata, manifest = package(tmp_path)
    raw = manifest["creation_spec"]["parameters"]
    target = raw if container == "layout" else raw["layers"][0]
    target[container] = "malformed object"
    (image.parent / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt = record_map_version(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=image, preflight=None, layout=manifest["layout"], project_dir=image.parent)
    with pytest.raises(RepairUnavailable):
        build_repair_plan(image, overlapping_review(), receipt, config=GeoConfig(), cwd=tmp_path)


@pytest.mark.parametrize("strategy", ["detail_map", "split_maps"])
@pytest.mark.parametrize("frame", [None, {"render_layers": None}])
def test_detail_strategies_accept_nullable_main_and_detail_frames(tmp_path, strategy, frame):
    from geoharness.cartography.quality import ReviewStrategy
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, metadata, manifest = package(tmp_path)
    manifest["creation_spec"]["parameters"]["layout"].update(
        main_frame=frame, detail_frame=None)
    (image.parent / "project.json").write_text(json.dumps(manifest), encoding="utf-8")
    receipt = record_map_version(receipt["job_id"], metadata, cwd=tmp_path, config=GeoConfig(),
        image_path=image, preflight=None, layout=manifest["layout"], project_dir=image.parent)
    review = overlapping_review()
    review.next_strategy = ReviewStrategy(strategy=strategy, reason="Separate subject",
                                          issue_ids=["capacity-covered"])
    plan = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.parameters.layout.main_frame.render_layers == ([0] if strategy == "split_maps"
                                                              else [0, 1])
    assert plan.parameters.layout.detail_frame is not None


@pytest.mark.parametrize("frame_id", ["footer", "legend"])
def test_repeated_non_map_issue_keeps_page_patch(tmp_path, monkeypatch, frame_id):
    from geoharness.cartography.quality import MapIssue, QgisLayoutPatch, QgisPresentationPatch
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, _, _ = package(tmp_path)
    review = overlapping_review()
    review.issues = [MapIssue(issue_id="page", severity="warning", issue_code="footer_density",
        description="Crowded page region", frame_id=frame_id, roi_ref=frame_id,
        bbox=[0, 0, 1, 1])]
    review.diagnostics["review_rois"] = [{"roi_ref": frame_id, "frame_id": frame_id,
        "pixel_bbox": [0, 90, 100, 100], "region_bbox": [0, 90, 100, 100],
        "original_size": [100, 100], "resampled": False}]
    review.qgis_patch = QgisPresentationPatch(issue_ids=["page"],
        layout=QgisLayoutPatch(footer_height_mm=30))
    monkeypatch.setattr("geoharness.cartography.quality_repair._repeat", lambda *args: True)
    plan = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.strategy == "adjust_presentation"
    assert plan.parameters.layout.footer_height_mm == 30


def test_repeated_map_issue_does_not_georeference_footer_and_keeps_its_patch(tmp_path, monkeypatch):
    from geoharness.cartography.quality import MapIssue, QgisLayoutPatch
    from geoharness.cartography.quality_repair import build_repair_plan

    image, receipt, _, _ = package(tmp_path)
    review = overlapping_review()
    review.issues.append(MapIssue(
        issue_id="footer", severity="warning", issue_code="footer_density",
        description="Crowded footer", frame_id="footer", roi_ref="footer", bbox=[0, 0, 1, 1]))
    review.diagnostics["review_rois"].append({"roi_ref": "footer", "frame_id": "footer",
        "pixel_bbox": [0, 90, 100, 100], "region_bbox": [0, 90, 100, 100],
        "original_size": [100, 100], "resampled": False})
    review.qgis_patch.issue_ids.append("footer")
    review.qgis_patch.layout = QgisLayoutPatch(footer_height_mm=30)
    monkeypatch.setattr("geoharness.cartography.quality_repair._repeat", lambda *args: True)
    plan = build_repair_plan(image, review, receipt, config=GeoConfig(), cwd=tmp_path)
    assert plan.strategy == "split_maps"
    assert plan.parameters.layout.footer_height_mm == 30
    assert "footer" in plan.issue_ids


@pytest.mark.parametrize("frame_id,extent", [
    ("footer", [0, 0, 100, 100]), ("main_map", None),
    ("main_map", [0, 0, 0, 100]), ("main_map", [0, 0, float("inf"), 100]),
    ("main_map", [0, 0, "100", 100]), ("main_map", [0, 0, 100]),
])
def test_issue_extent_rejects_non_map_or_invalid_native_frame(frame_id, extent):
    from geoharness.cartography.quality_repair import RepairUnavailable, _issue_extent

    review = overlapping_review()
    issue = review.issues[0].model_copy(update={"frame_id": frame_id})
    with pytest.raises(RepairUnavailable, match="native map frame"):
        _issue_extent(issue, review, {"extent": extent})


@pytest.mark.parametrize("legacy_auto_repair", [True, False])
async def test_public_image_review_preserves_package_and_never_plans_or_rebuilds(
    tmp_path, monkeypatch, legacy_auto_repair,
):
    from openharness.tools.base import ToolExecutionContext
    from test_map_review_evidence import Reviewer

    from geoharness.config.settings import MapReviewConfig
    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    image, receipt, metadata, _ = package(tmp_path)
    reviewer = Reviewer({"status": "issues", "summary": "Symbols overlap.", "issues": [{
        "severity": "warning", "description": "Two symbols cover one another.",
        "issue_code": "symbol_overlap", "frame_id": "main_map"}]})
    metadata.update(geoharness_config=GeoConfig(map_review=MapReviewConfig(
        auto_repair=legacy_auto_repair)), map_review_client=reviewer)
    before = {path: path.read_bytes() for path in image.parent.iterdir() if path.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("Image-only review must not plan a fix or render another version")

    monkeypatch.setattr("geoharness.cartography.quality_repair.build_repair_plan", forbidden)
    monkeypatch.setattr("geoharness.tools.geo_qgis_project.GeoQgisProjectTool.execute", forbidden)
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(image), task="Inspect visible problems"),
        ToolExecutionContext(cwd=tmp_path, metadata=metadata))
    assert not result.is_error
    assert result.metadata["review_status"] == "issues"
    assert result.metadata["render_executed"] is False
    assert len(reviewer.requests) == 1
    assert len(result.metadata["map_receipt"]["versions"]) == 1
    assert result.metadata["map_receipt"]["job_id"] == receipt["job_id"]
    assert "selected_candidate" not in result.metadata
    assert "final_render" not in result.metadata
    assert all(path.read_bytes() == content for path, content in before.items())


async def test_public_image_review_propagates_cancellation(tmp_path):
    import asyncio

    from openharness.tools.base import ToolExecutionContext

    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    image, _, metadata, _ = package(tmp_path)

    class CancelledReviewer:
        async def stream_message(self, request):
            raise asyncio.CancelledError
            yield  # Async generator transport interface.

    metadata.update(geoharness_config=GeoConfig(), map_review_client=CancelledReviewer())
    with pytest.raises(asyncio.CancelledError):
        await GeoMapReviewTool().execute(GeoMapReviewInput(
            file_path=str(image), task="Inspect"),
            ToolExecutionContext(cwd=tmp_path, metadata=metadata))
