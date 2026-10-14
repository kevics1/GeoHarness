"""Local source names remain consistent without guessing ambiguous aliases."""

import copy
import hashlib
from types import SimpleNamespace

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.data.file_loader import FileLoader
from geoharness.data.profiles import file_signature
from geoharness.data.task_context import data_references
from geoharness.execution.runner import _data_helpers
from geoharness.tools.geo_vector_data import GeoVectorDataInput, GeoVectorDataTool


async def _load_path(path, context):
    result = await GeoVectorDataTool().execute(
        GeoVectorDataInput(action="load", file_path=str(path)), context)
    assert not result.is_error, result.output
    return result


def _context(folder):
    loader = FileLoader(str(folder))
    return ToolExecutionContext(cwd=folder, metadata={
        "data_catalog": SimpleNamespace(get_connector=lambda _: loader)})


def _write_layer(path):
    gpd.GeoDataFrame({"县名": ["西昌"]}, geometry=[Point(102, 27)], crs=4326).to_file(
        path, encoding="GBK")


async def test_path_load_registers_stem_and_preserves_encoding_and_original_request(tmp_path):
    path = tmp_path / "西昌市“3·30”森林火灾事件.shp"
    _write_layer(path)
    context = _context(tmp_path)
    result = await _load_path(path, context)
    detail = result.metadata["source_detail"]
    assert detail["name"] == path.stem
    assert detail["requested_name"] == str(path)
    assert detail["path"] == str(path.resolve())
    assert f"Loaded vector file '{path.stem}'" in result.output
    refs = data_references(context.metadata["geo_data_context"]["profiles"], [])
    record = next(iter(refs.values()))
    assert record["name"] == path.stem and record["encoding"] == "gbk"
    assert record["requested_name"] == str(path)
    helpers = _data_helpers(refs)
    for alias in (path.stem, path.name, str(path), record["ref"]):
        assert helpers["read_vector"](alias)["县名"].tolist() == ["西昌"]
    inspected = await GeoVectorDataTool().execute(
        GeoVectorDataInput(action="inspect", file_path=str(path)), context)
    assert not inspected.is_error, inspected.output
    assert inspected.metadata["source_detail"]["name"] == path.stem
    assert f"File: {path.stem}" in inspected.output
    with pytest.raises(ValueError, match="Unknown data ref"):
        helpers["resolve_data"]('西昌市"3·30"森林火灾事件')


async def test_old_path_names_support_unique_aliases_and_still_reject_changed_sidecars(tmp_path):
    path = tmp_path / "事件.shp"
    _write_layer(path)
    context = _context(tmp_path)
    await _load_path(path, context)
    refs = data_references(context.metadata["geo_data_context"]["profiles"], [])
    record = next(iter(refs.values()))
    record["name"] = str(path)
    record.pop("requested_name")
    helpers = _data_helpers(refs)
    assert helpers["read_vector"](path.stem)["县名"].tolist() == ["西昌"]
    assert helpers["resolve_data"](path.name) == path
    record["requested_name"] = "original request alias"
    assert _data_helpers(refs)["resolve_data"]("original request alias") == path
    path.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")
    with pytest.raises(ValueError, match="stale or missing"):
        helpers["read_vector"](path.stem)


async def test_duplicate_stems_require_exact_id_or_path(tmp_path):
    context = _context(tmp_path)
    paths = []
    for folder in ("first", "second"):
        directory = tmp_path / folder
        directory.mkdir()
        path = directory / "事件.shp"
        _write_layer(path)
        paths.append(path)
        await _load_path(path, context)
    refs = data_references(context.metadata["geo_data_context"]["profiles"], [])
    helpers = _data_helpers(refs)
    for alias in ("事件", "事件.shp"):
        with pytest.raises(ValueError, match="Ambiguous data ref"):
            helpers["resolve_data"](alias)
    for ref, record in refs.items():
        assert helpers["resolve_data"](ref) == helpers["resolve_data"](record["path"])
        assert helpers["read_vector"](ref)["县名"].tolist() == ["西昌"]


def test_alias_resolution_keeps_output_hash_and_manifest_checks(tmp_path):
    path = tmp_path / "result.geojson"
    path.write_text("{}", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    record = {"name": str(path), "path": str(path), "kind": "vector",
              "signature": file_signature(path), "manifest": str(manifest),
              "manifest_signature": file_signature(manifest),
              "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    bad_hash = copy.deepcopy(record)
    bad_hash["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="fingerprint changed"):
        _data_helpers({"data:hash": bad_hash})["resolve_data"]("result")
    helpers = _data_helpers({"data:manifest": record})
    assert helpers["resolve_data"]("result") == path
    manifest.write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="manifest is stale"):
        helpers["resolve_data"]("result")
