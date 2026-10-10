"""Read packaged maps with the real, independently installed QGIS runtime."""

import xml.etree.ElementTree as ET
from pathlib import Path

import geopandas as gpd
import pytest
from PIL import Image
from shapely.geometry import box

from geoharness.cartography.project import export_map_project
from geoharness.config.settings import QgisConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.qgis.runner import execute_qgis


async def test_pal_label_offsets_priority_and_unlabeled_obstacle_survive_saved_project(tmp_path):
    from shapely.geometry import Point

    settings = QgisConfig()
    try:
        discover_qgis(settings, cwd=tmp_path)
    except (ValueError, RuntimeError, OSError) as exc:
        pytest.skip(f"An independent QGIS installation is required: {exc}")
    point = tmp_path / "point.geojson"
    gpd.GeoDataFrame({"name": ["起火点"]}, geometry=[Point(50, 50)], crs=3857).to_file(point)
    polygon = tmp_path / "focus.geojson"
    gpd.GeoDataFrame(geometry=[box(40, 40, 60, 60)], crs=3857).to_file(polygon)
    result = await execute_qgis("project_create", {
        "layers": [
            {"file_path": str(polygon), "name": "focus", "style": {
                "obstacle": True, "obstacle_factor": 3}},
            {"file_path": str(point), "name": "point", "label_field": "name", "label_style": {
                "placement": "over_point", "distance_mm": 2, "offset_x_mm": 3,
                "offset_y_mm": -2, "priority": 10}},
        ], "crs": "EPSG:3857", "map_extent": [0, 0, 100, 100],
        "layout": {"name": "Map"},
    }, cwd=tmp_path, settings=settings, read_roots=[tmp_path])
    assert result["status"] == "succeeded", result.get("error_summary")
    inspected = await execute_qgis("project_inspect", {
        "project_path": result["result"]["project_file"],
    }, cwd=tmp_path, settings=settings, read_roots=[tmp_path])
    assert inspected["status"] == "succeeded", inspected.get("error_summary")
    layers = {layer["name"]: layer for layer in inspected["result"]["layers"]}
    label = layers["point"]["label_style"]
    assert label["placement"] == "OverPoint"
    assert label["priority"] == 10
    assert label["distance_mm"] == 2
    assert label["offset_x_mm"] == 3 and label["offset_y_mm"] == -2
    assert label["distance_unit"] == label["offset_unit"] == "Millimeters"
    assert layers["focus"]["labels_enabled"] is False
    assert layers["focus"]["obstacle_enabled"] is True
    assert layers["focus"]["obstacle_factor"] == 3


async def test_legacy_project_readback_preserves_metric_crs_and_scale(tmp_path: Path):
    settings = QgisConfig()
    try:
        discover_qgis(settings, cwd=tmp_path)
    except (ValueError, RuntimeError, OSError) as exc:
        pytest.skip(f"An independent QGIS installation is required: {exc}")
    preview = tmp_path / "map.png"
    Image.new("RGB", (64, 64), "white").save(preview)
    data = gpd.GeoDataFrame(
        {"name": ["Known metric bounds"]},
        geometry=[box(809000, 3071000, 831000, 3096000)],
        crs="EPSG:32647",
    )
    package = export_map_project(
        tmp_path / "project",
        layers=[{"gdf": data, "label": "Metric layer"}],
        output_path=preview,
        render_kwargs={"mode": "layers", "title": "Readback regression"},
        extent=[809000, 3071000, 831000, 3096000],
    )
    result = await execute_qgis(
        "project_inspect",
        {"project_path": package["qgis_project"]},
        cwd=tmp_path,
        settings=settings,
        read_roots=[tmp_path],
    )
    assert result["status"] == "succeeded", result.get("error_summary")
    details = result["result"]
    assert details["crs"] == "EPSG:32647"
    assert details["crs_valid"] is True
    frames = details["layouts"][0]["map_frames"]
    assert frames and all(frame["crs_valid"] for frame in frames)
    assert all(1000 < frame["scale"] < 500000 for frame in frames)
    assert all(layer["valid"] for layer in details["layers"])
    # A valid CRS node and valid data layers do not compensate for disabled projection.
    qgs = Path(package["qgis_project"])
    tree = ET.parse(qgs)
    enabled = tree.find("properties/SpatialRefSys/ProjectionsEnabled")
    assert enabled is not None
    enabled.text = "0"
    tree.write(qgs, encoding="utf-8")
    rejected = await execute_qgis(
        "project_inspect", {"project_path": str(qgs)}, cwd=tmp_path,
        settings=settings, read_roots=[tmp_path],
    )
    assert rejected["status"] == "failed"
    assert "Project CRS is invalid" in rejected["error_summary"]
