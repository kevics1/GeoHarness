"""A rejected detail zoom returns measured native evidence and safe alternatives."""

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import box

from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def test_detail_zoom_failure_exposes_measured_frames_and_suggestions(tmp_path):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f"QGIS unavailable: {exc}")
    source = tmp_path / "region.geojson"
    gpd.GeoDataFrame(geometry=[box(0, 0, 10000, 10000)], crs=3857).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="preflight", crs="EPSG:3857", layers=[{"file_path": str(source)}],
        layout={"legend": False, "legend_auto_fit": False,
                "detail_extent": {"xmin": 2000, "ymin": 2000, "xmax": 8000, "ymax": 8000},
                "detail_minimum_zoom": 10}), context)
    assert result.is_error, result.output
    facts = result.metadata["diagnostics"]
    assert facts["code"] == "detail_zoom_insufficient"
    main, detail = facts["frames"]["main_map"], facts["frames"]["detail_map"]
    assert facts["actual_zoom"] == pytest.approx(
        main["scale_denominator"] / detail["scale_denominator"])
    assert facts["actual_zoom"] < facts["required_zoom"] == 10
    assert main["crs"] == detail["crs"] == "EPSG:3857"
    assert main["frame_mm"]["width"] > 0
    assert len(detail["extent"]) == 4
    enlargement, crop, separate = facts["suggestions"]
    assert enlargement["detail_frame_mm"]["width"] > detail["frame_mm"]["width"]
    assert enlargement["detail_frame_mm"]["height"] > detail["frame_mm"]["height"]
    assert crop["parameter"] == "layout.detail_extent"
    assert crop["automatic_application"] is False
    assert "every required subject" in crop["required_check"]
    assert separate["strategy"] == "separate_detail_map"
    assert "detail_zoom_insufficient" in result.output
    assert not list(tmp_path.rglob("*.png"))
