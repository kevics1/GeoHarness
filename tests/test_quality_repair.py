"""Presentation repairs preserve hash-bound snapshots and original analysis provenance."""

import hashlib
import json
import shutil

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


async def test_automatic_review_creates_once_in_same_job_and_reviews_new_png(tmp_path, monkeypatch):
    from openharness.config.settings import PermissionSettings
    from openharness.permissions.checker import PermissionChecker
    from openharness.tools.base import ToolExecutionContext, ToolResult
    from test_map_review_evidence import Reviewer

    from geoharness.cartography.workflow import reserve_map_render
    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool
    from geoharness.tools.geo_qgis_project import GeoQgisProjectTool

    image, receipt, metadata, _ = package(tmp_path)
    response = overlapping_review().model_dump(exclude={"diagnostics"})
    reviewer = Reviewer(response)
    metadata.update(
        geoharness_config=GeoConfig(),
        map_review_client=reviewer,
        geoharness_permission_checker=PermissionChecker(
            PermissionSettings(allowed_tools=["geo_qgis_project"])
        ),
    )
    reserve_map_render(receipt["job_id"], metadata, GeoConfig())
    calls = []

    async def create(self, arguments, context, *, _source_bindings=None):
        calls.append((arguments, _source_bindings))
        assert reserve_map_render(arguments.map_id, context.metadata, GeoConfig()) is None
        new_image, _, _, _ = package(
            tmp_path, metadata, name="repaired", color="blue", map_id=arguments.map_id
        )
        reviewer.response = {"status": "passed", "summary": "Capacity visible", "issues": []}
        return ToolResult(
            output="Created",
            metadata={"preview_path": str(new_image), "project_dir": str(new_image.parent)},
        )

    monkeypatch.setattr(GeoQgisProjectTool, "execute", create)
    context = ToolExecutionContext(cwd=tmp_path, metadata=metadata)
    result = await GeoMapReviewTool().execute(
        GeoMapReviewInput(file_path=str(image), task="Preserve capacity"), context
    )
    assert len(calls) == 1
    assert calls[0][0].map_id == receipt["job_id"]
    assert calls[0][0].layers[1].style.fill_opacity == 0
    assert len(reviewer.requests) == 2
    assert result.metadata["repair_count"] == 1
    assert result.metadata["latest_image"] == str(tmp_path / "repaired" / "preview.png")
    assert len(result.metadata["map_receipt"]["versions"]) == 2
    assert result.metadata["latest_review_status"] == "passed"


@pytest.mark.parametrize("outcome", ["passed_after_eight", "create_failed", "pixel_cycle"])
async def test_unlimited_repairs_stop_on_outcome_and_expose_create_failure(
    tmp_path, monkeypatch, outcome,
):
    from types import SimpleNamespace

    from openharness.config.settings import PermissionSettings
    from openharness.permissions.checker import PermissionChecker
    from openharness.tools.base import ToolExecutionContext, ToolResult
    from test_map_review_evidence import Reviewer

    from geoharness.config.settings import MapReviewConfig
    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool
    from geoharness.tools.geo_qgis_project import GeoQgisProjectTool

    image, receipt, metadata, _ = package(tmp_path)
    reviewer = Reviewer(overlapping_review().model_dump(exclude={"diagnostics"}))
    metadata.update(geoharness_config=GeoConfig(map_review=MapReviewConfig(
        max_repairs=0, max_reviews_per_request=1, total_timeout_seconds=0.001)),
        map_review_client=reviewer,
        geoharness_permission_checker=PermissionChecker(
            PermissionSettings(allowed_tools=["geo_qgis_project"])))
    calls = []

    def plan(*args, **kwargs):
        return SimpleNamespace(digest=f"plan-{len(calls)}", source_bindings=[],
                               parameters=SimpleNamespace(map_id=receipt["job_id"]))

    async def create(self, arguments, context, **kwargs):
        calls.append(arguments)
        if outcome == "create_failed":
            return ToolResult(output="Error: data fingerprint mismatch for layer 4",
                              is_error=True, metadata={"failure_stage": "preflight"})
        color = "white" if outcome == "pixel_cycle" and len(calls) == 2 else (
            10 * len(calls), 20, 40)
        rendered, _, _, _ = package(tmp_path, metadata, name=f"repair-{len(calls)}",
                                    color=color, map_id=arguments.map_id)
        if outcome == "passed_after_eight" and len(calls) == 8:
            reviewer.response = {"status": "passed", "summary": "Readable", "issues": []}
        return ToolResult(output="Created", metadata={"preview_path": str(rendered),
                          "project_dir": str(rendered.parent)})

    monkeypatch.setattr("geoharness.cartography.quality_repair.build_repair_plan", plan)
    monkeypatch.setattr(GeoQgisProjectTool, "execute", create)
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(image), task="Preserve capacity"),
        ToolExecutionContext(cwd=tmp_path, metadata=metadata))
    if outcome == "create_failed":
        assert len(calls) == 1
        assert "data fingerprint mismatch" in result.output
        assert result.metadata["repair_failure"]["metadata"]["failure_stage"] == "preflight"
        assert result.metadata["repair_failure"]["output"].endswith("layer 4")
    elif outcome == "pixel_cycle":
        assert len(calls) == 2
        assert len(reviewer.requests) == 2
        assert "previously reviewed pixels" in result.metadata["repair_stop_reason"]
    else:
        assert len(calls) == result.metadata["repair_count"] == 8
        assert len(reviewer.requests) == 9
        assert result.metadata["latest_review_status"] == "passed"


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
