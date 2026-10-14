"""Frozen native plans bind real resources without importing PyQGIS in the agent."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from geoharness.config.settings import QgisConfig
from geoharness.qgis.discovery import child_environment, discover_qgis
from geoharness.qgis.project_worker import _json_digest, _layout_input_digest
from geoharness.qgis.runner import execute_qgis


def test_layout_binding_is_independent_of_temporary_containers_and_outputs():
    payload = {"layers": [{"file_path": "random/source.gpkg", "layer_name": "frozen_1",
                           "name": "subject", "style": {"size_mm": 3}}],
               "_layout_sources": [{"source": "input.geojson", "kind": "vector",
                                    "data_sha256": "a" * 64}],
               "_explicit_map_extent": False,
               "_extent_policy": {"minimum_span_projected": 1000, "extent_padding": 0.05},
               "map_extent": [0, 0, 1000, 1000], "crs": "EPSG:3857", "dpi": 150,
               "title": "地图", "layout": {"width_mm": 297, "font_file": "font.ttc"}}
    relocated = copy.deepcopy(payload)
    relocated["layers"][0].update(file_path="another/snapshot.gpkg", layer_name="frozen_2")
    relocated.update(action="project_create", map_id="map-task", output_path="new.pdf",
                     output_format="pdf", project_format="qgs")
    relocated["layout"]["layout_plan"] = {"input_digest": "irrelevant"}
    assert _layout_input_digest(relocated) == _layout_input_digest(payload)
    assert "qgis.core" not in sys.modules
    for key, value in (("title", "改变标题"), ("dpi", 300), ("_explicit_map_extent", True)):
        changed = {**payload, key: value}
        assert _layout_input_digest(changed) != _layout_input_digest(payload)
    changed = copy.deepcopy(payload)
    changed["layers"][0]["style"]["size_mm"] = 4
    assert _layout_input_digest(changed) != _layout_input_digest(payload)
    changed = copy.deepcopy(payload)
    changed["_layout_sources"][0]["data_sha256"] = "b" * 64
    assert _layout_input_digest(changed) != _layout_input_digest(payload)


def _native_case(tmp_path):
    import geopandas as gpd
    from shapely.geometry import box

    settings = QgisConfig(outputs_dir=str(tmp_path / "native-output"))
    try:
        discover_qgis(settings, cwd=tmp_path)
    except (ValueError, RuntimeError, OSError) as exc:
        pytest.skip(f"An independent QGIS installation is required: {exc}")
    source = tmp_path / "source.geojson"
    gpd.GeoDataFrame({"name": ["subject"]},
        geometry=[box(1000, 1000, 2000, 2000)], crs=3857).to_file(source)
    return settings, {"crs": "EPSG:3857", "title": "原生计划校验", "dpi": 72,
                      "layers": [{"file_path": str(source), "name": "subject"}],
                      "map_extent": [900, 900, 2100, 2100],
                      "layout": {"legend": False, "scale_bar": False}}


async def test_runtime_and_plan_digest_are_checked_before_render(tmp_path):
    settings, payload = _native_case(tmp_path)
    preflight = await execute_qgis("project_preflight", payload, cwd=tmp_path,
                                  settings=settings, read_roots=[tmp_path])
    assert preflight["status"] == "succeeded", preflight.get("error_summary")
    original = preflight["result"]["layout_plan"]
    for resign, expected in ((False, "digest is invalid"), (True, "runtime changed")):
        plan = copy.deepcopy(original)
        plan["runtime_identity"]["qgis"] = "changed runtime"
        if resign:
            plan["plan_digest"] = _json_digest({key: value for key, value in plan.items()
                                                 if key != "plan_digest"})
        rejected = await execute_qgis("project_create",
            {**payload, "layout": {**payload["layout"], "layout_plan": plan}},
            cwd=tmp_path, settings=settings, read_roots=[tmp_path])
        assert rejected["status"] == "failed", rejected
        assert expected in rejected["error_summary"]
        assert not list(Path(rejected["run_dir"]).rglob("*.png"))
        assert not list(Path(rejected["run_dir"]).rglob("*.qgz"))


async def test_replaced_font_resource_invalidates_plan_with_same_font_path(tmp_path):
    settings, payload = _native_case(tmp_path)
    font_root = Path(os.environ.get("WINDIR", os.environ.get("SystemRoot", ""))) / "Fonts"
    fonts = [font_root / name for name in ("msyh.ttc", "simsun.ttc")]
    if not all(path.is_file() for path in fonts):
        pytest.skip("Two installed static CJK fonts are required for the resource fixture")
    configured = tmp_path / "selected-font.ttc"
    shutil.copyfile(fonts[0], configured)
    payload["layout"]["font_file"] = str(configured)
    preflight = await execute_qgis("project_preflight", payload, cwd=tmp_path,
                                  settings=settings, read_roots=[tmp_path])
    assert preflight["status"] == "succeeded", preflight.get("error_summary")
    plan = preflight["result"]["layout_plan"]
    assert plan["font_identities"]["layout"]["resource_sha256"]
    shutil.copyfile(fonts[1], configured)
    with_plan = {**payload, "layout": {**payload["layout"], "layout_plan": plan}}
    assert _layout_input_digest(with_plan) == plan["input_digest"]
    rejected = await execute_qgis("project_create", with_plan, cwd=tmp_path,
                                  settings=settings, read_roots=[tmp_path])
    assert rejected["status"] == "failed", rejected
    assert "font resource or native face changed" in rejected["error_summary"]
    assert not list(Path(rejected["run_dir"]).rglob("*.png"))
    assert not list(Path(rejected["run_dir"]).rglob("*.qgz"))


async def test_same_bbox_attribute_change_invalidates_direct_native_plan(tmp_path):
    settings, payload = _native_case(tmp_path)
    preflight = await execute_qgis("project_preflight", payload, cwd=tmp_path,
                                  settings=settings, read_roots=[tmp_path])
    assert preflight["status"] == "succeeded", preflight.get("error_summary")
    plan = preflight["result"]["layout_plan"]
    source = Path(payload["layers"][0]["file_path"])
    content = json.loads(source.read_text(encoding="utf-8"))
    content["features"][0]["properties"]["name"] = "changed attribute, unchanged geometry"
    source.write_text(json.dumps(content), encoding="utf-8")
    with_plan = {**payload, "layout": {**payload["layout"], "layout_plan": plan}}
    assert _layout_input_digest(with_plan) == plan["input_digest"]
    rejected = await execute_qgis("project_create", with_plan, cwd=tmp_path,
                                  settings=settings, read_roots=[tmp_path])
    assert rejected["status"] == "failed", rejected
    assert "data source changed" in rejected["error_summary"]
    assert not list(Path(rejected["run_dir"]).rglob("*.png"))
    assert not list(Path(rejected["run_dir"]).rglob("*.qgz"))


@pytest.mark.parametrize("kind", ["shapefile", "raster"])
async def test_native_source_identity_binds_sidecars_and_raster_pixels(tmp_path, kind):
    settings, payload = _native_case(tmp_path)
    original = Path(payload["layers"][0]["file_path"])
    if kind == "shapefile":
        import geopandas as gpd

        source = tmp_path / "subject.shp"
        data = gpd.read_file(original)
        data.to_file(source)
    else:
        import numpy as np
        import rasterio
        from rasterio.transform import from_bounds

        from geoharness._proj_fix import ensure_proj_data

        ensure_proj_data()
        source = tmp_path / "subject.tif"
        with rasterio.open(source, "w", driver="GTiff", width=10, height=10, count=1,
            dtype="uint8", crs="EPSG:3857",
            transform=from_bounds(1000, 1000, 2000, 2000, 10, 10)) as dataset:
            dataset.write(np.ones((1, 10, 10), dtype="uint8"))
    payload["layers"] = [{"file_path": str(source), "name": "subject",
                          "kind": "vector" if kind == "shapefile" else "raster"}]
    preflight = await execute_qgis("project_preflight", payload, cwd=tmp_path,
                                  settings=settings, read_roots=[tmp_path])
    assert preflight["status"] == "succeeded", preflight.get("error_summary")
    plan = preflight["result"]["layout_plan"]
    identities = plan["source_identities"][0]["files"]
    assert any(Path(item["path"]) == source for item in identities)
    if kind == "shapefile":
        assert any(Path(item["path"]).suffix == ".dbf" for item in identities)
        geometry_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        data["name"] = ["changed DBF content"]
        data.to_file(source)
        assert hashlib.sha256(source.read_bytes()).hexdigest() == geometry_digest
    else:
        with rasterio.open(source, "r+") as dataset:
            dataset.write(np.full((1, 10, 10), 2, dtype="uint8"))
    with_plan = {**payload, "layout": {**payload["layout"], "layout_plan": plan}}
    assert _layout_input_digest(with_plan) == plan["input_digest"]
    rejected = await execute_qgis("project_create", with_plan, cwd=tmp_path,
                                  settings=settings, read_roots=[tmp_path])
    assert rejected["status"] == "failed", rejected
    assert "data source changed" in rejected["error_summary"]
    assert not list(Path(rejected["run_dir"]).rglob("*.png"))
    assert not list(Path(rejected["run_dir"]).rglob("*.qgz"))


@pytest.mark.parametrize("window", ["measuring", "snapshotting"])
async def test_native_source_mutation_during_plan_consumption_is_rejected(tmp_path, window):
    settings, payload = _native_case(tmp_path)
    preflight = await execute_qgis("project_preflight", payload, cwd=tmp_path,
                                  settings=settings, read_roots=[tmp_path])
    assert preflight["status"] == "succeeded", preflight.get("error_summary")
    payload["layout"]["layout_plan"] = preflight["result"]["layout_plan"]
    request = tmp_path / "injected-request.json"
    request.write_text(json.dumps(payload), encoding="utf-8")
    output = tmp_path / "injected-output"
    runtime = discover_qgis(settings, cwd=tmp_path)
    worker = Path(__file__).parents[1] / "src/geoharness/qgis/worker.py"
    script = """
