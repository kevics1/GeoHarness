"""One saved QGIS map has output-backed facts and independently verified stages."""

import asyncio
import hashlib
import json
from pathlib import Path
from threading import Event

import geopandas as gpd
import pytest
from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.tools.base import ToolExecutionContext
from PIL import Image
from shapely.geometry import Point, box

from geoharness.cartography.jobs import (
    begin_map_request,
    ensure_map_job,
    map_job_receipt,
    record_map_review,
    record_map_version,
)
from geoharness.config.settings import AnalysisConfig, GeoConfig, MapReviewConfig
from geoharness.tools.geo_analysis import GeoAnalysisInput, GeoAnalysisTool, SpatialSource
from geoharness.tools.geo_cartography import GeoCartographyInput, GeoCartographyTool
from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool
from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool


class PassingReviewer:
    """A test fixture, not evidence about the real service's accuracy."""

    def __init__(self):
        self.requests = []

    async def stream_message(self, request):
        self.requests.append(request)
        yield ApiMessageCompleteEvent(
            message=ConversationMessage.from_user_text(
                json.dumps(
                    {
                        "status": "passed",
                        "summary": "Test fixture passed.",
                        "issues": [],
                    }
                )
            ),
            usage=UsageSnapshot(),
        )


async def test_real_qgis_analysis_footer_receipt_delivery_and_restart(tmp_path):
    config = GeoConfig(
        analysis=AnalysisConfig(metric_crs="EPSG:32648"),
        map_review=MapReviewConfig(max_repairs=0),
    )
    reviewer = PassingReviewer()
    context = ToolExecutionContext(
        cwd=tmp_path,
        metadata={
            "geoharness_config": config,
            "map_review_client": reviewer,
        },
    )
    assets = gpd.GeoDataFrame(
        {"name": ["内部", "邻近", "外部"]},
        geometry=[Point(220000, 3100000), Point(220025, 3100000), Point(220100, 3100000)],
        crs=32648,
    )
    hazard = gpd.GeoDataFrame(
        geometry=[box(219990, 3099990, 220010, 3100010)],
        crs=32648,
    )
    analysis = await GeoAnalysisTool().execute(
        GeoAnalysisInput(
            action="impact",
            source=SpatialSource(geojson=assets.to_crs(4326).to_json()),
            reference=SpatialSource(geojson=hazard.to_crs(4326).to_json()),
            distance_m=20,
        ),
        context,
    )
    assert not analysis.is_error, analysis.output
    mapped = await GeoCartographyTool().execute(
        GeoCartographyInput(
            title="影响分析验证",
            map_id="initial",
            map_crs="EPSG:32647",
            output_path="exports/map.png",
            project_output_dir="exports/editable",
            analysis_manifests=[analysis.metadata["manifest_path"]],
            layers=json.dumps(
                [
                    {
                        "file_path": analysis.metadata["paths"]["reference"]["geojson"],
                        "label": "危险范围",
                        "color": "#cc3344",
                    },
                    {
                        "file_path": analysis.metadata["paths"]["result"]["geojson"],
                        "label": "居民点",
                        "label_field": "name",
                        "field_name": "affected",
                        "categories": [
                            {"value": True, "label": "受影响", "color": "#df5647"},
                            {"value": False, "label": "未影响", "color": "#32886c"},
                        ],
                    },
                ]
            ),
            qgis_layout={"footer_text": "合成数据验证"},
        ),
        context,
    )
    assert not mapped.is_error, mapped.output
    job_id = mapped.metadata["map_id"]
    assert "map_job_state" in context.metadata
    assert Path(mapped.metadata["qgis_project"]).parent == tmp_path / "exports/editable"
    footer = mapped.metadata["layout"]["footer_text"]
    for text in ("受影响 2", "未影响 1", "20 m", "EPSG:32648", "EPSG:32647"):
        assert text in footer
    review_payload = json.loads(reviewer.requests[0].messages[0].content[0].text.split(
        "任务及渲染证据：\n", 1,
    )[1])["evidence"]
    assert "versions" not in review_payload
    assert review_payload["previous_visual_reviews"] == []
    assert review_payload["remaining_render_versions"] is None
    assert review_payload["generation_unlimited"] is True
    receipt = mapped.metadata["map_receipt"]
    assert receipt["quality_status"] == "passed"
    assert receipt["analysis_correctness"] == "unverified"
    assert all(
        receipt["stages"][stage]["status"] == "passed"
        for stage in (
            "data_checked",
            "rendered",
            "layout_checked",
            "visual_checked",
            "packaged",
        )
    )
    denied = await GeoCartographyTool().execute(
        GeoCartographyInput(
            geojson=assets.to_crs(4326).to_json(),
            map_id="renamed-map",
            title="换名",
        ),
        context,
    )
    assert not denied.metadata.get("render_budget_exhausted", False)
    delivered = await GeoDeliverTool().execute(
        GeoDeliverInput(
            primary_map=mapped.metadata["primary_map"],
            project_dir=mapped.metadata["project_dir"],
            review_path=mapped.metadata["review_path"],
            name="验收交付",
        ),
        context,
    )
    assert not delivered.is_error, delivered.output
    assert delivered.metadata["quality_status"] == "passed"
    saved = json.loads(Path(delivered.metadata["receipt_path"]).read_text(encoding="utf-8"))
    assert saved["map_id"] == job_id and saved["stages"]["delivered"]["status"] == "passed"
    restarted = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    queried = await GeoCartographyTool().execute(
        GeoCartographyInput(
            action="status",
            map_path=mapped.metadata["primary_map"],
        ),
        restarted,
    )
    assert queried.metadata["map_receipt"]["map_id"] == job_id
    assert queried.metadata["map_receipt"]["quality_status"] == "passed"
    begin_map_request(restarted.metadata, cwd=tmp_path, config=config, continuation=True)
    assert ensure_map_job(restarted.metadata, cwd=tmp_path, config=config) == job_id


