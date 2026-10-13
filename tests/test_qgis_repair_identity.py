"""Real native packaging must not add system fields to verified business evidence."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point, box

from geoharness.analysis.vector import analyze_vectors
from geoharness.config.settings import GeoConfig
from geoharness.qgis.discovery import discover_qgis
from geoharness.tools.geo_analysis import GeoAnalysisInput, SpatialSource
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


@pytest.mark.parametrize("business_fid", [False, True])
async def test_native_package_rebuild_preserves_business_schema_and_analysis(
    tmp_path, business_fid,
):
    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError) as exc:
        pytest.skip(f"QGIS unavailable: {exc}")
    properties = {"name": ["inside", "outside"]}
    if business_fid:
        properties.update(fid=[41, 42], _geoharness_fid=[81, 82])
    assets = gpd.GeoDataFrame(properties, geometry=[Point(0, 0), Point(100, 100)], crs=3857)
    hazard = gpd.GeoDataFrame(geometry=[box(-10, -10, 10, 10)], crs=3857)
    analysis = analyze_vectors(GeoAnalysisInput(action="impact",
        source=SpatialSource(geojson=assets.to_json(), crs="EPSG:3857"),
        reference=SpatialSource(geojson=hazard.to_json(), crs="EPSG:3857")),
        cwd=tmp_path, outputs_dir="analysis", metric_crs="EPSG:3857")
    source = Path(analysis["paths"]["result"]["geojson"])
    original = gpd.read_file(source)
    original_bytes = source.read_bytes()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    tool = GeoQgisProjectTool()
    created = await tool.execute(GeoQgisProjectInput(action="create", dpi=72,
        layers=[{"file_path": str(source), "name": "assets", "label_field": "name"}],
        analysis_manifests=[analysis["manifest_path"]],
        layout={"legend": False, "scale_bar": False}), context)
    assert not created.is_error, created.output
    manifest_path = Path(created.metadata["project_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    packed = gpd.read_file(manifest_path.parent / manifest["layers"][0]["file"])
    assert set(packed.columns) == set(original.columns)
    for field in original.columns.drop(original.geometry.name):
        assert packed[field].tolist() == original[field].tolist()
    capsule = manifest["creation_spec"]
    assert capsule["source_bindings"][0]["layer_key"]
    assert manifest["frame_checks"]["layers"][0]["layer_key"] == \
        capsule["source_bindings"][0]["layer_key"]
    parameters = capsule["parameters"]
    for layer in parameters["layers"]:
        layer["file_path"] = str(manifest_path.parent / layer["file_path"])
    rebuilt = await tool.execute(GeoQgisProjectInput(action="create", **parameters), context,
                                 _source_bindings=capsule["source_bindings"])
    assert not rebuilt.is_error, rebuilt.output
    assert rebuilt.metadata["preflight"]["analysis"]["status"] == "verified"
    assert source.read_bytes() == original_bytes
    # The correction must not allow a real business edit of the packaged layer.
    packed.loc[0, "name"] = "changed"
    tampered = tmp_path / "changed.gpkg"
    packed.to_file(tampered, driver="GPKG", layer="changed", index=False,
                   FID="separate_internal_pk")
    parameters["layers"][0].update(file_path=str(tampered), layer_name="changed")
    rejected = await tool.execute(GeoQgisProjectInput(action="create", **parameters), context,
                                  _source_bindings=capsule["source_bindings"])
    assert rejected.is_error and "Mapped data changed" in rejected.output

