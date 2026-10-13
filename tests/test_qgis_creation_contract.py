"""Repair reconstruction starts with original notes/strategy and immutable source bindings."""

from pathlib import Path

import geopandas as gpd
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