def _verified_package(tmp_path):
    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    image = tmp_path / "map.png"
    Image.new("RGB", (24, 24), "white").save(image)
    project = tmp_path / "editable"
    project.mkdir()
    data = project / "data.gpkg"
    data.write_bytes(b"snapshot data")
    (project / "map.qgz").write_bytes(b"project fixture")
    (project / "preview.png").write_bytes(image.read_bytes())
    layout = {"bounds_check": {"checked": True, "valid": True, "problems": []}}
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    (project / "project.json").write_text(
        json.dumps(
            {
                "backend": "qgis",
                "project_file": "map.qgz",
                "preview": "preview.png",
                "preview_sha256": digest,
                "layout": layout,
                "files": ["map.qgz", "data.gpkg", "preview.png", "project.json"],
            }
        ),
        encoding="utf-8",
    )
    job_id = ensure_map_job(context.metadata, cwd=tmp_path, config=config, artifact_path=image)
    record_map_version(
        job_id,
        context.metadata,
        cwd=tmp_path,
        config=config,
        image_path=image,
        preflight={
            "status": "passed",
            "scope": "map_data_preflight",
            "display_crs": "EPSG:3857",
            "map_extent": [0, 0, 1, 1],
            "layers": [{"feature_count": 1}],
            "checks": {"geometry": "passed"},
        },
        layout=layout,
        project_dir=project,
    )
    review = {"status": "passed", "summary": "Fixture.", "issues": [], "image_sha256": digest}
    record_map_review(
        job_id, context.metadata, cwd=tmp_path, config=config, image_path=image, review=review
    )
    report = tmp_path / "review.json"
    report.write_text(json.dumps(review), encoding="utf-8")
    return context, image, project, data, report


async def test_delivery_detects_copied_data_change_even_if_source_restored(tmp_path, monkeypatch):
    import shutil

    context, image, project, data, report = _verified_package(tmp_path)
    copy = shutil.copy2
    original = data.read_bytes()

    def changed_copy(source, target):
        if Path(source) == data:
            data.write_bytes(b"changed during copy")
            try:
                return copy(source, target)
            finally:
                data.write_bytes(original)
        return copy(source, target)

    monkeypatch.setattr("geoharness.tools.geo_deliver.shutil.copy2", changed_copy)
    result = await GeoDeliverTool().execute(
        GeoDeliverInput(
            primary_map=str(image),
            project_dir=str(project),
            review_path=str(report),
        ),
        context,
    )
    assert not result.is_error, result.output
    assert result.metadata["snapshot_status"] == "changed"
    assert result.metadata["quality_status"] == "draft"
    assert result.metadata["map_receipt"]["status"] == "stale"
    assert data.read_bytes() == original


