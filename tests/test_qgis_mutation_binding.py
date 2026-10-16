"""Prepared plans cannot overwrite a newer map, including calls from fresh contexts."""

import asyncio
import json
from threading import Event

import pytest
from openharness.tools.base import ToolExecutionContext

from geoharness.cartography.jobs import ensure_map_job, record_map_version
from geoharness.config.settings import GeoConfig
from geoharness.qgis.prepared import save_preflight
from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool


async def test_public_create_rejects_stale_plan_before_native_execution(tmp_path, monkeypatch):
    from geoharness.cartography.mutation_control import plan_binding

    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    job = ensure_map_job(context.metadata, cwd=tmp_path, config=config, map_id="business-map")
    ref, _ = save_preflight({"map_id": job, "layers": [{"file_path": "unused.geojson"}]},
                           {"status": "passed"}, cwd=tmp_path, settings=config.qgis,
                           binding=plan_binding(context, map_id=job))
    image = tmp_path / "new.png"
    image.write_bytes(b"new map version")
    record_map_version(job, context.metadata, cwd=tmp_path, config=config, image_path=image,
                       preflight=None, layout=None, project_dir=None)
    called = []

    async def native(*args, **kwargs):
        called.append(True)
        raise AssertionError("A stale plan reached native execution")

    monkeypatch.setattr(GeoQgisProjectTool, "_execute", native)
    fresh_context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="create", plan_ref=ref), fresh_context)
    assert result.is_error and not called
    assert result.metadata["mutation_conflict"]
    assert result.metadata["latest_version"] == 1
    assert "stale" in result.output.lower()


async def test_new_draft_carries_persistent_job_identity(tmp_path):
    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="spec", title="Map with editable state"), context)
    assert not result.is_error, result.output
    saved = json.loads(result.output)
    read = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="inspect", spec_ref=saved["spec_ref"]), ToolExecutionContext(
            cwd=tmp_path, metadata={"geoharness_config": config}))
    assert json.loads(read.output)["parameters"]["map_id"]


async def test_editing_the_draft_invalidates_older_specs_even_before_rendering(tmp_path):
    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    tool = GeoQgisProjectTool()
    first = await tool.execute(GeoQgisProjectInput(action="spec", title="A"), context)
    old_ref = first.metadata["spec_ref"]
    edited = await tool.execute(GeoQgisProjectInput(action="spec", spec_ref=old_ref,
        patches=[{"path": ["title"], "value": "B"}]), context)
    assert not edited.is_error, edited.output
    stale = await tool.execute(GeoQgisProjectInput(action="preflight", spec_ref=old_ref), context)
    assert stale.is_error and stale.metadata["mutation_conflict"]
    assert stale.metadata["latest_spec_ref"] == edited.metadata["spec_ref"]
    history = await tool.execute(GeoQgisProjectInput(action="inspect", spec_ref=old_ref), context)
    assert not history.is_error


async def test_cancel_waits_for_registration_thread_before_releasing_map(tmp_path, monkeypatch):
    import geoharness.cartography.tracking as tracking
    import geoharness.qgis.runner as runner

    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    job = ensure_map_job(context.metadata, cwd=tmp_path, config=config, map_id="map")
    entered, release = Event(), Event()

    async def native(*args, **kwargs):
        return {"status": "succeeded", "result": {}}

    def register(result, *args, **kwargs):
        entered.set()
        release.wait(10)
        return result

    monkeypatch.setattr(runner, "execute_qgis", native)
    monkeypatch.setattr(tracking, "track_render_result", register)
    task = asyncio.create_task(GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="export", project_path="saved.qgs", map_id=job), context))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        blocked = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
            action="spec", map_id=job, title="concurrent"), context)
        assert blocked.is_error and blocked.metadata["mutation_conflict"]
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
