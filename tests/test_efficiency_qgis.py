import copy
import json

import pytest
from openharness.tools.base import ToolResult
from pydantic import ValidationError

from geoharness.config.settings import GeoConfig
from geoharness.execution.contracts import describe_input
from geoharness.qgis.reuse import recall_render, remember_render, render_key
from geoharness.qgis.specification import contract_help
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput


def test_unambiguous_edit_aliases_are_normalized_and_recorded_without_guessing():
    raw = {"action": "build", "spec_ref": "qgis-spec:actual", "edits": [
        {"operation": "style", "layer": "returned-key", "style": {"color": "#ffffff"}},
        {"operation": "roster", "roster": None},
        {"operation": "frame", "frame": "main", "settings": {
            "purpose": "总览", "requirements": [{"layer_index": "returned-key"}],
            "extent": {"xmin": 0, "ymin": 0, "xmax": 10, "ymax": 10}}}]}
    before = copy.deepcopy(raw)
    parsed = GeoQgisProjectInput.model_validate(raw)
    assert raw == before and len(parsed._edit_normalizations) == 3
    assert parsed.edits[0].layer_key == "returned-key"
    assert parsed.edits[2].extent.xmax == 10
    raw["edits"][0]["layer_key"] = "another"
    with pytest.raises(ValidationError, match="both layer and layer_key"):
        GeoQgisProjectInput.model_validate(raw)


def test_edit_help_examples_validate_against_actual_contract():
    for edit in contract_help("edits")["examples"].values():
        GeoQgisProjectInput(action="build", spec_ref="qgis-spec:actual", edits=[edit])


def test_render_cache_binds_data_and_rechecks_every_packaged_file(tmp_path):
    source = tmp_path / "layer.gpkg"
    source.write_bytes(b"real bytes")
    manifest = tmp_path / "project.json"
    manifest.write_text(json.dumps({"files": ["layer.gpkg"]}), encoding="utf-8")
    result = ToolResult(output='{"status":"succeeded"}', metadata={
        "project_manifest": str(manifest), "render_executed": True})
    state = {}
    remember_render(state, "actual-key", result, limit=8)
    cached = recall_render(state, "actual-key")
    assert cached.metadata["reused"] and not cached.metadata["render_executed"]
    assert result.metadata["render_executed"]  # Original result is untouched.
    result.metadata['map_receipt'] = {'selected_version': 1}
    remember_render(state, 'versioned', result, limit=8)
    assert recall_render(state, 'versioned', current_version=2) is None
    assert recall_render(state, 'versioned', current_version=1) is not None
    source.write_bytes(b"changed bytes")
    assert recall_render(state, "actual-key") is None
    payload = {"layers": [{"file_path": "random-temporary-path", "name": "source"}],
               "_layout_sources": [{"source": "actual-source", "data_sha256": "a" * 64}]}
    key = render_key(payload, GeoConfig())
    payload["layers"][0]["file_path"] = "different-temporary-path"
    assert render_key(payload, GeoConfig()) == key
    payload["_layout_sources"][0]["data_sha256"] = "b" * 64
    assert render_key(payload, GeoConfig()) != key
    payload["layout"] = {"font_file": "external.ttf"}
    assert render_key(payload, GeoConfig()) is None


def test_describe_input_measures_intermediate_types_shapes_and_nulls():
    import numpy as np
    import pandas as pd

    table = pd.DataFrame({"numeric_text": ["2", None]})
    actual = describe_input(table)
    assert actual["rows"] == 2 and actual["null_counts"] == {"numeric_text": 1}
    actual = describe_input(np.array([[1., float("inf")]]))
    assert actual["shape"] == [1, 2] and actual["nonfinite_count"] == 1
    assert describe_input(pd.DataFrame({'crs': ['text'], 'geometry': ['text']}))['kind'] == 'table'