import json,os,runpy,sys
from pathlib import Path
runpy.run_path(sys.argv[1],run_name='plan_mutation_fixture')
from qgis.core import QgsApplication,QgsProject
from geoharness.qgis import project_worker as native
from geoharness.qgis.worker_context import WorkerContext
QgsApplication.setPrefixPath(os.environ['QGIS_PREFIX_PATH'],True)
app=QgsApplication([],False);app.initQgis()
payload=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
context=WorkerContext(Path(sys.argv[3]),Path(sys.argv[4]),[Path(sys.argv[3])])
def mutate():
    source=Path(payload['layers'][0]['file_path'])
    content=json.loads(source.read_text(encoding='utf-8'))
    content['features'][0]['properties']['name']='changed inside deterministic window'
    source.write_text(json.dumps(content),encoding='utf-8')
if sys.argv[5]=='measuring':
    original=native._freeze_layout_plan
    def injected(*args,**kwargs):
        mutate()
        return original(*args,**kwargs)
    native._freeze_layout_plan=injected
else:
    original=native._snapshot_layer
    def injected(*args,**kwargs):
        result=original(*args,**kwargs)
        mutate()
        return result
    native._snapshot_layer=injected
try:
    native.dispatch('project_create',payload,context)
    print(json.dumps({'error':None}))
