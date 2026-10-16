"""Map pages have their own furniture and rosters never change geographic coverage."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from pydantic import ValidationError
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def test_table_only_sources_cannot_be_used_as_map_subjects():
    layers = [{"file_path": "map.gpkg", "layer_key": "map"},
              {"file_path": "roster.gpkg", "layer_key": "roster", "role": "table_only"}]
    arguments = GeoQgisProjectInput(action="create", layers=layers,
        layout={"label_table": {"layer_index": "roster", "fields": ["id", "name"]}})
    assert arguments.layers[1].role == "table_only"
    for field in ("render_layers", "focus_layers", "label_layers"):
        with pytest.raises(ValidationError, match="table_only"):
            GeoQgisProjectInput(action="create", layers=layers,
                layout={"main_frame": {field: ["roster"]}})
    with pytest.raises(ValidationError, match="table_only"):
        GeoQgisProjectInput(action="create", layers=layers,
            layout={"main_frame": {"requirements": [{"layer_index": "roster"}]}})
    with pytest.raises(ValidationError, match="map layer"):
        GeoQgisProjectInput(action="create", layers=[layers[1]])


def test_table_only_schema_and_counts_remain_checked(tmp_path):
    source = tmp_path / "places.gpkg"
    gpd.GeoDataFrame({"id": [1], "name": ["名单"]},
        geometry=[Point(0, 0)], crs=3857).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path,
        metadata={"geoharness_config": GeoConfig()})
    args = GeoQgisProjectInput(action="preflight", crs="EPSG:3857", layers=[
        {"file_path": str(source), "layer_key": "map"},
        {"file_path": str(source), "layer_key": "roster", "role": "table_only",
         "expected_feature_count": 2}])
    with pytest.raises(ValueError, match="expected_feature_count"):
        GeoQgisProjectTool()._preflight(args, context)


def test_table_only_does_not_choose_or_require_the_map_projection(tmp_path):
    from geoharness.cartography.preflight import validate_frames

    mapped = gpd.GeoDataFrame(geometry=[Point(500000, 3000000)], crs=32647)
    roster = gpd.GeoDataFrame(geometry=[Point(9, 0)], crs=4326)
    result = validate_frames([{"gdf": roster, "role": "table_only"}, {"gdf": mapped}])
    assert result["display_crs"] == "EPSG:32647"
    assert result["layers"][0]["display_bounds"] is None
    assert max(result["map_extent"]) < 4000000


async def test_native_detail_page_is_complete_and_table_source_does_not_draw(tmp_path):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f"QGIS unavailable: {exc}")
    source, roster = tmp_path / "places.gpkg", tmp_path / "roster.gpkg"
    gpd.GeoDataFrame({"id": [1, 2], "name": ["西侧居民点", "东侧居民点"]},
        geometry=[Point(1000, 1000), Point(2000, 2000)], crs=3857).to_file(source)
    # The roster's distant geometry must neither expand the extent nor enter legends.
    gpd.GeoDataFrame({"id": [9.123, 7.123], "name": ["目标九", "目标七"]},
        geometry=[Point(1000000, 1000000), Point(1100000, 1100000)], crs=3857).to_file(roster)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    tool = GeoQgisProjectTool()
    created = await tool.execute(GeoQgisProjectInput(action="create", title="全域概览",
        crs="EPSG:3857", dpi=36, layers=[
            {"file_path": str(source), "layer_key": "places", "name": "居民点"},
            {"file_path": str(roster), "layer_key": "roster", "name": "仅供名录",
             "role": "table_only"}],
        layout={"detail_placement": "page", "detail_title": "火情核心区详图",
            "detail_extent": {"xmin": 800, "ymin": 800, "xmax": 2200, "ymax": 2200},
            "detail_frame": {"purpose": "分析用途说明不会替代可见标题"},
            "label_table": {"layer_index": "roster", "fields": ["id", "name"],
                            "keys": [7.123, 9.123], "precision": {"id": 1},
                            "placement": "pages", "columns": 1}}), context)
    assert not created.is_error, created.output
    manifest_path = Path(created.metadata["project_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    layout = manifest["layout"]
    completeness = layout["page_completeness"]
    assert completeness["valid"]
    detail = next(page for page in completeness["pages"] if page["page_index"] == 1)
    assert detail["title_text"] == "火情核心区详图"
    assert detail["map_frames"] == ["detail_map"]
    assert detail["legends"] and detail["scale_bars"]
    assert layout["bounds_check"]["valid"]
    for frame in manifest["native_facts"]["frames"].values():
        assert frame["render_layers"] == [0]
        assert max(frame["extent"]) < 10000
    for legend in layout["legends"].values():
        assert {layer["layer_index"] for layer in legend["layers"]} == {0}
    selection = layout["roster_selection"]
    assert set(selection["keys"]) == {7.123, 9.123}
    assert selection["membership_check"]["valid"]
    assert selection["membership_check"]["missing_keys"] == []
    assert selection["membership_check"]["unexpected_keys"] == []
    assert all((manifest_path.parent / preview["path"]).is_file()
               for preview in manifest["native_facts"]["page_previews"])
    from geoharness.cartography.quality import _review_images

    preview = next(p for p in manifest["native_facts"]["page_previews"] if p["page_index"] == 1)
    _, catalog = _review_images(manifest_path.parent / preview["path"], {
        "render_metadata": {"layout": layout, "native_facts": manifest["native_facts"],
                            "review_page_index": 1}}, config.map_review)
    assert any(roi["frame_id"] == "legend" for roi in catalog)
    assert any(roi["frame_id"] == "footer" for roi in catalog)
    exported = await tool.execute(GeoQgisProjectInput(action="export",
        project_path=created.metadata["project_file"], dpi=36), context)
    assert not exported.is_error, exported.output
    saved = json.loads(Path(exported.metadata["project_manifest"]).read_text(encoding="utf-8"))
    assert saved["layout"]["page_completeness"] == completeness


async def test_detail_legend_participates_in_native_auto_fit(tmp_path):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f"QGIS unavailable: {exc}")
    source = tmp_path / "places.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(1000, 1000)], crs=3857).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    created = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action="create", dpi=36,
        crs="EPSG:3857",
        layers=[{"file_path": str(source), "layer_key": "main", "name": "A"},
                {"file_path": str(source), "layer_key": "detail",
                 "name": "Detail legend must fit this considerably longer layer name"}],
        layout={"detail_placement": "page", "main_frame": {"render_layers": [0]},
                "detail_frame": {"render_layers": [1]},
                "detail_extent": {"xmin": 800, "ymin": 800, "xmax": 1200, "ymax": 1200}}),
        context)
    assert not created.is_error, created.output
    manifest = json.loads(Path(created.metadata["project_manifest"]).read_text(encoding="utf-8"))
    assert manifest["layout"]["bounds_check"]["valid"]
    assert manifest["layout"]["page_completeness"]["valid"]
