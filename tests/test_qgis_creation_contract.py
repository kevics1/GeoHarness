"""Repair reconstruction starts with original notes/strategy and immutable source bindings."""

from pathlib import Path

import geopandas as gpd
import pytest
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig
from geoharness.qgis.project_worker import _layout_input_digest
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def test_creation_capsule_preserves_raw_notes_focus_and_original_derivation(
    tmp_path, monkeypatch,
):
    path = tmp_path / "input.geojson"
    gpd.GeoDataFrame({"name": ["A", "B"]}, geometry=[Point(1000, 1000), Point(2000, 2000)],
                     crs=3857).to_file(path)
    captured = []
    names = []

    async def runner(action, payload, **kwargs):
        captured.append(payload)
        layer = payload["layers"][0]
        names.extend(gpd.read_file(layer["file_path"], layer=layer["layer_name"])["name"])
        return {"status": "succeeded", "result": {}}

    monkeypatch.setattr("geoharness.qgis.runner.execute_qgis", runner)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="create", layers=[{"file_path": str(path), "derivation": {
            "feature_indices": [1], "keep_fields": ["name"]}, "style": {
                "renderer": "categorized", "field": "name", "categories": [
                    {"value": "B", "label": "B", "color": "#ff0000"},
                    {"value": None, "label": "Other / NULL", "color": "#ffffff"}]}}],
        layout={"main_frame": {"focus_layers": [0]}, "footer_text": "用户原备注"},
    ), context)
    assert not result.is_error, result.output
    payload = captured[0]
    capsule = payload["_creation_spec"]
    assert capsule["parameters"]["layout"]["footer_text"] == "用户原备注"
    assert capsule["parameters"]["layout"]["main_frame"]["focus_layers"] == [0]
    assert capsule["parameters"]["layers"][0]["style"]["categories"][1]["value"] is None
    assert "map_extent" not in capsule["parameters"]
    assert capsule["source_bindings"][0]["file_path"] == str(path)
    assert capsule["source_bindings"][0]["derivation"]["feature_indices"] == [1]
    spec = payload["layers"][0]
    assert names == ["B"]
    without = {key: value for key, value in payload.items() if key != "_creation_spec"}
    assert _layout_input_digest(without) == _layout_input_digest(payload)
    assert not Path(spec["file_path"]).exists()


async def test_native_long_source_notes_are_retained_without_dense_footer(tmp_path):
    import json

    from geoharness.qgis.discovery import discover_qgis

    config = GeoConfig()
    try:
        discover_qgis(config.qgis, cwd=tmp_path)
    except (ValueError, OSError):
        pytest.skip("QGIS runtime unavailable")
    source = tmp_path / "points.geojson"
    gpd.GeoDataFrame(geometry=[Point(1000, 1000), Point(2000, 2000)],
                     crs=3857).to_file(source)
    notes = "来源：原始应急数据；时间：2026-10-13\n" + "完整说明与方法需要保留。\n" * 50
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    rendered = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="create", layers=[{"file_path": str(source)}],
        title="来源备注保留验证", layout={"footer_text": notes},
    ), context)
    assert not rendered.is_error, rendered.output
    manifest = json.loads(Path(rendered.metadata["project_manifest"]).read_text(encoding="utf-8"))
    assert manifest["analysis"]["source_notes"] == notes
    assert manifest["analysis"]["status"] == "none"
    assert len(manifest["layout"]["footer_text"].splitlines()) <= 3
    assert "2026-10-13" in manifest["layout"]["footer_text"]
    assert manifest["layout"]["bounds_check"]["valid"]
    assert manifest["creation_spec"]["parameters"]["layout"]["footer_text"] == notes


@pytest.mark.parametrize("layers", [
    '[{"file_path":"first.geojson","file_path":"second.geojson"}]',
    '[{"file_path":"data.geojson","style":{"renderer":"categorized",'
    '"field":"risk","categories":[{"value":NaN,"color":"#ffffff"}]}}]',
    '[{"file_path":"data.geojson","style":{"opacity":Infinity}}]',
])
def test_layers_json_rejects_ambiguous_and_nonstandard_values(layers):
    with pytest.raises(ValueError, match="duplicate key|cannot contain"):
        GeoQgisProjectInput(action="create", layers=layers)
