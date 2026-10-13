"""Frozen data refs prevent guessed paths and keep native analysis provenance."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.analysis.vector import analyze_vectors
from geoharness.cartography.derivation import derive_frame
from geoharness.cartography.facts import load_analysis_facts
from geoharness.config.settings import GeoConfig
from geoharness.data.profiles import file_signature
from geoharness.data.task_context import data_references, model_data_context, recent_outputs
from geoharness.execution import execute_python
from geoharness.execution.runner import _data_helpers
from geoharness.tools.geo_analysis import GeoAnalysisInput, SpatialSource
from geoharness.tools.geo_python import (
    GeoPythonInput,
    GeoPythonTool,
    python_recovery_hints,
    qgis_diagnostic_files,
)


def _profile(path, *, name="points", crs=4326):
    path.parent.mkdir(parents=True, exist_ok=True)
    gpd.GeoDataFrame({"value": [1, 2]}, geometry=[Point(0, 0), Point(1, 1)],
                     crs=crs).to_file(path)
    return {"path": str(path), "name": name, "kind": "vector", "crs": f"EPSG:{crs}",
            "feature_count": 2, "file_stat": file_signature(path), "freshness": "current"}


def _native(tmp_path, config):
    frame = gpd.GeoDataFrame({"name": ["A", "B"]},
                            geometry=[Point(0, 0), Point(100, 100)], crs=3857)
    return analyze_vectors(GeoAnalysisInput(action="buffer", distance_m=10,
        source=SpatialSource(geojson=frame.to_json(), crs="EPSG:3857")),
        cwd=tmp_path, outputs_dir=config.analysis.outputs_dir, metric_crs="EPSG:3857")


def test_refs_only_register_current_signed_profiles_and_keep_stable_ids(tmp_path):
    path = tmp_path / "points.geojson"
    profile = _profile(path)
    refs = data_references({"source": profile}, [])
    record = next(iter(refs.values()))
    assert record["path"] == str(path) and record["signature"] == profile["file_stat"]
    assert record["producer"] == "measured_source" and record["kind"] == "vector"
    assert data_references({"other key": {**profile, "name": "renamed"}}, []).keys() == refs.keys()
    assert data_references({"unsigned": {**profile, "file_stat": None}}, []) == {}
    assert data_references({"unsigned": {**profile, "freshness": "unverified"}}, []) == {}
    path.write_text("replacement", encoding="utf-8")
    assert data_references({"source": profile}, []) == {}
    with pytest.raises(ValueError, match="stale or missing"):
        _data_helpers(refs)["resolve_data"](record["ref"])
    path.unlink()
    with pytest.raises(ValueError, match="stale or missing"):
        _data_helpers(refs)["resolve_data"](record["ref"])


def test_refs_reject_unknown_and_ambiguous_names_without_guessing(tmp_path):
    first = _profile(tmp_path / "a/points.geojson")
    second = _profile(tmp_path / "b/points.geojson")
    refs = data_references({"a": first, "b": second}, [])
    resolve = _data_helpers(refs)["resolve_data"]
    with pytest.raises(ValueError, match="Ambiguous"):
        resolve("points")
    with pytest.raises(ValueError, match="Unknown"):
        resolve("20261015/result.geojson")
    assert resolve(first["path"]) == Path(first["path"])
    for ref, record in refs.items():
        assert resolve(ref) == Path(record["path"])


def test_native_refs_use_actual_output_label_manifest_and_fingerprint(tmp_path):
    config = GeoConfig()
    manifest = _native(tmp_path, config)
    refs = data_references({}, recent_outputs(tmp_path, config))
    record = next(item for item in refs.values() if item.get("output_label") == "result")
    assert record["producer"] == "geo_analysis" and record["action"] == "buffer"
    assert record["path"] == manifest["paths"]["result"]["geojson"]
    assert record["manifest"] == manifest["manifest_path"]
    assert record["manifest_signature"] == file_signature(record["manifest"])
    assert record["sha256"] == manifest["paths"]["result"]["geojson_sha256"]
    helpers = _data_helpers(refs)
    layer = helpers["native_layer"](record["ref"], derivation={"feature_indices": [0]},
                                     name="map subset", style={"renderer": "single"})
    assert layer["file_path"] == record["path"]
    layer["gdf"] = derive_frame(gpd.read_file(record["path"]), layer["derivation"])
    facts = load_analysis_facts([record["manifest"]], cwd=tmp_path,
        read_roots=[tmp_path, tmp_path / config.analysis.outputs_dir],
        mapped_sources=[layer], display_crs="EPSG:3857")
    assert facts["analyses"][0]["mapped_outputs"][0]["match"] == "derivation"
    assert facts["analyses"][0]["statistics"]["feature_count"] == 2
    with pytest.raises(TypeError):
        helpers["native_layer"](record["ref"], file_path="invented.geojson")
    Path(record["path"]).write_text("changed output", encoding="utf-8")
    assert not any(item["path"] == record["path"] for item in
                   data_references({}, recent_outputs(tmp_path, config)).values())


def test_changed_producer_manifest_invalidates_frozen_ref(tmp_path):
    config = GeoConfig()
    _native(tmp_path, config)
    refs = data_references({}, recent_outputs(tmp_path, config))
    record = next(iter(refs.values()))
    Path(record["manifest"]).write_text("changed manifest", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest is stale"):
        _data_helpers(refs)["resolve_data"](record["ref"])


@pytest.mark.parametrize("helper", ["resolve_data", "read_vector", "native_layer"])
def test_native_ref_detects_content_change_with_unchanged_stat(tmp_path, helper):
    config = GeoConfig()
    _native(tmp_path, config)
    refs = data_references({}, recent_outputs(tmp_path, config))
    record = next(item for item in refs.values() if item.get("output_label") == "result")
    path = Path(record["path"])
    stat = path.stat()
    original = path.read_bytes()
    changed = original.replace(b'"A"', b'"Z"', 1)
    assert original != changed and len(original) == len(changed)
    path.write_bytes(changed)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    if file_signature(path) != record["signature"]:
        pytest.skip("This filesystem changes ctime on writes; stat already detects the change")
    with pytest.raises(ValueError, match="fingerprint changed"):
        _data_helpers(refs)[helper](record["ref"])


def test_empty_context_exposes_actual_data_root_and_discovery(tmp_path):
    config = GeoConfig()
    metadata = {}
    text = model_data_context(metadata, cwd=tmp_path, config=config)
    payload = json.loads(text.split("\n")[-1])
    assert payload["data_refs"] == {} and payload["data_root"] == str(tmp_path.resolve())
    assert "geo_vector_data list/inspect" in text
    assert metadata["_data_refs_snapshot"] == {}


async def test_runtime_tool_loop_shares_inspected_profiles_and_refs(tmp_path, monkeypatch):
    from dataclasses import replace

    from openharness.api.client import ApiMessageCompleteEvent
    from openharness.api.usage import UsageSnapshot
    from openharness.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
    from openharness.engine.stream_events import ToolExecutionCompleted

    from geoharness.runtime import build_geo_runtime

    source = tmp_path / "data/points.geojson"
    _profile(source)

    class Client:
        snapshots = []

        async def stream_message(self, request):
            snapshot = json.loads(self.data_context_provider().split("\n")[-1])
            step = len(self.snapshots)
            self.snapshots.append(snapshot)
            if step == 0:
                block = ToolUseBlock(id="inspect", name="geo_vector_data", input={
                    "action": "inspect", "file_path": str(source)})
            elif step == 1:
                block = ToolUseBlock(id="read", name="geo_python", input={"code": (
                    "import json\nref = next(iter(DATA_REFS))\n"
                    "assert len(read_vector(ref)) == 2\n"
                    "print(json.dumps({'profiles': len(GEO_DATA), 'refs': len(DATA_REFS)}))")})
            else:
                block = TextBlock(text="done")
            yield ApiMessageCompleteEvent(
                message=ConversationMessage(role="assistant", content=[block]),
                usage=UsageSnapshot(), stop_reason="tool_calls" if step < 2 else "stop",
            )

    client = Client()
    monkeypatch.setattr("geoharness.runtime._build_api_client", lambda config: client)
    monkeypatch.setattr("geoharness.runtime._warm_data_layer", lambda catalog: None)
    config = GeoConfig()
    config = replace(config, data=replace(config.data, file_dir=str(source.parent)))
    bundle = await build_geo_runtime(tmp_path, config_override=config)
    bundle.engine._hook_executor = None
    events = [event async for event in bundle.engine.submit_message("inspect then read data")]
    assert client.snapshots[1]["profiles"]
    assert client.snapshots[1]["data_refs"]
    completed = [event for event in events if isinstance(event, ToolExecutionCompleted)]
    assert len(completed) == 2 and all(not event.is_error for event in completed)
    execution = json.loads(completed[1].output)
    assert json.loads(execution["stdout"]) == {"profiles": 1, "refs": 1}


async def test_model_visible_refs_are_passed_unchanged_to_child(tmp_path, monkeypatch):
    profile = _profile(tmp_path / "points.geojson")
    metadata = {"geoharness_config": GeoConfig(),
                "geo_data_context": {"profiles": {"source": profile}}}
    visible = model_data_context(metadata, cwd=tmp_path, config=metadata["geoharness_config"])
    refs = json.loads(visible.split("\n")[-1])["data_refs"]
    assert refs
    captured = {}

    async def execute(**kwargs):
        captured.update(kwargs)
        return {"run_dir": str(tmp_path / "new-run"), "files": [], "exit_code": 0,
                "timed_out": False, "stderr": ""}

    monkeypatch.setattr("geoharness.tools.geo_python.execute_python", execute)
    Path(profile["path"]).write_text("changed after model snapshot", encoding="utf-8")
    result = await GeoPythonTool().execute(GeoPythonInput(code="print(DATA_REFS)"),
        ToolExecutionContext(cwd=tmp_path, metadata=metadata))
    assert not result.is_error
    assert captured["data_refs"] == refs
    with pytest.raises(ValueError, match="stale or missing"):
        _data_helpers(captured["data_refs"])["resolve_data"](next(iter(refs)))


async def test_real_child_helpers_read_transform_and_keep_original_native_path(tmp_path):
    config = GeoConfig()
    manifest = _native(tmp_path, config)
    refs = data_references({}, recent_outputs(tmp_path, config))
    record = next(item for item in refs.values() if item.get("output_label") == "result")
    code = f"""
