from geoharness.cartography.scene import Page, Scene
from geoharness.cartography.scene_layout import compile_layout


def test_portrait_square_extent_uses_full_width_without_model_extent_loop():
    scene = Scene(
        crs="EPSG:32647",
        width_mm=297,
        height_mm=420,
        layers=[{"id": "data", "source_ref": "actual"}],
    )
    page = Page(id="overview")
    result = compile_layout(scene, page, [0, 0, 100, 100], ["边界", "重点"], lambda t: 20)
    assert result["strategy"] != "side"
    assert result["map_rect_mm"][2] == 279
    assert result["extent"] == [0, 0, 100, 100]
    assert result["map_rect_mm"][1] < 50


def test_manual_fixed_frame_expands_extent_without_clipping():
    scene = Scene(crs="EPSG:32647", layers=[{"id": "data", "source_ref": "actual"}])
    page = Page(
        id="main",
        layout={
            "placement": "manual",
            "map_rect_mm": [9, 25, 170, 140],
            "legend_rect_mm": [190, 25, 90, 100],
            "fit": "frame",
        },
    )
    result = compile_layout(scene, page, [0, 0, 10, 10], ["真实图例"], lambda t: 20)
    x0, y0, x1, y1 = result["extent"]
    assert x0 <= 0 and y0 <= 0 and x1 >= 10 and y1 >= 10
    assert abs((x1 - x0) / (y1 - y0) - 170 / 140) < 1e-9


async def test_native_render_exposes_frames_regions_and_review_gets_real_crops(tmp_path):
    import json
    from pathlib import Path

    from test_native_scene_workflow import setup

    from geoharness.cartography.quality import _review_images
    from geoharness.tools.geo_map_review import _verified_render_evidence
    from geoharness.tools.geo_native_map import GeoNativeMapTool, NativeMapInput

    context, scene = setup(tmp_path)
    scene.width_mm, scene.height_mm = 297, 420
    scene.pages[0].purpose = "总览只表达最终时段；时序细节由其他页承载"
    result = await GeoNativeMapTool().execute(NativeMapInput(action="render", scene=scene), context)
    assert not result.is_error, result.output
    manifest = json.loads(Path(result.metadata["project_manifest"]).read_text("utf8"))
    frame = manifest["native_facts"]["frames"]["overview"]
    assert frame["page_id"] == "overview" and frame["rect_mm"][1] < 50
    assert frame["rect_mm"][2] > 260
    import hashlib

    path = Path(result.metadata["preview_path"])
    evidence = _verified_render_evidence(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        result.metadata["map_receipt"],
        [tmp_path],
    )
    blocks, rois = _review_images(path, evidence, context.metadata["geoharness_config"].map_review)
    assert len(blocks) >= 4
    assert any(r["frame_id"] == "overview" and r.get("resampled") is False for r in rois)
    from geoharness.cartography.review_contract import stable_contract

    contract = stable_contract(evidence)
    assert contract["frames"] and contract["regions"]
    assert "最终时段" in contract["purpose"]


async def test_native_locator_tracks_actual_extent_and_labels_are_only_paper_offsets(tmp_path):
    import json
    from pathlib import Path

    from test_native_scene_workflow import setup

    from geoharness.tools.geo_native_map import GeoNativeMapTool, NativeMapInput

    context, scene = setup(tmp_path)
    scene.layers[0].label_offsets_mm = {"2": [6, 6]}
    scene.pages[0].locator_pages = ["detail"]
    scene.pages.insert(
        1,
        Page(
            id="detail",
            role="detail",
            layers=["places"],
            extent=[-200, -200, 2000, 1600],
            layout={"fit": "frame", "placement": "side"},
        ),
    )
    result = await GeoNativeMapTool().execute(NativeMapInput(action="render", scene=scene), context)
    assert not result.is_error, result.output
    plan = json.loads((Path(result.metadata["project_dir"]) / "scene-plan.json").read_text("utf8"))
    overview, detail = plan["pages"][:2]
    assert overview["locator_frames"][0]["extent"] == detail["frame"]["extent"]
    label = next(t for t in overview["texts"] if t.get("entity_key") == "2")
    assert label["anchor_mm"] != [label["x_mm"], label["y_mm"]]
    assert len(overview["lines"]) >= 2
    assert len(plan["layers"]) == 1  # No auxiliary data/publishing workflow.
    changed = await GeoNativeMapTool().execute(
        NativeMapInput(
            action="render",
            scene_ref=result.metadata["scene_ref"],
            edits=[
                {"target": "page", "id": "detail", "changes": {"extent": [-300, -300, 2200, 1800]}}
            ],
        ),
        context,
    )
    assert not changed.is_error, changed.output
    updated = json.loads(
        (Path(changed.metadata["project_dir"]) / "scene-plan.json").read_text("utf8")
    )
    assert (
        updated["pages"][0]["locator_frames"][0]["extent"] == updated["pages"][1]["frame"]["extent"]
    )
    assert not changed.metadata["native_facts"]["page_previews"][0]["reused"]
