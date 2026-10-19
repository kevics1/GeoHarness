"""Deliver readable, editable artifacts into the current workspace."""

import asyncio
import hashlib
import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import DeliveryConfig, GeoConfig, MapReviewConfig
from geoharness.tools.geo_cartography import GeoCartographyInput, GeoCartographyTool
from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool


def _script_project(tmp_path):
    from PIL import Image

    from geoharness.cartography.project import export_map_project

    image = tmp_path / "final-custom.png"
    Image.new("RGB", (20, 20), "white").save(image)
    project = export_map_project(
        tmp_path / "project", output_path=image,
        layers=[{"gdf": gpd.GeoDataFrame(geometry=[Point(0, 0)], crs=3857)}],
        render_kwargs={"mode": "layers"},
    )
    return image, project


@pytest.mark.asyncio
async def test_native_map_has_editable_project_and_default_workspace_output(tmp_path):
    config = GeoConfig(map_review=MapReviewConfig(enabled=False))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    data = gpd.GeoDataFrame({"value": [1, 2]}, geometry=[Point(0, 0), Point(1, 1)], crs=3857)
    mapped = await GeoCartographyTool().execute(
        GeoCartographyInput(
            geojson=data.to_json(),
            field_name="value",
            output_format="png",
            title="可编辑地图",
        ),
        context,
    )
    assert not mapped.is_error, mapped.output
    assert Path(mapped.metadata["output_path"]).parent == tmp_path / "exports"
    assert not mapped.metadata.get('qgis_project')
    assert not list(tmp_path.rglob('*.qgz'))
    delivered = await GeoDeliverTool().execute(
        GeoDeliverInput(
            files=[mapped.metadata["output_path"], mapped.metadata["review_path"]],
            project_dir=mapped.metadata["project_dir"],
            name="用户交付",
        ),
        context,
    )
    assert not delivered.is_error, delivered.output
    folder = Path(delivered.metadata["delivery_dir"])
    assert folder.parent == tmp_path / "exports"
    assert Path(delivered.metadata["qgis_project"]).is_file()
    assert list((folder / "project" / "data").glob("*.gpkg"))
    # The original and any user edits to a delivered project are preserved.
    edited = folder / "project" / "README.md"
    edited.write_text("User modified this", encoding="utf-8")
    again = await GeoDeliverTool().execute(
        GeoDeliverInput(
            files=[mapped.metadata["output_path"]],
            name="用户交付",
        ),
        context,
    )
    assert not again.is_error
    assert Path(again.metadata["delivery_dir"]) != folder
    assert edited.read_text(encoding="utf-8") == "User modified this"


@pytest.mark.asyncio
async def test_script_map_can_export_project_without_rendering_again(tmp_path, monkeypatch):
    from PIL import Image

    data = gpd.GeoDataFrame(geometry=[Point(0, 0)], crs=3857)
    image = tmp_path / "custom.png"
    Image.new("RGB", (20, 20), "white").save(image)
    original = image.read_bytes()
    tool = GeoCartographyTool()
    monkeypatch.setattr(tool, "_render", lambda *args: pytest.fail("Project must not render again"))
    ctx = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await tool.execute(
        GeoCartographyInput(
            action="project",
            geojson=data.to_json(),
            output_path=str(image),
        ),
        ctx,
    )
    assert not result.is_error, result.output
    assert Path(result.metadata["qgis_project"]).is_file()
    assert image.read_bytes() == original
    assert result.metadata["quality_status"] == "draft"
    assert result.metadata["preview_sha256"] == hashlib.sha256(original).hexdigest()


@pytest.mark.asyncio
async def test_delivery_rejects_external_destination_and_credentials(tmp_path):
    file = tmp_path / ".env"
    file.write_text("Never deliver credentials", encoding="utf-8")
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoDeliverTool().execute(GeoDeliverInput(files=[str(file)]), context)
    assert result.is_error and not (tmp_path / "exports").exists()
    context.metadata["geoharness_config"] = GeoConfig(
        delivery=DeliveryConfig(outputs_dir=str(tmp_path.parent / "outside-delivery")),
    )
    result = await GeoDeliverTool().execute(GeoDeliverInput(files=[str(file)]), context)
    assert result.is_error and "subfolder" in result.output


