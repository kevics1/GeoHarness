"""Per-layer encoding survives loading, model context and Python helpers."""

import json
from types import SimpleNamespace

import geopandas as gpd
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.data.encoding import source_encoding
from geoharness.data.file_loader import FileLoader
from geoharness.data.task_context import data_references
from geoharness.execution.runner import _data_helpers
from geoharness.tools.geo_vector_data import GeoVectorDataInput, GeoVectorDataTool


async def test_two_shapefiles_keep_distinct_declared_encodings(tmp_path):
    for name, encoding in (("legacy", "GBK"), ("modern", "UTF-8")):
        gpd.GeoDataFrame({"县名": ["西昌"]}, geometry=[Point(102, 27)], crs=4326).to_file(
            tmp_path / f"{name}.shp", encoding=encoding)
    loader = FileLoader(str(tmp_path))
    context = ToolExecutionContext(cwd=tmp_path, metadata={
        "data_catalog": SimpleNamespace(get_connector=lambda _: loader)})
    tool = GeoVectorDataTool()
    for name, encoding in (("legacy", "gbk"), ("modern", "utf-8")):
        result = await tool.execute(GeoVectorDataInput(action="load", name=name), context)
        assert not result.is_error, result.output
        assert f"Encoding: {encoding}" in result.output
        assert result.metadata["source_detail"]["encoding_source"] == "cpg"
    refs = data_references(context.metadata["geo_data_context"]["profiles"], [])
    helper = _data_helpers(refs)["read_vector"]
    assert helper("legacy")["县名"].tolist() == ["西昌"]
    assert helper("modern")["县名"].tolist() == ["西昌"]
    assert {r["encoding"] for r in refs.values()} == {"gbk", "utf-8"}
    json.dumps(refs, allow_nan=False)


def test_missing_invalid_and_numeric_cpg_declarations(tmp_path):
    path = tmp_path / "layer.shp"
    assert source_encoding(path)["encoding_status"] == "unknown"
    path.with_suffix(".cpg").write_text("not-a-codec", encoding="ascii")
    assert source_encoding(path)["encoding_status"] == "unknown"
    path.with_suffix(".cpg").write_text("65001", encoding="ascii")
    assert source_encoding(path)["encoding"] == "utf-8"
