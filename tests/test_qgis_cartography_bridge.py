"""Default static maps and their editable projects use one native layout."""

import asyncio
import hashlib
import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import box

from geoharness.config.settings import GeoConfig, MapReviewConfig, QgisConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.tools.geo_cartography import GeoCartographyInput, GeoCartographyTool


@pytest.mark.parametrize("deny_review", [False, True])
async def test_bridge_native_repair_keeps_matching_selected_package_without_extra_version(
    tmp_path, deny_review,
):
    from openharness.api.client import ApiMessageCompleteEvent
    from openharness.api.usage import UsageSnapshot
    from openharness.engine.messages import ConversationMessage, ToolUseBlock

    from geoharness.runtime import _build_permission_checker

    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError):
        pytest.skip("QGIS runtime unavailable")
    source = tmp_path / "areas.geojson"
    gpd.GeoDataFrame({"risk": ["High", "Low"]},
        geometry=[box(100, 25, 101, 26), box(101, 25, 102, 26)], crs=4326).to_file(source)

    class ReviewClient:
        calls = 0

        async def stream_message(self, request):
            self.calls += 1
            if self.calls == 1:
                (tmp_path / "exports/map.png").write_bytes(b"user-edited-preview")
                roi = next(json.loads(block.text) for block in request.messages[0].content
                           if getattr(block, "type", "") == "text"
                           and block.text.startswith('{"roi_ref": "legend"'))
                payload = {"status": "issues", "summary": "测试图例字号修正", "issues": [{
                    "issue_id": "legend-size", "severity": "warning",
                    "issue_code": "legend_mismatch", "description": "测试图例字号不足",
                    "frame_id": "legend", "roi_ref": roi["roi_ref"],
                    "bbox": [.1, .1, .9, .9], "source_indices": [0]}],
                    "qgis_patch": {"issue_ids": ["legend-size"],
                                   "layout": {"legend_font_size_pt": 12}}}
            else:
                payload = {"status": "passed", "summary": "离线测试审查通过", "issues": []}
            yield ApiMessageCompleteEvent(message=ConversationMessage(role="assistant", content=[
                ToolUseBlock(id=f"review-{self.calls}", name="record_map_review", input=payload)]),
                usage=UsageSnapshot(), stop_reason="tool_calls")

    client = ReviewClient()
    checker = _build_permission_checker(config)
    if deny_review:
        checker._settings.denied_tools.append("geo_map_review")
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config,
        "map_review_client": client, "geoharness_permission_checker": checker})
    rendered = await GeoCartographyTool().execute(GeoCartographyInput(
        title="工程配对测试", map_crs="EPSG:4326", output_path="exports/map.png",
        project_output_dir="exports/editable", layers=json.dumps([{
            "file_path": str(source), "field_name": "risk", "categories": [
                {"value": "High", "label": "高", "color": "#ff0000"},
                {"value": "Low", "label": "低", "color": "#00ff00"}]}])), context)
    assert not rendered.is_error, rendered.output
    metadata = rendered.metadata
    assert client.calls == (0 if deny_review else 2)
    assert metadata["repair_count"] == (0 if deny_review else 1)
    assert metadata["review_status"] == ("unverified" if deny_review else "passed")
    package = Path(metadata["project_dir"])
    assert package.is_relative_to(tmp_path / "exports")
    manifest = json.loads((package / "project.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(Path(metadata["primary_map"]).read_bytes()).hexdigest() == (
        manifest["preview_sha256"])
    assert len(metadata["map_receipt"]["versions"]) == (1 if deny_review else 2)
    if not deny_review:
        assert manifest["layout"]["effective_parameters"]["legend_font_size_pt"] == 12
        assert (tmp_path / "exports/map.png").read_bytes() == b"user-edited-preview"
        assert Path(metadata["primary_map"]) != tmp_path / "exports/map.png"


def test_default_static_renderer_is_qgis():
    assert GeoConfig().cartography.static_renderer == "qgis"


def test_bridge_preserves_optional_empty_source_indices(tmp_path):
    from geoharness.cartography.qgis_bridge import prepare_layers

    source = tmp_path / "empty.geojson"
    gpd.GeoDataFrame(geometry=[], crs=3857).to_file(source)
    subject = tmp_path / "subject.geojson"
    gpd.GeoDataFrame(geometry=[box(10, 10, 20, 20)], crs=3857).to_file(subject)
    args = GeoCartographyInput(layers=json.dumps([
        {"file_path": str(source), "required": False, "color": "#0000ff"},
        {"file_path": str(subject), "color": "#ff0000"}]),
        layout=GeoConfig().map_review.layout)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = prepare_layers(args, {}, context, tmp_path)
    assert len(result) == 2
    assert result[0]["expected_feature_count"] == 0
    assert result[1]["_source_binding"]["file_path"] == str(subject)


async def test_bridge_retains_focused_extent_policy_and_complete_native_plan(tmp_path):
    config = GeoConfig(map_review=MapReviewConfig(enabled=False))
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError):
        pytest.skip("QGIS runtime unavailable")
    paths = [tmp_path / name for name in ("background.geojson", "subject.geojson")]
    for path, shape in zip(paths, [box(-1000, -1000, 1000, 1000), box(10, 10, 20, 20)]):
        gpd.GeoDataFrame(geometry=[shape], crs=3857).to_file(path)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    common = {"map_crs": "EPSG:3857", "output_path": "exports/focus.png",
              "layers": json.dumps([{"file_path": str(paths[0]), "required": False},
                                    {"file_path": str(paths[1]), "color": "#ff0000"}]),
              "qgis_layout": {"main_frame": {"focus_layers": [1]}}}
    checked = await GeoCartographyTool().execute(
        GeoCartographyInput(action="preflight", **common), context)
    assert not checked.is_error, checked.output
    plan = checked.metadata["preflight"]["layout_plan"]
    assert plan["frames"]["main_map"]["focus_bbox"] == [10, 10, 20, 20]
    assert checked.metadata["preflight"]["frame_checks"]["valid"] is True
    created = await GeoCartographyTool().execute(GeoCartographyInput(**common), context)
    assert not created.is_error, created.output
    actual = created.metadata["preflight"]["layout_plan"]
    assert actual["frames"]["main_map"]["focus_layers"] == [1]
    assert created.metadata["preflight"]["map_extent"] == actual["frames"]["main_map"]["extent"]


