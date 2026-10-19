"""Regression cases from the test02 mapping session."""

import json

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from openharness.tools.base import ToolExecutionContext
from PIL import Image
from rasterio.transform import from_origin
from shapely.geometry import box

from geoharness.cartography.workflow import reserve_map_render
from geoharness.config.settings import GeoConfig
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool
from geoharness.tools.geo_qgis_project import QgisVectorStyle


def test_composition_warns_before_rendering_dense_cross_layer_labels():
    from shapely.geometry import Point

    from geoharness.cartography.composition import composition_risks

    frames = [{"gdf": gpd.GeoDataFrame({"label": ["A", "B"]},
              geometry=[Point(50, 50), Point(50.1, 50)], crs=3857), "label_field": "label"}]
    risk = composition_risks(frames, report={"map_extent": [0, 0, 100, 100],
                             "display_crs": "EPSG:3857"}, layout={})
    assert risk["warnings"][0]["code"] == "dense_point_labels"
    assert risk["warnings"][0]["close_anchor_pairs"] == 1
    # Font-size estimates do not prove rendered label or symbol collisions.
    assert risk["hard_conflict_count"] == 0
    assert risk["scope"].endswith("not a visual overlap test")

def test_unlabeled_point_symbols_report_density_without_forbidding_them():
    from shapely.geometry import Point

    from geoharness.cartography.composition import composition_risks

    frame = gpd.GeoDataFrame({"id": [1, 2]}, geometry=[Point(1284, 875),
                                                       Point(1284.00001, 875)], crs=3857)
    risk = composition_risks([{"gdf": frame, "style": {"size_mm": 5.4},
                               "layer_index": 7}],
        report={"map_extent": [1283.9, 874.9, 1284.1, 875.1],
                "display_crs": "EPSG:3857"}, layout={})
    assert risk["symbolized_point_count"] == 2
    assert risk["hard_conflict_count"] == 0
    assert risk["warnings"][0]["minimum_anchor_distance_mm"] < 5.4


def test_previous_outputs_use_persisted_actual_paths_and_reject_fabricated_inputs(tmp_path):
    from geoharness.data.task_context import recent_outputs

    job = tmp_path / ".geoharness/runs/prior-job-20261009"
    job.mkdir(parents=True)
    actual = job / "union.geojson"
    actual.write_text("{}", encoding="utf-8")
    invented = job.parent / "prior-job-20261015/union.geojson"
    (job / "manifest.json").write_text(json.dumps({"run_dir": str(job), "exit_code": 0,
        "files": [{"path": str(actual)}, {"path": str(invented)}]}), encoding="utf-8")
    records = recent_outputs(tmp_path, GeoConfig())
    assert [r["path"] for r in records] == [str(actual)]


def test_dynamic_context_bounds_whole_records_including_long_artifact_paths(tmp_path, monkeypatch):
    from geoharness.data import profiles, task_context

    monkeypatch.setattr(profiles, "current_profiles", lambda metadata: {
        "dem": {"name": "dem", "kind": "raster", "path": "/actual/dem.tif", "crs": "EPSG:32647"},
    })
    monkeypatch.setattr(task_context, "recent_outputs", lambda *args, **kwargs: [
        {"path": "/actual/" + "a" * 12000 + "/result.geojson"} for _ in range(16)
    ])
    monkeypatch.setattr(task_context, "recent_qgis_results", lambda *args, **kwargs: [
        {"result_file": "/actual/" + "b" * 12000 + "/result.json"} for _ in range(4)
    ])
    context = task_context.model_data_context({}, cwd=tmp_path, config=GeoConfig())
    assert len(context) < 20000
    assert "EPSG:32647" in context
    document = json.loads(context.split("\n")[-1])
    assert all(item["path"].endswith("/result.geojson") for item in document["existing_outputs"])


def test_map_generation_remains_unlimited_after_many_versions():
    config = GeoConfig()
    metadata = {}
    for _ in range(100):
        assert reserve_map_render("test02", metadata, config) is None
    assert metadata["map_workflow_state"]["maps"]["test02"]["renders"] == 100


def test_reversed_graduated_ranges_are_canonicalized_without_changing_colors():
    style = QgisVectorStyle(renderer="graduated", field="area_m2", ranges=[
        {"lower": 10, "upper": 20, "color": "red", "label": "large"},
        {"lower": 0, "upper": 10, "color": "orange", "label": "small"},
    ])
    assert [(r.lower, r.upper, r.color, r.label) for r in style.ranges] == [
        (0, 10, "orange", "small"), (10, 20, "red", "large"),
    ]
    with pytest.raises(ValueError, match="overlapping"):
        QgisVectorStyle(renderer="graduated", field="area", ranges=[
            {"lower": 0, "upper": 11, "color": "orange"},
            {"lower": 10, "upper": 20, "color": "red"},
        ])


async def test_qgis_returned_preview_and_project_metadata_are_exact_read_only(tmp_path):
    job = tmp_path / ".geoharness/qgis/project_create-test"
    job.mkdir(parents=True)
    image = job / "preview.png"
    Image.new("RGB", (10, 12), "white").save(image)
    project = job / "project.json"
    project.write_text('{"layout": "Map"}', encoding="utf-8")
    (job / "manifest.json").write_text(json.dumps({"run_dir": str(job)}), encoding="utf-8")
    (job / "result.json").write_text(json.dumps({
        "status": "succeeded", "result": {"preview_path": str(image),
        "project_manifest": str(project)},
    }), encoding="utf-8")
    code = f"""
from PIL import Image
import json
assert Image.open({str(image)!r}).size == (10, 12)
assert json.loads(open({str(project)!r}).read())['layout'] == 'Map'
for target in [{str(image)!r}, {str(project)!r}]:
    try: open(target, 'wb').write(b'changed')
    except PermissionError: pass
    else: raise AssertionError('output write granted')
print('returned preview and metadata readable')
"""
    result = await GeoPythonTool().execute(GeoPythonInput(code=code), ToolExecutionContext(
        cwd=tmp_path, metadata={"geoharness_config": GeoConfig()},
    ))
    assert not result.is_error, result.output


async def test_raster_mask_transforms_crs_bearing_geometry_before_clipping(tmp_path):
    raster = tmp_path / "dem.tif"
    with rasterio.open(raster, "w", driver="GTiff", height=10, width=10, count=1,
                       dtype="uint8", crs="EPSG:32647",
                       transform=from_origin(800000, 3100000, 30, 30)) as dataset:
        dataset.write(np.ones((1, 10, 10), dtype="uint8"))
    geometry = gpd.GeoDataFrame(geometry=[box(800030, 3099760, 800240, 3099970)],
                                crs="EPSG:32647").to_crs(32648)
    vector = tmp_path / "clip.geojson"
    geometry.to_file(vector, driver="GeoJSON")
    code = f"""
import geopandas as gpd
g = gpd.read_file({str(vector)!r})
assert g.crs.to_epsg() == 32648
pixels, transform = mask_raster({str(raster)!r}, g, crop=True)
assert pixels.size > 0 and pixels.max() == 1
try: mask_raster({str(raster)!r}, [g.geometry.iloc[0]], crop=True)
except ValueError as exc: assert 'CRS' in str(exc)
else: raise AssertionError('undeclared geometry CRS accepted')
print('mixed CRS mask succeeded')
"""
    result = await GeoPythonTool().execute(GeoPythonInput(code=code), ToolExecutionContext(
        cwd=tmp_path, metadata={"geoharness_config": GeoConfig()},
    ))
    assert not result.is_error, result.output