@pytest.mark.asyncio
async def test_project_source_names_resolve_against_workspace_not_process_cwd(
    tmp_path, monkeypatch
):
    from PIL import Image

    gpd.GeoDataFrame(geometry=[Point(0, 0)], crs=3857).to_file(tmp_path / "assets.gpkg")
    Image.new("RGB", (20, 20), "white").save(tmp_path / "map.png")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    ctx = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoCartographyTool().execute(
        GeoCartographyInput(
            action="project",
            source_name="assets.gpkg",
            output_path="map.png",
        ),
        ctx,
    )
    assert not result.is_error, result.output
    assert Path(result.metadata["qgis_project"]).is_file()


@pytest.mark.asyncio
async def test_delivery_filename_collisions_do_not_overwrite_selected_artifacts(tmp_path):
    paths = []
    names = ("map-2.png", "map.png", "map.png", "DELIVERY.JSON", "PROJECT",
             "SUMMARY.JSON", "summary.md", "MAP-RECEIPT.JSON")
    for index, name in enumerate(names):
        directory = tmp_path / str(index)
        directory.mkdir()
        path = directory / name
        path.write_text(str(index), encoding="utf-8")
        paths.append(str(path))
    ctx = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoDeliverTool().execute(
        GeoDeliverInput(files=paths, allow_report_only=True), ctx)
    assert not result.is_error, result.output
    copied = result.metadata["files"]
    assert len(set(copied)) == len(names)
    assert [Path(path).read_text(encoding="utf-8") for path in copied] == [
        str(index) for index in range(len(names))]
    assert Path(result.metadata["summary_json"]).name == "summary.json"
    assert json.loads(Path(result.metadata["summary_json"]).read_text(encoding="utf-8"))[
        "primary_map_sha256"] == result.metadata["primary_map_sha256"]


@pytest.mark.asyncio
async def test_delivery_rejects_map_from_different_project(tmp_path):
    from PIL import Image

    actual, project = _script_project(tmp_path)
    other = tmp_path / "generic-native.png"
    Image.new("RGB", (20, 20), "red").save(other)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoDeliverTool().execute(GeoDeliverInput(
        files=[str(other)], project_dir=project["project_dir"],
    ), context)
    assert result.is_error and "differs" in result.output and "SHA256" in result.output
    assert not (tmp_path / "exports").exists()
    # An explicitly selected wrong primary map also fails, even when the right map is listed.
    result = await GeoDeliverTool().execute(GeoDeliverInput(
        files=[str(actual)], map_path=str(other), project_dir=project["project_dir"],
    ), context)
    assert result.is_error and "Primary map differs" in result.output


@pytest.mark.asyncio
async def test_project_snapshot_rejects_changed_preview(tmp_path):
    from PIL import Image

    image, project = _script_project(tmp_path)
    preview = Path(project["project_dir"]) / "preview.png"
    Image.new("RGB", (20, 20), "red").save(preview)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoDeliverTool().execute(GeoDeliverInput(
        files=[str(image)], project_dir=project["project_dir"],
    ), context)
    assert result.is_error and "recorded snapshot" in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("review, quality", [
    (None, "draft"),
    ({"status": "unverified", "summary": "Unavailable"}, "draft"),
    ({"status": "issues", "summary": "Legend clipped"}, "draft"),
    ({"status": "passed", "summary": "Clear", "issues": []}, "draft"),
    ({"status": "passed", "summary": "Repair needed", "layout_patch": {
        "legend_outside": True,
    }}, "draft"),
    ({"status": "passed", "summary": "Clear", "issues": [
        {"severity": "warning", "description": "Legend clipped"},
    ]}, "draft"),
])
async def test_delivery_quality_requires_review_of_same_image(tmp_path, review, quality):
    image, project = _script_project(tmp_path)
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    cache = {digest: review} if review else {}
    context = ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": GeoConfig(), "map_workflow_state": {"cache": cache},
    })
    result = await GeoDeliverTool().execute(GeoDeliverInput(
        files=[str(image)], project_dir=project["project_dir"],
    ), context)
    assert not result.is_error, result.output
    assert result.metadata["quality_status"] == quality
    assert result.metadata["primary_map_sha256"] == digest
    assert result.metadata["project_preview_sha256"] == digest
    assert Path(result.metadata["primary_map"]).read_bytes() == image.read_bytes()