async def test_disabled_native_renderer_returns_actionable_error(tmp_path):
    config = GeoConfig(qgis=QgisConfig(enabled=False))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    result = await GeoCartographyTool().execute(
        GeoCartographyInput(
            geojson=gpd.GeoDataFrame(geometry=[box(1, 1, 2, 2)], crs=4326).to_json(),
            output_path="exports/map.png",
            project_output_dir="exports/editable",
        ),
        context,
    )
    assert result.is_error and "renderer=matplotlib" in result.output
    assert not context.metadata["map_workflow_state"].get("maps")


async def test_native_map_preview_matches_portable_project(tmp_path):
    config = GeoConfig(map_review=MapReviewConfig(enabled=False))
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError):
        pytest.skip("QGIS runtime unavailable")
    source = tmp_path / "zones.geojson"
    gpd.GeoDataFrame(
        {"risk": ["High", "Low"], "name": ["危险区", "安全区"]},
        geometry=[box(100, 25, 101, 26), box(101, 25, 102, 26)],
        crs=4326,
    ).to_file(source, driver="GeoJSON")
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    result = await GeoCartographyTool().execute(
        GeoCartographyInput(
            title="风险分析图",
            output_path="exports/map.png",
            project_output_dir="exports/editable",
            map_crs="EPSG:4326",
            layers=json.dumps(
                [
                    {
                        "file_path": str(source),
                        "label": "风险区域",
                        "field_name": "risk",
                        "categories": [
                            {"value": "High", "label": "高", "color": "#ff0000"},
                            {"value": "Low", "label": "低", "color": "#00ff00"},
                        ],
                        "label_field": "name",
                    }
                ]
            ),
            qgis_layout={
                "detail_extent": {"xmin": 100, "ymin": 25, "xmax": 101, "ymax": 26},
                "footer_text": "来源：测试数据；显示坐标系 EPSG:4326",
            },
        ),
        context,
    )
    assert not result.is_error, result.output
    metadata = result.metadata
    project = Path(metadata["project_dir"])
    assert project == tmp_path / "exports/editable"
    assert Path(metadata["output_path"]).read_bytes().startswith(b"\x89PNG")
    manifest = json.loads((project / "project.json").read_text(encoding="utf-8"))
    assert Path(metadata["project_file"]).is_file()
    assert Path(metadata["output_path"]).parent == tmp_path / "exports"
    assert (
        hashlib.sha256(Path(metadata["output_path"]).read_bytes()).hexdigest()
        == (manifest["preview_sha256"])
    )
    assert metadata["review_status"] == "unverified"
    assert manifest["backend"] == "qgis"
    assert len(manifest["layout"]["map_frames"]) == 2
    assert metadata["layers"][0]["categories"][0]["color"] == "#ff0000"
    assert source.is_file()


