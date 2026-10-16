"""Native sizing keeps complete content and coverage instead of trial-and-error crops."""

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point, box

from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.qgis.project_worker import _detail_zoom_satisfied, _fit_detail_paper
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def test_zoom_rounding_tolerance_and_paper_proposal():
    assert _detail_zoom_satisfied(1.9999953108881092, 2)
    assert not _detail_zoom_satisfied(1.991, 2)
    spec = {"height_mm": 195, "detail_height_ratio": 0.42,
            "detail_extent": {"xmin": 0, "ymin": 0, "xmax": 6000, "ymax": 6000}}
    regions = {"main_map": [12, 12, 208, 148], "detail_map": [225, 12, 60, 62.16],
               "legend": [225, 79.16, 60, 80.84]}
    proposal = _fit_detail_paper(spec, regions, 1.691, 2)
    assert proposal["height_mm"] > spec["height_mm"]
    assert proposal["width_mm"] - proposal["sidebar_width_mm"] == pytest.approx(237)
    assert proposal["detail_extent"] == spec["detail_extent"]
    assert _fit_detail_paper(spec, regions, 0.1, 20) is None


@pytest.mark.parametrize("minimum_zoom", [None, 2])
async def test_native_legend_table_and_zoom_fit_then_reuse_plan(tmp_path, minimum_zoom):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f"QGIS unavailable: {exc}")
    region = tmp_path / "region.geojson"
    roster = tmp_path / "roster.geojson"
    gpd.GeoDataFrame(geometry=[box(0, 0, 10000, 10000)], crs=3857).to_file(region)
    gpd.GeoDataFrame({"key": list(range(24)),
                      "name": [f"Shelter {index}" for index in range(24)]},
                     geometry=[Point(3000 + index * 70, 3000) for index in range(24)],
                     crs=3857).to_file(roster)
    layout = {"height_mm": 195, "detail_height_ratio": 0.42,
              "detail_extent": {"xmin": 2000, "ymin": 2000, "xmax": 8000, "ymax": 8000},
              "label_table": {"layer_index": 1, "fields": ["key", "name"],
                              "columns": 1, "height_mm": 15}}
    if minimum_zoom is not None:
        layout["detail_minimum_zoom"] = minimum_zoom
    inputs = {"action": "preflight", "crs": "EPSG:3857", "layers": [
        {"file_path": str(region), "name": "Region"},
        {"file_path": str(roster), "name": "Shelters", "style": {
            "renderer": "categorized", "field": "key", "categories": [
                {"value": index, "label": f"Shelter class {index}", "color": "#008800"}
                for index in range(24)]}}], "layout": layout}
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(**inputs), context)
    assert not result.is_error, result.output
    preflight = result.metadata["preflight"]
    plan = preflight["layout_plan"]
    assert plan["parameters"]["height_mm"] > 195
    assert plan["parameters"]["detail_extent"] == layout["detail_extent"]
    assert plan["parameters"]["label_table"]["columns"] == 1
    # Auto rosters may move intact to their own pages instead of consuming
    # more map area. Check delivered rows, rather than one fitting mechanism.
    table_plan = plan["parameters"]["label_table"]
    assert table_plan["placement"] == "pages" or table_plan["height_mm"] > 15
    tables = preflight["layout"]["label_tables"]
    assert sum(table["rows"] for table in tables) == 24
    assert sum(table["visible_rows"] for table in tables) == 24
    assert preflight["layout"]["roster_selection"]["membership_check"]["valid"]
    frame = plan["frames"]["detail_map"]
    assert frame["extent"][0] <= 2000 and frame["extent"][2] >= 8000
    assert frame["extent"][1] <= 2000 and frame["extent"][3] >= 8000
    if minimum_zoom:
        scales = {item["id"]: item["scale"] for item in preflight["layout"]["map_frames"]}
        assert _detail_zoom_satisfied(
            scales["main_map"] / scales["detail_map"], minimum_zoom)
    # Re-measure the frozen capsule: no export, provider request or changed adaptation.
    repeated = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        **{**inputs, "layout": {**layout, "layout_plan": plan}}), context)
    assert not repeated.is_error, repeated.output
    assert repeated.metadata["preflight"]["layout_plan"] == plan
    assert not list(tmp_path.rglob("*.png"))
