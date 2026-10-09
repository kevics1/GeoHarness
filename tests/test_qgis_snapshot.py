"""Create preflight and workers must read the same independently frozen inputs."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from xml.sax.saxutils import escape

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from openharness.tools.base import ToolExecutionContext
from rasterio.transform import from_origin
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig, QgisConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def _context(tmp_path, **qgis_settings):
    config = GeoConfig(qgis=QgisConfig(**qgis_settings))
    return ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})


def _vector(path, *, changed=False, count=1):
    gpd.GeoDataFrame(
        {"name": ["changed" if changed else "original"] * count},
        geometry=[Point(10 + int(changed), 20)] * count, crs=4326,
    ).to_file(path, driver="GeoJSON")


@pytest.mark.parametrize("new_count", [1, 2])
async def test_create_worker_uses_preflight_vector_after_original_changes(
    tmp_path, monkeypatch, new_count,
):
    source = tmp_path / "source.geojson"
    _vector(source)
    snapshots = []

    async def runner(action, payload, **kwargs):
        _vector(source, changed=True, count=new_count)
        spec = payload["layers"][0]
        frozen = Path(spec["file_path"])
        frame = gpd.read_file(frozen, layer=spec.get("layer_name") or None)
        assert len(frame) == 1
        assert frame["name"].tolist() == ["original"]
        assert frame.geometry.iloc[0] == Point(10, 20)
        assert spec["name"] == "source"
        assert frozen.parent in kwargs["read_roots"]
        snapshots.append(frozen)
        return {"status": "succeeded", "result": {}}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    arguments = GeoQgisProjectInput(
        action="create", layers=[{"file_path": str(source), "expected_feature_count": 1}],
    )
    result = await GeoQgisProjectTool().execute(arguments, _context(tmp_path))
    assert not result.is_error, result.output
    assert result.metadata["preflight"]["layers"][0]["feature_count"] == 1
    assert snapshots and not snapshots[0].parent.exists()
    assert arguments.layers[0].file_path == str(source)


async def test_create_worker_uses_frozen_raster_pixels(tmp_path, monkeypatch):
    source = tmp_path / "source.tif"

    def write(value):
        with rasterio.open(
            source, "w", driver="GTiff", height=2, width=2, count=1, dtype="uint8",
            crs=4326, transform=from_origin(10, 20, 0.01, 0.01),
        ) as dataset:
            dataset.write(np.full((1, 2, 2), value, dtype="uint8"))

    write(3)
    snapshots = []

    async def runner(action, payload, **kwargs):
        write(9)
        frozen = Path(payload["layers"][0]["file_path"])
        with rasterio.open(frozen) as dataset:
            assert dataset.driver == "GTiff"
            assert np.all(dataset.read(1) == 3)
            assert all(Path(path).parent == frozen.parent for path in dataset.files)
        snapshots.append(frozen)
        return {"status": "succeeded", "result": {}}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="create", layers=[{"file_path": str(source), "kind": "raster"}]),
        _context(tmp_path),
    )
    assert not result.is_error, result.output
    assert snapshots and not snapshots[0].parent.exists()


async def test_read_only_preflight_does_not_create_snapshot_directories(tmp_path, monkeypatch):
    source = tmp_path / "source.geojson"
    _vector(source)
    original_files = set(tmp_path.rglob("*"))
    monkeypatch.setattr("geoharness.tools.geo_qgis_project.TemporaryDirectory",
                        lambda **kwargs: pytest.fail("read-only preflight created a snapshot"))
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="preflight", layers=[{"file_path": str(source)}]),
        _context(tmp_path, enabled=False),
    )
    assert not result.is_error, result.output
    assert json.loads(result.output)["layers"][0]["feature_count"] == 1
    assert set(tmp_path.rglob("*")) == original_files


async def test_source_change_during_freezing_rejects_before_budget_and_cleans(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.geojson"
    _vector(source)
    original_read = gpd.read_file
    snapshots = []

    def changing_read(path, **kwargs):
        frame = original_read(path, **kwargs)
        if Path(path) == source:
            _vector(source, changed=True)
        return frame

    from geoharness.cartography.snapshot import freeze_qgis_inputs

    def remember_snapshot(*args, **kwargs):
        snapshots.append(kwargs["directory"])
        return freeze_qgis_inputs(*args, **kwargs)

    monkeypatch.setattr(gpd, "read_file", changing_read)
    monkeypatch.setattr("geoharness.tools.geo_qgis_project.freeze_qgis_inputs", remember_snapshot)
    context = _context(tmp_path)
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="create", layers=[{"file_path": str(source)}]), context,
    )
    assert result.is_error and "source changed" in result.output
    assert result.metadata["render_executed"] is False
    assert "map_workflow_state" not in context.metadata
    assert snapshots and not snapshots[0].exists()


async def test_shapefile_attribute_change_during_freezing_is_not_hidden_by_shp_hash(
    tmp_path, monkeypatch,
):
    source = tmp_path / "source.shp"
    gpd.GeoDataFrame({"name": ["original"]}, geometry=[Point(10, 20)], crs=4326).to_file(source)
    original_geometry_bytes = source.read_bytes()
    original_read = gpd.read_file
    calls = []

    def changing_read(path, **kwargs):
        frame = original_read(path, **kwargs)
        if Path(path) == source:
            attributes = source.with_suffix(".dbf")
            raw = attributes.read_bytes()
            assert b"original" in raw
            attributes.write_bytes(raw.replace(b"original", b"changed "))
        return frame

    async def runner(action, payload, **kwargs):
        calls.append(payload)
        return {"status": "succeeded", "result": {}}

    monkeypatch.setattr(gpd, "read_file", changing_read)
    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    context = _context(tmp_path)
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="create", layers=[{"file_path": str(source)}]), context,
    )
    assert source.read_bytes() == original_geometry_bytes
    assert result.is_error and "source changed" in result.output and ".dbf" in result.output
    assert not calls and "map_workflow_state" not in context.metadata


async def test_snapshot_root_does_not_authorize_original_outside_read_roots(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "source.geojson"
    _vector(source)
    context = _context(workspace)
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="create", layers=[{"file_path": str(source)}]), context,
    )
    assert result.is_error and "outside allowed read roots" in result.output
    assert "map_workflow_state" not in context.metadata


def _write_vrt(path: Path, source: str) -> None:
    path.write_text(
        '<VRTDataset rasterXSize="2" rasterYSize="2"><SRS>EPSG:4326</SRS>'
        '<GeoTransform>10,0.01,0,20,0,-0.01</GeoTransform>'
        '<VRTRasterBand dataType="Byte" band="1"><SimpleSource>'
        f'<SourceFilename relativeToVRT="1">{escape(source)}</SourceFilename>'
        '<SourceBand>1</SourceBand><SrcRect xOff="0" yOff="0" xSize="2" ySize="2" />'
        '<DstRect xOff="0" yOff="0" xSize="2" ySize="2" />'
        '</SimpleSource></VRTRasterBand></VRTDataset>', encoding="utf-8",
    )


async def test_safe_local_vrt_is_frozen_without_depending_on_original_pixels(tmp_path, monkeypatch):
    pixels = tmp_path / "pixels.tif"
    source = tmp_path / "local.tif"

    def write(value):
        with rasterio.open(
            pixels, "w", driver="GTiff", height=2, width=2, count=1, dtype="uint8",
            crs=4326, transform=from_origin(10, 20, 0.01, 0.01),
        ) as dataset:
            dataset.write(np.full((1, 2, 2), value, dtype="uint8"))

    write(3)
    _write_vrt(source, pixels.name)
    with rasterio.open(source) as dataset:
        assert dataset.driver == "VRT"
        assert np.all(dataset.read(1) == 3)
    original_vrt_bytes = source.read_bytes()
    snapshots = []

    async def runner(action, payload, **kwargs):
        write(9)
        frozen = Path(payload["layers"][0]["file_path"])
        with rasterio.open(frozen) as dataset:
            assert dataset.driver == "GTiff"
            assert np.all(dataset.read(1) == 3)
            assert all(Path(path).parent == frozen.parent for path in dataset.files)
        snapshots.append(frozen)
        return {"status": "succeeded", "result": {}}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="create", layers=[{"file_path": str(source), "kind": "raster"}]),
        _context(tmp_path),
    )
    assert not result.is_error, result.output
    assert source.read_bytes() == original_vrt_bytes
    assert snapshots and not snapshots[0].parent.exists()


@pytest.mark.parametrize("dependency", ["remote", "outside", "credential"])
async def test_vrt_dependencies_are_guarded_before_native_read(tmp_path, monkeypatch, dependency):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "unsafe.tif"
    if dependency == "remote":
        locator = "https://example.invalid/pixels.tif"
        message = "Only local file paths"
    else:
        path = (tmp_path / "outside.tif" if dependency == "outside" else
                workspace / "credentials.tif")
        path.write_bytes(b"not read")
        locator = str(path)
        message = "outside allowed read roots" if dependency == "outside" else "Credential"
    _write_vrt(source, locator)
    monkeypatch.setattr(rasterio, "open",
                        lambda *args, **kwargs: pytest.fail("unguarded VRT reached rasterio"))
    context = _context(workspace)
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="create", layers=[{"file_path": str(source), "kind": "raster"}]),
        context,
    )
    assert result.is_error and message in result.output
    assert "map_workflow_state" not in context.metadata


@pytest.mark.parametrize("selector", ["|layername=selected", "|layerid=1", ""])
async def test_create_freezes_the_selected_gpkg_layer(tmp_path, monkeypatch, selector):
    source = tmp_path / "source.gpkg"
    for name in ("other", "selected"):
        gpd.GeoDataFrame({"name": [name], "fid": [42]}, geometry=[Point(10, 20)], crs=4326).to_file(
            source, driver="GPKG", layer=name, FID="internal_id", index=False,
        )

    async def runner(action, payload, **kwargs):
        spec = payload["layers"][0]
        frozen = gpd.read_file(spec["file_path"], layer=spec["layer_name"])
        assert frozen["name"].tolist() == ["selected"]
        assert frozen["fid"].tolist() == [42]
        return {"status": "succeeded", "result": {}}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="create", layers=[{
            "file_path": str(source) + selector,
            "layer_name": "selected" if not selector else "",
            "expected_feature_count": 1,
        }]), _context(tmp_path),
    )
    assert not result.is_error, result.output


async def test_create_preserves_optional_empty_layer_and_count_checks(tmp_path, monkeypatch):
    source = tmp_path / "source.geojson"
    _vector(source)
    empty = tmp_path / "empty.geojson"
    gpd.GeoDataFrame({"name": []}, geometry=[], crs=4326).to_file(empty, driver="GeoJSON")
    calls = []

    async def runner(action, payload, **kwargs):
        spec = payload["layers"][1]
        assert gpd.read_file(spec["file_path"], layer=spec["layer_name"]).empty
        calls.append(payload)
        return {"status": "succeeded", "result": {}}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    layers = [{"file_path": str(source)}, {"file_path": str(empty), "required": False}]
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="create", layers=layers), _context(tmp_path),
    )
    assert not result.is_error, result.output
    assert result.metadata["preflight"]["layers"][1]["minimum_visible_features"] == 0
    assert result.metadata["preflight"]["warnings"]
    context = _context(tmp_path)
    layers[0]["expected_feature_count"] = 2
    failed = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="create", layers=layers), context,
    )
    assert failed.is_error and "expected_feature_count=2" in failed.output
    assert len(calls) == 1 and "map_workflow_state" not in context.metadata


@pytest.mark.parametrize("interruption", ["cancel", "timeout"])
async def test_preflight_drains_owned_thread_before_snapshot_cleanup(
    tmp_path, monkeypatch, interruption,
):
    source = tmp_path / "source.geojson"
    _vector(source)
    entered, release = threading.Event(), threading.Event()
    snapshots = []
    tool = GeoQgisProjectTool()
    original = tool._preflight

    def blocking_preflight(arguments, context, **kwargs):
        snapshots.append(Path(arguments.layers[0].file_path))
        entered.set()
        assert release.wait(5)
        assert snapshots[0].exists()
        return original(arguments, context, **kwargs)

    monkeypatch.setattr(tool, "_preflight", blocking_preflight)
    context = _context(tmp_path, timeout_seconds=0.3 if interruption == "timeout" else 30)
    task = asyncio.create_task(tool.execute(
        GeoQgisProjectInput(action="create", layers=[{"file_path": str(source)}]), context,
    ))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if interruption == "cancel":
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(0.4)
        assert not task.done() and snapshots[0].exists()
    finally:
        release.set()
    if interruption == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result.is_error and result.metadata["render_executed"] is False
    assert not snapshots[0].parent.exists()
    assert "map_workflow_state" not in context.metadata


async def test_worker_shutdown_precedes_snapshot_cleanup_on_repeated_cancel(tmp_path, monkeypatch):
    source = tmp_path / "source.geojson"
    _vector(source)
    entered, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    snapshots = []

    async def runner(action, payload, **kwargs):
        snapshots.append(Path(payload["layers"][0]["file_path"]))
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopping.set()
            await release.wait()
            assert snapshots[0].exists()
            raise

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    context = _context(tmp_path)
    task = asyncio.create_task(GeoQgisProjectTool().execute(
        GeoQgisProjectInput(action="create", map_id="cancelled",
                            layers=[{"file_path": str(source)}]), context,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(stopping.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and snapshots[0].exists()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not snapshots[0].parent.exists()
    assert context.metadata["map_workflow_state"]["maps"]["cancelled"]["renders"] == 0


async def test_real_worker_package_and_receipt_use_frozen_analysis_data(tmp_path, monkeypatch):
    from geoharness.analysis.vector import analyze_vectors
    from geoharness.qgis.runner import execute_qgis
    from geoharness.tools.geo_analysis import GeoAnalysisInput, SpatialSource

    configured_outputs = tmp_path.parent / f"{tmp_path.name}-configured-qgis"
    context = _context(tmp_path, outputs_dir=str(configured_outputs))
    try:
        discover_qgis(context.metadata["geoharness_config"].qgis, cwd=tmp_path)
    except (OSError, ValueError, RuntimeError) as exc:
        pytest.skip(f"An independent QGIS installation is required: {exc}")
    manifest = analyze_vectors(
        GeoAnalysisInput(action="buffer", distance_m=15, source=SpatialSource(
            geojson=gpd.GeoDataFrame({"name": ["original"]},
                                    geometry=[Point(500000, 3400000)], crs=32650).to_json(),
            crs="EPSG:32650",
        )), cwd=tmp_path, outputs_dir="analysis", metric_crs="EPSG:32650",
    )
    source = Path(manifest["paths"]["result"]["geojson"])
    original = gpd.read_file(source)
    snapshots = []

    async def mutate_then_execute(action, payload, **kwargs):
        snapshots.append(Path(payload["layers"][0]["file_path"]))
        changed = original.copy()
        changed["name"] = "changed"
        changed.geometry = changed.translate(xoff=0.1)
        changed.to_file(source, driver="GeoJSON")
        assert payload["analysis_facts"]["status"] == "verified"
        return await execute_qgis(action, payload, **kwargs)

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", mutate_then_execute)
    result = await GeoQgisProjectTool().execute(
        GeoQgisProjectInput(
            action="create", layers=[{"file_path": str(source), "expected_feature_count": 1}],
            analysis_manifests=[manifest["manifest_path"]], dpi=36,
        ), context,
    )
    assert not result.is_error, result.output
    assert Path(result.metadata["project_dir"]).is_relative_to(configured_outputs)
    packaged = gpd.read_file(Path(result.metadata["project_dir"]) / "data/layer_0.gpkg")
    assert packaged["name"].tolist() == ["original"]
    assert packaged.geometry.iloc[0].equals_exact(original.geometry.iloc[0], 1e-12)
    assert result.metadata["map_receipt"]["stages"]["data_checked"]["status"] == "passed"
    assert result.metadata["map_receipt"]["analysis_correctness"] == "unverified"
    facts = json.loads((Path(result.metadata["project_dir"]) /
                        "analysis/verified-facts.json").read_text(encoding="utf-8"))
    assert facts == result.metadata["preflight"]["analysis"]
    assert not snapshots[0].parent.exists()
