"""Short build validates/exports once and preserves revision/coverage protection."""

import json
from pathlib import Path

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.config.settings import GeoConfig
from geoharness.qgis import runner
from geoharness.qgis.specification import load_spec
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def test_build_edits_uses_one_real_native_job_and_preserves_draft(tmp_path, monkeypatch):
    import geopandas as gpd
    from shapely.geometry import box

    source = tmp_path / "subject.geojson"
    gpd.GeoDataFrame({"name": ["Subject"]}, geometry=[box(1000, 1000, 2000, 2000)],
                     crs=3857).to_file(source)
    cfg = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": cfg})
    native = runner.execute_qgis
    actions = []

    async def counted(action, *args, **kwargs):
        actions.append(action)
        return await native(action, *args, **kwargs)

    monkeypatch.setattr(runner, "execute_qgis", counted)
    tool = GeoQgisProjectTool()
    draft = await tool.execute(GeoQgisProjectInput(action="spec", title="Original", dpi=72,
        crs="EPSG:3857", layers=[{"file_path": str(source), "name": "Subject"}],
        layout={"legend": False, "scale_bar": False}), context)
    assert not draft.is_error, draft.output
    ref = draft.metadata["spec_ref"]
    assert json.loads(draft.output)["build_call"]["spec_ref"] == ref
    built = await tool.execute(GeoQgisProjectInput(action="build", spec_ref=ref,
        patches=[{"path": ["title"], "value": "Edited"}]), context)
    assert not built.is_error, built.output
    assert actions == ["project_create"]
    assert Path(built.metadata["preview_path"]).is_file()
    updated = built.metadata["latest_spec_ref"]
    assert built.metadata["spec_ref"] == updated
    assert load_spec(updated, cwd=tmp_path, settings=cfg.qgis)["title"] == "Edited"
    assert built.metadata["preflight"]["status"] == "passed"
    assert built.metadata["map_receipt"]["quality_status"] != "passed"
    rejected = await tool.execute(GeoQgisProjectInput(action="build", spec_ref=ref), context)
    assert rejected.is_error and rejected.metadata["mutation_conflict"]
    assert actions == ["project_create"]


def test_build_rejects_selectors_and_extra_fields_before_execution():
    from pydantic import ValidationError

    for fields in ({"title": "Cannot override reference"}, {"detail_path": ["layers"]}):
        with pytest.raises(ValidationError):
            GeoQgisProjectInput(action="build", spec_ref="qgis-spec:example", **fields)