import json
ref = {record['ref']!r}
assert DATA_DIRS[0] == SOURCE_CWD
assert resolve_data(ref) != RUN_DIR / 'result.geojson'
frame = read_vector(ref, target_crs='EPSG:3857')
assert frame.crs.to_epsg() == 3857 and len(frame) == 2
DATA_REFS[ref]['path'] = str(RUN_DIR / 'fake.geojson')
assert resolve_data(ref).is_file()
layer = native_layer(ref, derivation={{'feature_indices': [0]}}, name='subset')
assert layer['file_path'] == {manifest['paths']['result']['geojson']!r}
assert layer['derivation']['feature_indices'] == [0]
print(json.dumps(layer))
"""
    result = await execute_python(code=code, name="data-refs", cwd=tmp_path, data_dir=None,
        outputs_dir=tmp_path / "runs", timeout_seconds=30, max_output_chars=12000,
        read_dirs=(tmp_path / config.analysis.outputs_dir,), data_refs=refs)
    assert result["exit_code"] == 0, result["stderr"]
    assert json.loads(result["stdout"])["file_path"] == record["path"]


def test_read_vector_rejects_nonvectors_unknown_crs_and_file_changes(tmp_path):
    profile = _profile(tmp_path / "points.geojson")
    refs = data_references({"source": profile}, [])
    record = next(iter(refs.values()))
    corrupted = copy.deepcopy(refs)
    corrupted[record["ref"]]["kind"] = "raster"
    with pytest.raises(ValueError, match="registered vector"):
        _data_helpers(corrupted)["read_vector"](record["ref"])
    with pytest.raises(ValueError, match="registered vector"):
        _data_helpers(corrupted)["native_layer"](record["ref"])
    gpd.GeoDataFrame(geometry=[Point(0, 0)]).to_file(tmp_path / "unknown.gpkg")
    unknown = {"path": str(tmp_path / "unknown.gpkg"), "kind": "vector", "name": "unknown",
               "freshness": "current", "file_stat": file_signature(tmp_path / "unknown.gpkg")}
    refs = data_references({"unknown": unknown}, [])
    with pytest.raises(ValueError, match="source CRS is unknown"):
        _data_helpers(refs)["read_vector"]("unknown", target_crs=3857)


def test_series_crs_and_first_read_root_have_distinct_actionable_hints():
    hints = python_recovery_hints("gdf.loc[0].to_crs(3857)",
                                 "AttributeError: 'Series' object has no attribute 'to_crs'")
    assert "attribute row" in hints[0] and "gdf.geometry GeoSeries" in hints[0]
    assert "Shapely geometry has no CRS" not in hints[0]
    hints = python_recovery_hints("DATA_DIRS[0] / 'points.geojson'", "No such file")
    assert "SOURCE_CWD" in hints[0] and "DATA_REFS" in hints[0]


async def test_qgis_returned_files_do_not_grant_adjacent_or_global_artifacts(tmp_path):
    from PIL import Image

    job = tmp_path / ".geoharness/qgis/job"
    job.mkdir(parents=True)
    image = job / "preview.png"
    Image.new("RGB", (10, 12), "white").save(image)
    project = job / "project.json"
    project.write_text('{"layout": "Map"}', encoding="utf-8")
    other = job / "unreturned.png"
    Image.new("RGB", (1, 1), "red").save(other)
    global_artifact = tmp_path / ".geoharness/tool_artifacts/private.json"
    global_artifact.parent.mkdir()
    global_artifact.write_text('{"private": true}', encoding="utf-8")
    stray = job.parent / "stray-job/outside.png"
    stray.parent.mkdir()
    Image.new("RGB", (1, 1), "blue").save(stray)
    (job / "manifest.json").write_text(json.dumps({"run_dir": str(job)}), encoding="utf-8")
    (job / "result.json").write_text(json.dumps({"status": "succeeded", "result": {
        "preview_path": str(image), "project_manifest": str(project),
        "output_path": str(stray)}}), encoding="utf-8")
    allowed = qgis_diagnostic_files(job.parent)
    assert image in allowed and project in allowed
    assert other not in allowed and stray not in allowed and global_artifact not in allowed
    code = f"""
import json
from pathlib import Path
from PIL import Image
assert Image.open({str(image)!r}).size == (10, 12)
assert json.loads(Path({str(project)!r}).read_text())['layout'] == 'Map'
for target in [{str(other)!r}, {str(stray)!r}, {str(global_artifact)!r}]:
    try: Path(target).read_bytes()
    except PermissionError: pass
    else: raise AssertionError('unreturned file was granted')
for target in [{str(job)!r}, {str(global_artifact.parent)!r}]:
    try: list(Path(target).iterdir())
    except PermissionError: pass
    else: raise AssertionError('directory listing was granted')
for target in [{str(image)!r}, {str(project)!r}]:
    try: Path(target).write_bytes(b'changed')
    except PermissionError: pass
    else: raise AssertionError('returned output write was granted')
print('exact read-only QGIS files only')
"""
    result = await GeoPythonTool().execute(GeoPythonInput(code=code),
        ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()}))
    assert not result.is_error, result.output
