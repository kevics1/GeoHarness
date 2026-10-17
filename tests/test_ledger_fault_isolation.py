"""An unavailable map ledger must not erase artifacts or become a false quality pass."""
import json
from pathlib import Path

from openharness.tools.base import ToolExecutionContext, ToolResult
from PIL import Image

from geoharness.cartography import jobs, tracking  # noqa: F401 (bind before failure injection)
from geoharness.config.settings import GeoConfig


async def test_failed_draft_registration_preserves_successful_native_result(tmp_path, monkeypatch):
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    path = tmp_path / "rendered.png"
    import geopandas as gpd
    from shapely.geometry import Point

    source = tmp_path / "input.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs=3857).to_file(source)
    async def create(*args, **kwargs):
        path.write_bytes(b"native result")
        return ToolResult(output="created", metadata={"preview_path": str(path)})
    def fail(*args, **kwargs):
        raise ValueError("injected ledger failure")
    # Exercise the real mutation wrapper around a native result, not its helper.
    monkeypatch.setattr(GeoQgisProjectTool, "_execute", create)
    monkeypatch.setattr(jobs, "record_map_draft", fail)
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action="create",
        layers=[{"file_path": str(source)}]), context)
    assert result.is_error
    assert result.metadata["render_executed"]
    assert result.metadata["preview_path"] == str(path)
    assert path.is_file()


async def test_selected_draft_can_be_copied_when_ledger_is_unreadable(tmp_path, monkeypatch):
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    path = tmp_path / "preview.png"
    Image.new("RGB", (8, 8), "red").save(path)
    def fail(*args, **kwargs):
        raise ValueError("injected ledger failure")
    monkeypatch.setattr(jobs, "map_job_receipt", fail)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    tool = GeoDeliverTool()
    denied = await tool.execute(GeoDeliverInput(primary_map=str(path)), context)
    assert denied.is_error
    delivered = await tool.execute(GeoDeliverInput(primary_map=str(path),
        version_policy="selected"), context)
    assert not delivered.is_error, delivered.output
    assert delivered.metadata["quality_status"] == "draft"
    assert delivered.metadata["ledger_errors"]
    assert Path(delivered.metadata["primary_map"]).is_file()


async def test_ledger_health_does_not_need_to_parse_oversized_history(tmp_path):
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    identity = jobs.ensure_map_job(context.metadata, cwd=tmp_path, config=config)
    receipt = tmp_path / config.cartography.jobs_dir / identity / "receipt.json"
    receipt.write_text(" " * (16*1024*1024 + 1), encoding="utf-8")
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="inspect", map_id=identity), context)
    assert not result.is_error, result.output
    assert json.loads(result.output)["requires_recovery"]


async def test_review_only_retains_pixel_report_without_a_healthy_ledger(tmp_path, monkeypatch):
    from test_map_receipt_workflow import PassingReviewer

    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    image = tmp_path / "preview.png"
    Image.new("RGB", (32, 32), "blue").save(image)
    def fail(*args, **kwargs):
        raise ValueError("injected ledger failure")
    monkeypatch.setattr(jobs, "map_job_receipt", fail)
    client = PassingReviewer()
    context = ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": GeoConfig(), "map_review_client": client})
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(image), task="Review actual pixels", auto_repair=False), context)
    assert not result.is_error, result.output
    assert result.metadata["receipt_status"] == "unregistered"
    assert result.metadata["quality_status"] == "draft"
    assert Path(result.metadata["review_path"]).is_file()
    assert len(client.requests) == 1
