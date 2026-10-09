"""Editable map delivery: real data, portable references, styles and redraws."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from PIL import Image
from rasterio.transform import from_bounds
from shapely.geometry import Point, box

from geoharness.cartography.project import export_map_project, redraw_map_project
from geoharness.cartography.renderer import render_layers, render_map
from geoharness.config.settings import MapLayout


async def test_project_input_and_output_directory_are_independent(tmp_path):
    from openharness.tools.base import ToolExecutionContext

    from geoharness.config.settings import GeoConfig
    from geoharness.tools.geo_cartography import GeoCartographyInput, GeoCartographyTool

    source = tmp_path / "source.geojson"
    _polygons().to_file(source, driver="GeoJSON")
    preview = tmp_path / "original.png"
    Image.new("RGB", (64, 64), "white").save(preview)
    before = hashlib.sha256(preview.read_bytes()).hexdigest()
    context = ToolExecutionContext(
        cwd=tmp_path, metadata={"geoharness_config": GeoConfig()},
    )
    result = await GeoCartographyTool().execute(
        GeoCartographyInput(
            action="project", map_path=str(preview),
            project_output_dir="exports/editable",
            layers=json.dumps([{"file_path": str(source), "label": "Actual layers"}]),
        ), context,
    )
    assert not result.is_error, result.output
    assert Path(result.metadata["project_dir"]) == tmp_path / "exports/editable"
    assert Path(result.metadata["qgis_project"]).is_file()
    assert result.metadata["output_path"] == str(preview)
    assert hashlib.sha256(preview.read_bytes()).hexdigest() == before

    rejected = await GeoCartographyTool().execute(
        GeoCartographyInput(
            action="project", map_path=str(preview),
            project_output_dir=str(tmp_path.parent / "outside-project"),
            layers=json.dumps([{"file_path": str(source)}]),
        ), context,
    )
    assert rejected.is_error
    assert "inside the workspace" in rejected.output
    empty_target = tmp_path / "exports/existing-empty"
    empty_target.mkdir()
    identity = empty_target.stat().st_ino
    refused = await GeoCartographyTool().execute(
        GeoCartographyInput(
            action="project", map_path=str(preview),
            project_output_dir=str(empty_target),
            layers=json.dumps([{"file_path": str(source)}]),
        ), context,
    )
    assert refused.is_error and "already exists" in refused.output
    assert empty_target.stat().st_ino == identity
    assert not list(empty_target.iterdir())


def _polygons() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["A", "B", "C", "D"], "value": [1.0, 2.0, 8.0, 10.0]},
        geometry=[box(i, 0, i + 0.8, 1) for i in range(4)],
        crs="EPSG:4326",
    )


def test_single_redraw_retains_explicit_extent(tmp_path, monkeypatch):
    gdf = _polygons()
    bounds = [-1, -1, 8, 5]
    output = tmp_path / "original.png"
    render_map(gdf, column="value", output_path=output, extent=bounds)
    package = export_map_project(tmp_path / "single-extent", layers=[{"gdf": gdf, "label": "A"}],
                                 output_path=output, extent=bounds,
                                 render_kwargs={"mode": "single", "column": "value"})
    original = render_map
    called = {}

    def capture(*args, **kwargs):
        called.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr("geoharness.cartography.renderer.render_map", capture)
    redraw_map_project(package["project_file"], output_path=tmp_path / "single-redraw.png")
    assert called["extent"] == bounds


def test_html_project_extent_matches_basemap_crs_and_redraw(tmp_path, monkeypatch):
    from pyproj import CRS, Transformer

    extent = [99.9, 29.9, 100.2, 30.2]
    raster_bounds = Transformer.from_crs(4326, 32647, always_xy=True).transform_bounds(*extent)
    raster = tmp_path / "utm.tif"
    with rasterio.open(raster, "w", driver="GTiff", count=1, width=20, height=20,
                       dtype="uint8", crs="EPSG:32647",
                       transform=from_bounds(*raster_bounds, 20, 20)) as dataset:
        dataset.write(np.ones((1, 20, 20), dtype="uint8"))
    gdf = gpd.GeoDataFrame(geometry=[Point(100, 30), Point(100.1, 30.1)], crs=4326)
    original_map = tmp_path / "original.html"
    render_layers([{"gdf": gdf, "label": "Points"}], output_path=original_map,
                  extent=extent, extent_crs="EPSG:4326", basemap_raster=str(raster))
    package = export_map_project(tmp_path / "html-project",
                                 layers=[{"gdf": gdf, "label": "Points"}],
                                 output_path=original_map, extent=extent, extent_crs="EPSG:4326",
                                 render_kwargs={"mode": "layers", "basemap_raster": str(raster)})
    manifest = json.loads(Path(package["project_file"]).read_text(encoding="utf-8"))
    assert CRS.from_user_input(manifest["project_crs"]) == CRS.from_epsg(32647)
    np.testing.assert_allclose(manifest["extent"], raster_bounds)
    original = render_layers
    called = {}

    def capture(*args, **kwargs):
        called.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr("geoharness.cartography.renderer.render_layers", capture)
    result = redraw_map_project(package["project_file"], output_path=tmp_path / "redraw.html")
    assert result.is_file()
    assert CRS.from_user_input(called["extent_crs"]) == CRS.from_epsg(32647)
    np.testing.assert_allclose(called["extent"], raster_bounds)


def _single(tmp_path: Path, method: str = "graduated") -> dict[str, str]:
    gdf = _polygons()
    kwargs = {
        "mode": "single",
        "column": "value",
        "render_method": method,
        "color_scheme": "YlOrRd",
        "n_classes": 2,
        "title": "Editable map",
    }
    output = tmp_path / "map.png"
    render_map(gdf, output_path=output, **{k: v for k, v in kwargs.items() if k != "mode"})
    return export_map_project(
        tmp_path / "project",
        layers=[{"gdf": gdf, "label": "Analysis"}],
        output_path=output,
        render_kwargs=kwargs,
    )


def test_empty_impact_selection_remains_editable_without_changing_results(tmp_path):
    background = _polygons()
    empty = gpd.GeoDataFrame({"affected": []}, geometry=[], crs=4326)
    output = tmp_path / "map.png"
    Image.new("RGB", (64, 64), "white").save(output)
    project = export_map_project(
        tmp_path / "empty-project",
        layers=[
            {"gdf": background, "label": "Background"},
            {"gdf": empty, "label": "Affected: 0", "color": "#ff0000"},
        ],
        output_path=output,
        render_kwargs={"mode": "layers", "title": "Zero affected assets"},
    )
    manifest = json.loads(Path(project["project_file"]).read_text(encoding="utf-8"))
    assert manifest["layers"][1]["bounds"] == []
    retained = gpd.read_file(Path(project["project_dir"]) / manifest["layers"][1]["file"])
    assert retained.empty and retained.crs.to_epsg() == 4326
    assert np.isfinite(manifest["extent"]).all()


def test_qgis_data_crs_classification_and_editable_layout(tmp_path: Path) -> None:
    delivery = _single(tmp_path)
    root = ET.parse(delivery["qgis_project"]).getroot()
    assert root.findtext("projectCrs/spatialrefsys/authid") == "EPSG:4326"
    assert root.findtext("properties/Paths/Absolute") == "false"
    node = root.find("projectlayers/maplayer")
    assert node is not None
    reference = node.findtext("datasource")
    assert reference == "./data/layer_1.gpkg"
    data = gpd.read_file(Path(delivery["project_dir"]) / reference, layer="features")
    assert list(data["value"]) == [1, 2, 8, 10]
    assert data.crs.to_epsg() == 4326
    renderer = node.find("renderer-v2")
    assert renderer is not None and renderer.get("type") == "graduatedSymbol"
    assert renderer.get("attr") == "value"
    ranges = renderer.findall("ranges/range")
    assert [(float(r.get("lower")), float(r.get("upper"))) for r in ranges] == [(1, 2), (2, 10)]
    symbols = renderer.findall("symbols/symbol")
    assert len(symbols) == 2
    items = root.findall("Layouts/Layout/LayoutItem")
    by_name = {item.get("id"): item for item in items}
    assert {"Map", "Title", "Legend", "Scale bar", "North"} <= by_name.keys()
    assert by_name["Title"].get("labelText") == "Editable map"
    assert by_name["Legend"].get("map_uuid") == by_name["Map"].get("uuid")
    assert by_name["Scale bar"].get("mapUuid") == by_name["Map"].get("uuid")
    frame = by_name["Map"]
    width, height = map(float, frame.get("size").split(",")[:2])
    extent = frame.find("Extent")
    ratio = (float(extent.get("xmax")) - float(extent.get("xmin"))) / (
        float(extent.get("ymax")) - float(extent.get("ymin"))
    )
    assert ratio == pytest.approx(width / height)
    assert all(item.get("positionLock") == "false" for item in items)
    assert not root.findall(".//LayoutItem[@type='65640']")  # No preview-image layout.


def test_categorized_styles_use_original_editable_attribute(tmp_path: Path) -> None:
    delivery = _single(tmp_path, "categorized")
    renderer = ET.parse(delivery["qgis_project"]).find(".//renderer-v2")
    assert renderer is not None and renderer.get("attr") == "value"
    assert renderer.get("type") == "categorizedSymbol"
    assert [category.get("type") for category in renderer.findall("categories/category")] == [
        "double",
        "double",
        "double",
        "double",
        "NULL",
    ]


def test_redraw_after_move_and_json_layout_edit(tmp_path: Path) -> None:
    delivery = _single(tmp_path)
    relocated = tmp_path / "relocated"
    shutil.move(delivery["project_dir"], relocated)
    (tmp_path / "map.png").unlink()
    project_file = relocated / "project.json"
    project = json.loads(project_file.read_text(encoding="utf-8"))
    project["render_kwargs"]["title"] = "User adjusted title"
    project["layout"].update(width=5, height=4, dpi=72, polygon_alpha=0.3)
    project_file.write_text(json.dumps(project), encoding="utf-8")
    redrawn = redraw_map_project(project_file)
    assert redrawn == relocated / "redrawn.png"
    with Image.open(redrawn) as image:
        assert 100 < image.width < 450
        assert 100 < image.height < 400


def test_actual_frame_is_copied_without_source_credentials(tmp_path: Path) -> None:
    gdf = _polygons().iloc[:2].copy()
    output = tmp_path / "source-render.png"
    render_map(gdf, output_path=output)
    delivery = export_map_project(
        tmp_path / "project",
        layers=[
            {"gdf": gdf, "label": "Snapshot", "source_name": "postgresql://user:secret@server/db"}
        ],
        output_path=output,
        render_kwargs={
            "mode": "single",
            "source_type": "postgis",
            "postgis_dsn": "postgresql://user:secret@server/db",
            "api_key": "private-key",
            "catalog": object(),
        },
    )
    for name in ("project_file", "qgis_project", "redraw_script", "project_readme"):
        content = Path(delivery[name]).read_text(encoding="utf-8")
        assert "secret" not in content and "private-key" not in content
        assert str(tmp_path) not in content
    copied = gpd.read_file(Path(delivery["project_dir"]) / "data/layer_1.gpkg")
    assert len(copied) == 2
    assert copied.geometry.equals(gdf.geometry.reset_index(drop=True))


def test_composition_order_and_original_crs_are_preserved(tmp_path: Path) -> None:
    base = _polygons().to_crs(3857)
    point = gpd.GeoDataFrame({"name": ["point"]}, geometry=[Point(1, 0.5)], crs=4326)
    layers = [
        {"gdf": base, "label": "Base", "color": "#115533"},
        {"gdf": point, "label": "Top", "color": "#ff0000"},
    ]
    output = tmp_path / "composed.png"
    render_layers(layers, output_path=output)
    delivery = export_map_project(
        tmp_path / "project", layers=layers, output_path=output, render_kwargs={"mode": "layers"}
    )
    root = ET.parse(delivery["qgis_project"]).getroot()
    assert [item.get("name") for item in root.findall("layer-tree-group/layer-tree-layer")] == [
        "Top",
        "Base",
    ]
    assert root.findtext("projectCrs/spatialrefsys/authid") == "EPSG:3857"
    nodes = root.findall("projectlayers/maplayer")
    assert [node.findtext("srs/spatialrefsys/authid") for node in nodes] == [
        "EPSG:3857",
        "EPSG:4326",
    ]
    assert redraw_map_project(delivery["project_file"]).exists()


def test_vrt_backdrop_becomes_standalone_tiff_and_redraws(tmp_path: Path) -> None:
    source = tmp_path / "original.tif"
    values = np.arange(100, dtype="float32").reshape(10, 10)
    with rasterio.open(
        source,
        "w",
        driver="GTiff",
        width=10,
        height=10,
        count=1,
        dtype="float32",
        crs=4326,
        transform=from_bounds(0, 0, 4, 1, 10, 10),
        nodata=-9999,
    ) as ds:
        ds.write(values, 1)
        ds.update_tags(source="postgresql://user:secret@server/db")
    from rasterio.shutil import copy

    vrt = tmp_path / "input.vrt"
    copy(source, vrt, driver="VRT")
    layers = [{"gdf": _polygons(), "label": "Polygons"}]
    output = tmp_path / "with-raster.png"
    render_layers(layers, output_path=output, basemap_raster=str(vrt))
    delivery = export_map_project(
        tmp_path / "project",
        layers=layers,
        output_path=output,
        render_kwargs={"mode": "layers", "basemap_raster": str(vrt)},
    )
    copied = Path(delivery["project_dir"]) / "data/basemap.tif"
    with rasterio.open(copied) as ds:
        np.testing.assert_array_equal(ds.read(1), values)
        assert "source" not in ds.tags()
        assert ds.crs.to_epsg() == 4326
        assert ds.nodata == -9999
    source.unlink()
    vrt.unlink()
    assert redraw_map_project(delivery["project_file"]).exists()
    root = ET.parse(delivery["qgis_project"]).getroot()
    raster = root.find("projectlayers/maplayer[@type='raster']")
    assert raster is not None
    assert raster.findtext("provider") == "gdal"
    assert len(raster.findall("pipe/rasterrenderer/rastershader/colorrampshader/item")) == 17


def test_generated_redraw_entry_point_runs_from_other_directory(tmp_path: Path) -> None:
    delivery = _single(tmp_path)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    result = subprocess.run(
        [sys.executable, delivery["redraw_script"]],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (Path(delivery["project_dir"]) / "redrawn.png").is_file()


def test_redraw_rejects_external_data_reference(tmp_path: Path) -> None:
    delivery = _single(tmp_path)
    project_file = Path(delivery["project_file"])
    project = json.loads(project_file.read_text(encoding="utf-8"))
    project["layers"][0]["file"] = "../outside.gpkg"
    project_file.write_text(json.dumps(project), encoding="utf-8")
    with pytest.raises(ValueError, match="escapes"):
        redraw_map_project(project_file)


def test_does_not_overwrite_user_edited_project(tmp_path: Path) -> None:
    delivery = _single(tmp_path)
    project_file = Path(delivery["project_file"])
    before = project_file.read_bytes()
    with pytest.raises(FileExistsError, match="already exists"):
        export_map_project(
            delivery["project_dir"],
            layers=[{"gdf": _polygons()}],
            output_path=tmp_path / "map.png",
            render_kwargs={"mode": "single"},
        )
    assert project_file.read_bytes() == before


def test_missing_crs_is_rejected_before_writing(tmp_path: Path) -> None:
    frame = _polygons().set_crs(None, allow_override=True)
    with pytest.raises(ValueError, match="declared CRS"):
        export_map_project(
            tmp_path / "project",
            layers=[{"gdf": frame}],
            output_path=tmp_path / "map.png",
            render_kwargs={"mode": "single"},
        )
    assert not (tmp_path / "project").exists()


def test_flow_ring_snapshot_can_redraw_and_has_editable_line_styles(tmp_path: Path) -> None:
    delivery = _single(tmp_path, "flow")
    root = ET.parse(delivery["qgis_project"]).getroot()
    assert root.find("projectlayers/maplayer").get("geometry") == "Line"
    data = gpd.read_file(Path(delivery["project_dir"]) / "data/layer_1.gpkg")
    assert all(kind == "MultiLineString" for kind in data.geom_type)
    assert redraw_map_project(delivery["project_file"]).exists()


def test_explicit_layout_is_preserved(tmp_path: Path) -> None:
    output = tmp_path / "map.png"
    layout = MapLayout(width=6, height=5, dpi=100, legend_outside=True, polygon_alpha=0.4)
    render_map(_polygons(), output_path=output, layout=layout)
    delivery = export_map_project(
        tmp_path / "project",
        layers=[{"gdf": _polygons()}],
        output_path=output,
        render_kwargs={"mode": "single"},
        layout=layout,
    )
    project = json.loads(Path(delivery["project_file"]).read_text(encoding="utf-8"))
    assert project["layout"] == layout.model_dump()
    root = ET.parse(delivery["qgis_project"]).getroot()
    assert root.find("Layouts/Layout").get("printResolution") == "100"


def test_risk_categories_match_native_pixels_qgis_and_project_redraw(tmp_path: Path) -> None:
    points = gpd.GeoDataFrame(
        {"RiskTier": ["High", "Medium", "Low"]},
        geometry=[Point(0, 0), Point(1, 1), Point(2, 0)], crs=3857,
    )
    colors = ["#d73027", "#fee08b", "#1a9850"]
    layers = [{
        "gdf": points, "label": "Risk sites", "field_name": "RiskTier",
        "style": {"categories": ["High", "Medium", "Low"], "colors": colors},
    }]
    image = tmp_path / "actual-risk.png"
    extent = [-0.5, -0.5, 2.5, 1.5]
    render_layers(layers, output_path=image, extent=extent)
    delivery = export_map_project(
        tmp_path / "risk-project", layers=layers, output_path=image,
        render_kwargs={"mode": "layers", "extent": extent},
    )
    manifest = json.loads(Path(delivery["project_file"]).read_text(encoding="utf-8"))
    assert manifest["preview_sha256"] == hashlib.sha256(image.read_bytes()).hexdigest()
    assert manifest["extent"] == extent
    assert manifest["extent_locked"] is True
    style = manifest["layers"][0]["style"]
    assert style["column"] == "RiskTier"
    assert [item["color"] for item in style["categories"][:3]] == colors
    renderer = ET.parse(delivery["qgis_project"]).find(".//renderer-v2")
    assert renderer.get("type") == "categorizedSymbol" and renderer.get("attr") == "RiskTier"
    assert [category.get("value") for category in renderer.findall("categories/category")][:3] == [
        "High", "Medium", "Low",
    ]
    qgis_colors = [symbol.find("layer/prop[@k='color']").get("v")
                   for symbol in renderer.findall("symbols/symbol")][:3]
    assert qgis_colors == ["215,48,39,255", "254,224,139,255", "26,152,80,255"]
    frame = ET.parse(delivery["qgis_project"]).find(".//LayoutItem[@id='Map']")
    qgis_extent = frame.find("Extent")
    assert [float(qgis_extent.get(axis)) for axis in ("xmin", "ymin", "xmax", "ymax")] == extent
    frame_width, frame_height = map(float, frame.get("size").split(",")[:2])
    assert frame_width / frame_height == pytest.approx(3 / 2)
    redrawn = redraw_map_project(delivery["project_file"])
    for path in (image, redrawn):
        with Image.open(path) as rendered:
            pixels = {color for _count, color in rendered.convert("RGB").getcolors(
                maxcolors=rendered.width * rendered.height
            )}
        assert {(215, 48, 39), (254, 224, 139), (26, 152, 80)} <= pixels


@pytest.mark.parametrize("style, message", [
    ({"field_name": "Missing"}, "does not exist"),
    ({"field_name": "value", "render_method": "magic"}, "Unsupported"),
    ({"field_name": "value", "categories": [1], "colors": ["red"]}, "absent"),
    ({"field_name": "value", "categories": [1, 2], "colors": ["red"]}, "one color"),
    ({"style": {"unsupported": True}}, "Unsupported"),
    ({"field_name": "value", "categories": [
        {"value": 1, "color": "red"}, {"value": None, "color": "blue"},
    ], "default_color": "yellow"}, "default_color must match"),
])
def test_multilayer_rejects_ignored_or_incomplete_styles(tmp_path, style, message):
    with pytest.raises(ValueError, match=message):
        render_layers(
            [{"gdf": _polygons(), "label": "Risk", **style}], output_path=tmp_path / "bad.png"
        )
    assert not (tmp_path / "bad.png").exists()


def test_explicit_context_extent_excludes_outlying_features(tmp_path, monkeypatch):
    from matplotlib.axes import Axes

    points = gpd.GeoDataFrame(geometry=[Point(0, 0), Point(1000, 1000)], crs=3857)
    seen = []
    original = Axes.set_xlim

    def record(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        seen.append(result)
        return result

    monkeypatch.setattr(Axes, "set_xlim", record)
    render_layers(
        [{"gdf": points, "label": "Context"}], output_path=tmp_path / "context.png",
        extent=[-1, -1, 1, 1],
    )
    assert seen[-1] == (-1, 1)


def test_single_risk_category_legend_matches_actual_points(tmp_path, monkeypatch):
    from matplotlib.axes import Axes

    points = gpd.GeoDataFrame(
        {"RiskTier": ["High", "High"]}, geometry=[Point(0, 0), Point(1, 1)], crs=3857,
    )
    recorded = []
    original = Axes.legend

    def inspect(self, *args, **kwargs):
        recorded.extend(kwargs.get("handles", []))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Axes, "legend", inspect)
    render_layers(
        [{"gdf": points, "label": "Risk sites", "field_name": "RiskTier"}],
        output_path=tmp_path / "single-risk.png",
    )
    assert len(recorded) == 1
    assert recorded[0].get_markerfacecolor() == "#e41a1c"
    assert recorded[0].get_label() == "Risk sites: High"


def test_used_default_category_is_in_native_legend(tmp_path, monkeypatch):
    from matplotlib.axes import Axes

    points = gpd.GeoDataFrame(
        {"RiskTier": ["High", "Medium"]}, geometry=[Point(0, 0), Point(1, 1)], crs=3857,
    )
    recorded = []
    original = Axes.legend

    def inspect(self, *args, **kwargs):
        recorded.extend(kwargs.get("handles", []))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Axes, "legend", inspect)
    render_layers([{
        "gdf": points, "label": "Risk sites", "field_name": "RiskTier",
        "categories": ["High"], "colors": ["red"], "default_color": "yellow",
    }], output_path=tmp_path / "fallback-risk.png")
    assert [handle.get_markerfacecolor() for handle in recorded] == ["#ff0000", "#ffff00"]
    assert recorded[1].get_label() == "Risk sites: Other / NULL"


def test_legacy_uniform_layer_project_still_redraws(tmp_path):
    image = tmp_path / "legacy.png"
    Image.new("RGB", (64, 64), "white").save(image)
    delivery = export_map_project(
        tmp_path / "legacy", layers=[{"gdf": _polygons(), "label": "Context"}],
        output_path=image, render_kwargs={"mode": "layers"},
    )
    manifest_file = Path(delivery["project_file"])
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest.pop("preview_sha256")
    manifest.pop("extent_locked")
    manifest["layers"][0]["options"] = {
        "color": "", "column": "", "render_method": "categorized",
        "color_scheme": "Set1", "n_classes": 5,
    }
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    assert redraw_map_project(manifest_file).is_file()
