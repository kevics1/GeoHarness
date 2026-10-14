"""Native data refs retain evidence; Python outputs and raw inputs gain no false binding."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import box

from geoharness.config.settings import AnalysisConfig, GeoConfig, QgisConfig
from geoharness.data.task_context import data_references, recent_outputs, resolve_registered_path
from geoharness.tools.geo_analysis import GeoAnalysisInput, GeoAnalysisTool
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def _analysis(tmp_path):
    config = GeoConfig(qgis=QgisConfig(enabled=False),
                       analysis=AnalysisConfig(metric_crs="EPSG:3857"))
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    source = gpd.GeoDataFrame({"stage": ["first", "second"]}, geometry=[
        box(0, 0, 100, 100), box(0, 0, 200, 200)], crs=3857)
    source_path = tmp_path / "source.geojson"
    source.to_file(source_path)
    result = await GeoAnalysisTool().execute(GeoAnalysisInput(action="area", source={
        "file_path": str(source_path)}), context)
    assert not result.is_error, result.output
    refs = data_references({}, recent_outputs(tmp_path, config))
    context.metadata["_data_refs_snapshot"] = refs
    record = next(item for item in refs.values() if item.get("output_label") == "result")
    return context, record, source_path


@pytest.mark.parametrize("locator", ["ref", "path"])
async def test_exact_native_locator_infers_evidence_and_preserves_derivation(tmp_path, locator):
    context, record, _ = await _analysis(tmp_path)
    assert record["analysis_manifest"] == record["manifest"]
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action="preflight", layers=[{
        "file_path": record[locator], "derivation": {"feature_indices": [0]}}]), context)
    assert not result.is_error, result.output
    report = result.metadata["preflight"]
    assert report["analysis_manifests"] == [record["manifest"]]
    assert report["analysis"]["status"] == "verified"
    assert report["analysis"]["analyses"][0]["mapped_outputs"][0]["match"] == "derivation"
    assert "provenance automatically" in report["analysis_binding_note"]


async def test_original_path_is_not_replaced_or_bound_to_unmapped_analysis(tmp_path):
    context, record, source = await _analysis(tmp_path)
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action="preflight",
        analysis_manifests=[record["manifest"]], layers=[{"file_path": str(source)}]), context)
    assert result.is_error and "analysis_output_not_mapped" in result.output
    assert result.metadata["diagnostics"]["mapped_layers"][0]["file_path"] == str(source)


async def test_frozen_reference_rejects_changed_manifest_or_fingerprint(tmp_path, monkeypatch):
    from geoharness.data import profiles

    context, record, _ = await _analysis(tmp_path)
    manifest = Path(record["manifest"])
    raw = manifest.read_text(encoding="utf-8")
    manifest.write_text(raw + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest is stale"):
        resolve_registered_path(record["ref"], context.metadata,
                                cwd=tmp_path, config=context.metadata["geoharness_config"])
    actual_signature = profiles.file_signature
    monkeypatch.setattr(profiles, "file_signature", lambda path: record["manifest_signature"]
        if Path(path) == manifest else actual_signature(path))
    with pytest.raises(ValueError, match="manifest changed"):
        resolve_registered_path(record["ref"], context.metadata,
                                cwd=tmp_path, config=context.metadata["geoharness_config"])
    manifest.write_text(raw, encoding="utf-8")
    record["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="analysis output changed"):
        resolve_registered_path(record["ref"], context.metadata,
                                cwd=tmp_path, config=context.metadata["geoharness_config"])


async def test_python_result_stays_unverified_not_native(tmp_path):
    context, _, source = await _analysis(tmp_path)
    directory = (tmp_path / context.metadata["geoharness_config"].execution.outputs_dir
                 / "python-job")
    directory.mkdir(parents=True)
    output = directory / "copy.geojson"
    output.write_bytes(source.read_bytes())
    (directory / "manifest.json").write_text(json.dumps({"run_dir": str(directory),
        "exit_code": 0, "files": [{"path": str(output)}]}), encoding="utf-8")
    refs = data_references({}, recent_outputs(tmp_path, context.metadata["geoharness_config"]))
    record = next(item for item in refs.values() if item["path"] == str(output))
    assert record["producer"] == "geo_python" and "analysis_manifest" not in record
    context.metadata["_data_refs_snapshot"] = refs
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action="preflight",
        layers=[{"file_path": record["ref"]}]), context)
    assert not result.is_error, result.output
    assert result.metadata["preflight"]["analysis_evidence_status"] == "not_provided"