@pytest.mark.asyncio
async def test_review_report_without_matching_hash_cannot_mark_delivery_passed(tmp_path):
    image, project = _script_project(tmp_path)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    report = tmp_path / "map-review.json"
    for report_hash, expected in [("", "draft"), ("0" * 64, "draft"), (
        hashlib.sha256(image.read_bytes()).hexdigest(), "draft",
    )]:
        report.write_text(json.dumps({
            "status": "passed", "summary": "Clear", "issues": [],
            "image_sha256": report_hash,
        }), encoding="utf-8")
        result = await GeoDeliverTool().execute(GeoDeliverInput(
            files=[str(image)], project_dir=project["project_dir"], review_path=str(report),
        ), context)
        assert not result.is_error, result.output
        assert result.metadata["quality_status"] == expected


@pytest.mark.asyncio
async def test_project_only_preserves_actual_risk_styles_and_extent(tmp_path, monkeypatch):
    from PIL import Image

    points = gpd.GeoDataFrame(
        {"RiskTier": ["High", "Low"]}, geometry=[Point(0, 0), Point(1, 1)], crs=3857,
    )
    image = tmp_path / "script-styled.png"
    Image.new("RGB", (20, 20), "white").save(image)
    tool = GeoCartographyTool()
    monkeypatch.setattr(tool, "_render", lambda *args: pytest.fail("No project redraw"))
    ctx = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await tool.execute(GeoCartographyInput(
        action="project", output_path=str(image), map_extent=[-1, -1, 2, 2],
        layers=json.dumps([{
            "geojson": points.to_json(), "label": "Sites", "field_name": "RiskTier",
            "categories": ["High", "Low"], "colors": ["#ff0000", "#00ff00"],
        }]),
    ), ctx)
    assert not result.is_error, result.output
    manifest = json.loads(Path(result.metadata["project_file"]).read_text(encoding="utf-8"))
    assert manifest["preview_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert manifest["extent"] == [-1, -1, 2, 2]
    assert manifest["layers"][0]["style"]["column"] == "RiskTier"
    assert [item["color"] for item in manifest["layers"][0]["style"]["categories"]][:2] == [
        "#ff0000", "#00ff00",
    ]


@pytest.mark.asyncio
async def test_native_multilayer_tool_passes_risk_field_to_renderer_and_project(tmp_path):
    from PIL import Image

    points = gpd.GeoDataFrame(
        {"RiskTier": ["High", "Low"]}, geometry=[Point(0, 0), Point(1, 1)], crs=3857,
    )
    context = ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": GeoConfig(map_review=MapReviewConfig(enabled=False)),
    })
    result = await GeoCartographyTool().execute(GeoCartographyInput(
        action="export", output_format="png", output_path="risk.png",
        renderer="matplotlib",
        layers=json.dumps([{"geojson": points.to_json(), "field_name": "RiskTier"}]),
    ), context)
    assert not result.is_error, result.output
    manifest = json.loads(Path(result.metadata["project_file"]).read_text(encoding="utf-8"))
    style = manifest["layers"][0]["style"]
    assert style["type"] == "categorizedSymbol" and style["column"] == "RiskTier"
    colors = [item["color"] for item in style["categories"] if item["value"] is not None]
    assert len(set(colors)) == 2
    with Image.open(result.metadata["output_path"]) as rendered:
        pixels = {color for _count, color in rendered.convert("RGB").getcolors(
            maxcolors=rendered.width * rendered.height
        )}
    from matplotlib.colors import to_rgb

    assert {tuple(round(channel * 255) for channel in to_rgb(color)) for color in colors} <= pixels


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_native_render_refunds_only_its_reservation(tmp_path, monkeypatch, cancelled):
    from openharness.tools.base import ToolResult

    tool = GeoCartographyTool()

    async def fail(*args, **kwargs):
        if cancelled:
            raise asyncio.CancelledError()
        return ToolResult(output="Render failed", is_error=True)

    monkeypatch.setattr(tool, "_run_off_loop", fail)
    context = ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": GeoConfig(map_review=MapReviewConfig(max_repairs=1)),
        "map_workflow_state": {"maps": {"risk": {"renders": 1, "reviews": 0}}},
    })
    points = gpd.GeoDataFrame(geometry=[Point(0, 0)], crs=3857)
    args = GeoCartographyInput(geojson=points.to_json(), map_id="risk", output_format="png")
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await tool.execute(args, context)
    else:
        assert (await tool.execute(args, context)).is_error
    assert context.metadata["map_workflow_state"]["maps"]["risk"]["renders"] == 1