except ValueError as exc:
    print(json.dumps({'error':str(exc)}))
finally:
    QgsProject.instance().clear();app.exitQgis()
"""
    response = subprocess.run([str(runtime.python_executable), "-c", script, str(worker),
        str(request), str(tmp_path), str(output), window],
        env=child_environment(runtime, tmp_path / "injected-runtime"),
        capture_output=True, text=True, encoding="utf-8", timeout=45)
    assert response.returncode == 0, response.stderr
    result = json.loads(response.stdout)
    assert f"data source changed while {window}" in (result["error"] or "")
    assert not list(output.rglob("*.png"))
    assert not list(output.rglob("*.qgz"))


@pytest.mark.parametrize("aux_metadata", [False, True])
async def test_direct_native_hillshade_keeps_original_raster_and_dependencies_read_only(
    tmp_path, aux_metadata,
):
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    from geoharness._proj_fix import ensure_proj_data

    ensure_proj_data()
    settings, payload = _native_case(tmp_path)
    source = tmp_path / "elevation.tif"
    with rasterio.open(source, "w", driver="GTiff", width=20, height=20, count=1,
        dtype="float32", crs=None if aux_metadata else "EPSG:3857",
        transform=from_origin(1000, 3000, 100, 100)) as dataset:
        dataset.write(np.arange(400, dtype=np.float32).reshape(20, 20), 1)
    if aux_metadata:
        from pyproj import CRS

        source.with_name(source.name + ".aux.xml").write_text(
            "<PAMDataset><SRS>" + CRS.from_epsg(3857).to_wkt() + "</SRS>"
            '<PAMRasterBand band="1"><NoDataValue>-9999</NoDataValue>'
            "</PAMRasterBand></PAMDataset>", encoding="utf-8")
    style = {"renderer": "hillshade", "band": 1, "azimuth": 300, "altitude": 35}
    payload["layers"] = [{"file_path": str(source), "kind": "raster", "raster_style": style}]
    payload["map_extent"] = [900, 900, 3100, 3100]
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
              for path in tmp_path.glob("elevation.*")}
    created = await execute_qgis("project_create", payload, cwd=tmp_path,
                                settings=settings, read_roots=[tmp_path])
    after = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in tmp_path.glob("elevation.*")}
    assert after == before, "Native raster provider wrote a dependency beside the original source"
    assert created["status"] == "succeeded", created.get("error_summary")
    assert created["result"]["layers"][0]["raster_style"] == style
    assert created["result"]["layers"][0]["crs"] == "EPSG:3857"
    if aux_metadata:
        packaged = Path(created["result"]["project_dir"]) / "data/layer_0.tif"
        with rasterio.open(packaged) as dataset:
            assert dataset.crs.to_epsg() == 3857
            assert dataset.nodata == -9999
