"""Native label rosters and per-frame label roles survive saved project export."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point, box

from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def test_number_table_and_detail_labels_are_retained_in_saved_layout(tmp_path):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f"QGIS unavailable: {exc}")
    points, background = tmp_path / "points.geojson", tmp_path / "background.geojson"
    gpd.GeoDataFrame({"number": range(1, 23), "name": [f"目标{i}" for i in range(1, 23)]},
        geometry=[Point(5000 + 100 * (i % 6), 5000 + 100 * (i // 6)) for i in range(22)],
        crs=3857).to_file(points)
    gpd.GeoDataFrame(geometry=[box(0, 0, 10000, 10000)], crs=3857).to_file(background)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    tool = GeoQgisProjectTool()
    created = await tool.execute(GeoQgisProjectInput(action="create", crs="EPSG:3857", dpi=72,
        layers=[{"file_path": str(background), "name": "context"},
                {"file_path": str(points), "name": "numbered", "label_field": "number"}],
        layout={"width_mm": 420, "height_mm": 297, "legend": False,
                "sidebar_width_mm": 100, "detail_minimum_zoom": 1.2,
                "main_frame": {"focus_layers": [0], "label_layers": []},
                "detail_frame": {"focus_layers": [1], "label_layers": [1]},
                "label_table": {"layer_index": 1, "fields": ["number", "name"],
                                    "headers": {"number": "编号", "name": "名称"},
                                "columns": 3, "height_mm": 55}}), context, _materialize=True)
    assert not created.is_error, created.output
    manifest = json.loads(Path(created.metadata["project_manifest"]).read_text(encoding="utf-8"))
    tables = manifest["layout"]["label_tables"]
    assert sum(t["rows"] for t in tables) == 22
    assert sum(t["visible_rows"] for t in tables) == 22
    assert {row[0] for t in tables for row in t["contents"]} == set(range(1, 23))
    assert manifest["layout"]["bounds_check"]["valid"]
    frames = {f["id"]: f for f in manifest["layout"]["map_frames"]}
    assert frames["main_map"]["scale"] / frames["detail_map"]["scale"] >= 1.2
    labels = manifest["native_facts"]["frames"]
    assert not labels["main_map"]["labels"]
    assert any(label["layer_index"] == 1 for label in labels["detail_map"]["labels"])
    exported = await tool.execute(GeoQgisProjectInput(action="export", dpi=72,
        project_path=created.metadata["project_file"]), context)
    assert not exported.is_error, exported.output
    saved = json.loads(Path(exported.metadata["project_manifest"]).read_text(encoding="utf-8"))
    assert sum(t["visible_rows"] for t in saved["layout"]["label_tables"]) == 22
    assert saved["preview_sha256"] == manifest["preview_sha256"]