async def test_corrupt_source_is_a_tool_error_before_render(tmp_path):
    source = tmp_path / "corrupt.geojson"
    source.write_text("bad GeoJSON", encoding="utf-8")
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoCartographyTool().execute(
        GeoCartographyInput(
            file_path=str(source),
            output_format="png",
        ),
        context,
    )
    assert result.is_error and "map preflight failed" in result.output
    assert not context.metadata["map_workflow_state"].get("maps")


async def test_invalid_qgis_output_directory_is_a_tool_error(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.write_text("keep", encoding="utf-8")
    config = GeoConfig(qgis=QgisConfig(outputs_dir=str(occupied)))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    result = await GeoCartographyTool().execute(
        GeoCartographyInput(
            geojson=gpd.GeoDataFrame(geometry=[box(1, 1, 2, 2)], crs=4326).to_json(),
            output_format="png",
        ),
        context,
    )
    assert result.is_error and "directory could not be created" in result.output
    assert occupied.read_text(encoding="utf-8") == "keep"


def test_legacy_explicit_layout_is_translated():
    from geoharness.cartography.qgis_bridge import native_layout
    from geoharness.config.settings import MapLayout

    args = GeoCartographyInput(layout=MapLayout(width=12, height=9, title_font_size=20))
    layout = native_layout(args)
    assert layout.width_mm == pytest.approx(304.8)
    assert layout.height_mm == pytest.approx(228.6)
    assert layout.title_font_size_pt == 20


def test_explicit_legend_font_reaches_native_layout_with_separate_footer_font():
    from geoharness.cartography.qgis_bridge import native_layout

    args = GeoCartographyInput(layout={"legend_font_size": 7},
                              qgis_layout={"body_font_size_pt": 9})
    assert native_layout(args).legend_font_size_pt == 7
    assert native_layout(args).body_font_size_pt == 9
    explicit = args.model_copy(update={"qgis_layout": args.qgis_layout.model_copy(
        update={"legend_font_size_pt": 8})})
    assert native_layout(explicit).legend_font_size_pt == 8


async def test_cancelled_source_preparation_preserves_cancellation(tmp_path, monkeypatch):
    from threading import Event

    started = asyncio.Event()
    release = Event()
    loop = asyncio.get_running_loop()

    def preparing(*args):
        loop.call_soon_threadsafe(started.set)
        release.wait(10)
        raise RuntimeError("writer failed after cancellation")

    monkeypatch.setattr("geoharness.cartography.qgis_bridge.prepare_layers", preparing)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    task = asyncio.create_task(
        GeoCartographyTool().execute(
            GeoCartographyInput(
                geojson='{"type":"FeatureCollection","features":[{"type":"Feature",'
                        '"properties":{},"geometry":{"type":"Point","coordinates":[1,1]}}]}',
                output_format="png",
            ),
            context,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list((tmp_path / ".geoharness/qgis").glob("sources-*"))
