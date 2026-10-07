"""Editable map delivery: real data, portable references, styles and redraws."""

from __future__ import annotations

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


def _polygons() -> gpd.GeoDataFrame:
    return gpd.GeoDataFrame(
        {"name": ["A", "B", "C", "D"], "value": [1.0, 2.0, 8.0, 10.0]},
        geometry=[box(i, 0, i + 0.8, 1) for i in range(4)],
        crs="EPSG:4326",
    )


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
