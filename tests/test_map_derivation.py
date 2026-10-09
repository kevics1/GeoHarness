"""Map-only subsets retain verifiable analytical provenance."""

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point
from test_map_facts import _analysis

from geoharness.cartography.facts import load_analysis_facts
from geoharness.config.settings import GeoConfig
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


def test_declared_subset_fields_and_annotation_match_actual_mapped_features(tmp_path):
    from geoharness.cartography.derivation import derive_frame

    manifest = _analysis(tmp_path, "distance")
    source = manifest["paths"]["result"]["geojson"]
    spec = {"feature_indices": [1], "keep_fields": ["asset", "distance_m"],
            "annotations": {"map_label": ["nearest shelter"]}}
    frame = derive_frame(gpd.read_file(source), spec)
    facts = load_analysis_facts(
        [manifest["manifest_path"]], cwd=tmp_path, read_roots=[tmp_path],
        mapped_sources=[{"file_path": source, "gdf": frame, "derivation": spec}],
        display_crs="EPSG:3857",
    )
    match = facts["analyses"][0]["mapped_outputs"][0]
    assert match["match"] == "derivation"
    assert match["mapped_feature_count"] == 1
    assert match["source_feature_count"] == 2
    assert facts["analyses"][0]["statistics"]["feature_count"] == 2
    assert "制图子集" in facts["footer_text"]
    frame.loc[frame.index[0], "geometry"] = Point(0, 0)
    with pytest.raises(ValueError, match="changed after it was loaded"):
        load_analysis_facts(
            [manifest["manifest_path"]], cwd=tmp_path, read_roots=[tmp_path],
            mapped_sources=[{"file_path": source, "gdf": frame, "derivation": spec}],
            display_crs="EPSG:3857",
        )


@pytest.mark.parametrize("spec", [
    {"feature_indices": [0, 0]}, {"feature_indices": [-1]}, {"feature_indices": [99]},
    {"keep_fields": ["missing"]}, {"annotations": {"asset": ["changed", "changed"]}},
    {"annotations": {"geometry": ["fake", "fake"]}},
    {"annotations": {"label": ["wrong length"]}},
])
def test_derivation_rejects_invalid_or_overwriting_operations(tmp_path, spec):
    from geoharness.cartography.derivation import derive_frame

    frame = gpd.GeoDataFrame({"asset": ["a", "b"]}, geometry=[Point(1, 1)] * 2, crs=4326)
    with pytest.raises(ValueError):
        derive_frame(frame, spec)


async def test_qgis_preflight_checks_frozen_subset_against_full_native_analysis(tmp_path):
    manifest = _analysis(tmp_path, "distance")
    source = manifest["paths"]["result"]["geojson"]
    arguments = GeoQgisProjectInput(action="preflight", crs="EPSG:4326",
        analysis_manifests=[manifest["manifest_path"]], layers=[{
            "file_path": source, "derivation": {"feature_indices": [1],
            "keep_fields": ["asset", "distance_m"], "annotations": {"label": ["B"]}},
            "label_field": "label",
        }])
    result = await GeoQgisProjectTool().execute(arguments, ToolExecutionContext(
        cwd=tmp_path, metadata={"geoharness_config": GeoConfig()},
    ))
    assert not result.is_error, result.output
    report = result.metadata["preflight"]
    assert report["layers"][0]["feature_count"] == 1
    assert report["analysis"]["analyses"][0]["mapped_outputs"][0]["match"] == "derivation"


async def test_native_manifest_is_inferred_and_conflicting_metric_crs_is_rejected(tmp_path):
    manifest = _analysis(tmp_path, "distance")
    source = manifest["paths"]["result"]["geojson"]
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="preflight", crs="EPSG:3857", layers=[{"file_path": source}],
    ), context)
    assert not result.is_error
    assert result.metadata["preflight"]["analysis"]["analyses"][0]["metric_crs"] == "EPSG:32650"
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="create", crs="EPSG:3857", layers=[{"file_path": source}],
        layout={"footer_text": "计量CRS EPSG:3857"},
    ), context)
    assert result.is_error and "contradicts verified analysis" in result.output
    assert not context.metadata.get("map_workflow_state", {}).get("maps")


async def test_repaired_reference_output_invalidates_cached_failure(tmp_path):
    from pathlib import Path

    manifest = _analysis(tmp_path, "distance")
    path = Path(manifest["paths"]["reference"]["geojson"])
    before = path.read_bytes()
    path.write_bytes(before + b"\n")
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    tool = GeoQgisProjectTool()
    arguments = GeoQgisProjectInput(action="preflight", layers=[{
        "file_path": manifest["paths"]["result"]["geojson"],
    }])
    assert (await tool.execute(arguments, context)).is_error
    assert (await tool.execute(arguments, context)).metadata["duplicate_error_blocked"]
    path.write_bytes(before)
    result = await tool.execute(arguments, context)
    assert not result.is_error, result.output
