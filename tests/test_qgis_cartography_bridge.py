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


def test_default_static_renderer_is_qgis():
    assert GeoConfig().cartography.static_renderer == "qgis"


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