async def test_cancelled_delivery_drains_publisher_before_return(tmp_path, monkeypatch):
    import shutil

    context, image, project, _, report = _verified_package(tmp_path)
    started, release = asyncio.Event(), Event()
    loop, copy = asyncio.get_running_loop(), shutil.copy2

    def delayed(source, target):
        loop.call_soon_threadsafe(started.set)
        release.wait(10)
        return copy(source, target)

    monkeypatch.setattr("geoharness.tools.geo_deliver.shutil.copy2", delayed)
    task = asyncio.create_task(
        GeoDeliverTool().execute(
            GeoDeliverInput(
                primary_map=str(image),
                project_dir=str(project),
                review_path=str(report),
            ),
            context,
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "exports").glob("*/delivery.json"))


async def test_review_of_existing_old_version_does_not_register_new_render(tmp_path):
    context, old_image, _, _, _ = _verified_package(tmp_path)
    config = context.metadata["geoharness_config"]
    job_id = map_job_receipt(
        context.metadata, cwd=tmp_path, config=config, artifact_path=old_image
    )["job_id"]
    new_image = tmp_path / "new.png"
    Image.new("RGB", (24, 24), "red").save(new_image)
    record_map_version(
        job_id,
        context.metadata,
        cwd=tmp_path,
        config=config,
        image_path=new_image,
        preflight=None,
        layout=None,
        project_dir=None,
    )
    context.metadata["map_review_client"] = PassingReviewer()
    reviewed = await GeoMapReviewTool().execute(
        GeoMapReviewInput(
            file_path=str(old_image),
            task="Old image",
            map_id=job_id,
        ),
        context,
    )
    assert not reviewed.is_error, reviewed.output
    receipt = reviewed.metadata["map_receipt"]
    assert len(receipt["versions"]) == 2 and receipt["latest_version"] == 2
    assert receipt["current_version"] == 1 and receipt["quality_status"] == "passed"


async def test_same_image_updates_measured_layout_and_tracks_report_mutation(tmp_path):
    context, image, _, _, _ = _verified_package(tmp_path)
    context.metadata["map_review_client"] = PassingReviewer()
    geometry = {
        "scope": "layout_geometry",
        "status": "passed",
        "image_sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
        **{
            key: []
            for key in (
                "panel_overlaps",
                "label_overlaps",
                "clipped_labels",
                "overflow_text",
                "figure_text_overlaps",
                "clipped_figure_text",
                "text_panel_overlaps",
                "panels_outside_canvas",
            )
        },
    }
    path = image.with_suffix(".layout.json")
    path.write_text(
        json.dumps({**geometry, "status": "issues", "panel_overlaps": [{}]}), encoding="utf-8"
    )
    args = GeoMapReviewInput(file_path=str(image), task="Same pixels")
    first = await GeoMapReviewTool().execute(args, context)
    assert first.metadata["map_receipt"]["stages"]["layout_checked"]["status"] == "issues"
    assert first.metadata["map_receipt"]["stages"]["visual_checked"]["status"] == "unverified"
    path.write_text(json.dumps(geometry), encoding="utf-8")
    second = await GeoMapReviewTool().execute(args, context)
    assert second.metadata["map_receipt"]["quality_status"] == "passed"
    assert len(second.metadata["map_receipt"]["versions"]) == 1
    path.write_text("{}", encoding="utf-8")
    config = context.metadata["geoharness_config"]
    receipt = map_job_receipt({}, cwd=tmp_path, config=config, artifact_path=image)
    assert receipt["stages"]["layout_checked"]["status"] == "stale"
    assert receipt["quality_status"] == "draft"