@pytest.mark.asyncio
async def test_legacy_limit_allows_render_and_preserves_prior_success_accounting(
    tmp_path, monkeypatch,
):
    tool = GeoCartographyTool()
    original = tool._run_off_loop

    async def preflight_and_render(work, *, what):
        if what == "map rendering":
            from openharness.tools.base import ToolResult

            return ToolResult(output="Rendered",
                              metadata={"output_path": str(tmp_path / "map.png")})
        assert what == "map preflight"
        return await original(work, what=what)

    monkeypatch.setattr(tool, "_run_off_loop", preflight_and_render)
    context = ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": GeoConfig(map_review=MapReviewConfig(max_repairs=0)),
        "map_workflow_state": {"maps": {"risk": {"renders": 1, "reviews": 0}}},
    })
    points = gpd.GeoDataFrame(geometry=[Point(0, 0)], crs=3857)
    result = await tool.execute(GeoCartographyInput(
        geojson=points.to_json(), map_id="risk", output_format="png",
    ), context)
    assert not result.metadata.get("render_budget_exhausted", False)
    assert context.metadata["map_workflow_state"]["maps"]["risk"]["renders"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_repair_releases_its_budget_and_preserves_actual_layout(
    tmp_path, monkeypatch, cancelled
):
    from openharness.tools.base import ToolResult
    from PIL import Image

    from geoharness.cartography.quality import LayoutPatch, MapIssue, MapReview

    tool = GeoCartographyTool()
    calls = 0
    image = tmp_path / "map.png"
    Image.new("RGB", (32, 32), "white").save(image)

    async def render(*args, **kwargs):
        nonlocal calls
        if kwargs.get("what") == "map preflight":
            return await asyncio.to_thread(args[0])
        calls += 1
        if calls == 1:
            return ToolResult(output="Existing rendered map", metadata={"output_path": str(image)})
        if cancelled:
            raise asyncio.CancelledError()
        return ToolResult(output="Repair failed", is_error=True)

    async def review(*args, **kwargs):
        return MapReview(
            status="issues", summary="Legend overlap",
            issues=[MapIssue(severity="warning", description="Legend overlap")],
            layout_patch=LayoutPatch(width=12, legend_outside=True),
        ), {
            "review_cached": False, "review_budget_exhausted": False,
            "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        }

    monkeypatch.setattr(tool, "_run_off_loop", render)
    monkeypatch.setattr("geoharness.tools.geo_cartography.review_with_budget", review)
    config = GeoConfig(map_review=MapReviewConfig(max_repairs=1))
    context = ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": config, "map_review_client": object(),
    })
    points = gpd.GeoDataFrame(geometry=[Point(0, 0)], crs=3857)
    args = GeoCartographyInput(
        geojson=points.to_json(), map_id="risk", output_format="png", output_path=str(image),
        renderer="matplotlib",
    )
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await tool.execute(args, context)
    else:
        result = await tool.execute(args, context)
        assert not result.is_error, result.output
        assert result.metadata["review_status"] == "unverified"
        assert result.metadata["final_layout"] == config.map_review.layout.model_dump()
        report = json.loads(Path(result.metadata["review_path"]).read_text(encoding="utf-8"))
        assert report["issues"][0]["description"] == "Legend overlap"
        assert report["image_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert calls == 2
    assert context.metadata["map_workflow_state"]["maps"]["risk"]["renders"] == 1
