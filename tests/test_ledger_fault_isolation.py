"""An unavailable map ledger must not erase artifacts or become a false quality pass."""
import json
from pathlib import Path

from openharness.tools.base import ToolExecutionContext, ToolResult
from PIL import Image

from geoharness.cartography import jobs, tracking  # noqa: F401 (bind before failure injection)
from geoharness.config.settings import GeoConfig


async def test_failed_draft_registration_preserves_successful_native_result(tmp_path, monkeypatch):
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    path = tmp_path / "rendered.png"
    import geopandas as gpd
    from shapely.geometry import Point

    source = tmp_path / "input.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs=3857).to_file(source)
    async def create(*args, **kwargs):
        path.write_bytes(b"native result")
        return ToolResult(output="created", metadata={"preview_path": str(path)})
    def fail(*args, **kwargs):
        raise ValueError("injected ledger failure")
    # Exercise the real mutation wrapper around a native result, not its helper.
    monkeypatch.setattr(GeoQgisProjectTool, "_execute", create)
    monkeypatch.setattr(jobs, "record_map_draft", fail)
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(action="create",
        layers=[{"file_path": str(source)}]), context)
    assert result.is_error
    assert result.metadata["render_executed"]
    assert result.metadata["preview_path"] == str(path)
    assert path.is_file()


async def test_selected_draft_can_be_copied_when_ledger_is_unreadable(tmp_path, monkeypatch):
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    path = tmp_path / "preview.png"
    Image.new("RGB", (8, 8), "red").save(path)
    def fail(*args, **kwargs):
        raise ValueError("injected ledger failure")
    monkeypatch.setattr(jobs, "map_job_receipt", fail)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    tool = GeoDeliverTool()
    denied = await tool.execute(GeoDeliverInput(primary_map=str(path)), context)
    assert denied.is_error
    delivered = await tool.execute(GeoDeliverInput(primary_map=str(path),
        version_policy="selected"), context)
    assert not delivered.is_error, delivered.output
    assert delivered.metadata["quality_status"] == "draft"
    assert delivered.metadata["ledger_errors"]
    assert Path(delivered.metadata["primary_map"]).is_file()


async def test_ledger_health_does_not_need_to_parse_oversized_history(tmp_path):
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": config})
    identity = jobs.ensure_map_job(context.metadata, cwd=tmp_path, config=config)
    receipt = tmp_path / config.cartography.jobs_dir / identity / "receipt.json"
    receipt.write_text(" " * (16*1024*1024 + 1), encoding="utf-8")
    result = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action="inspect", map_id=identity), context)
    assert not result.is_error, result.output
    assert json.loads(result.output)["requires_recovery"]


async def test_review_only_retains_pixel_report_without_a_healthy_ledger(tmp_path, monkeypatch):
    from test_map_receipt_workflow import PassingReviewer

    from geoharness.tools.geo_map_review import GeoMapReviewInput, GeoMapReviewTool

    image = tmp_path / "preview.png"
    Image.new("RGB", (32, 32), "blue").save(image)
    def fail(*args, **kwargs):
        raise ValueError("injected ledger failure")
    monkeypatch.setattr(jobs, "map_job_receipt", fail)
    client = PassingReviewer()
    context = ToolExecutionContext(cwd=tmp_path, metadata={
        "geoharness_config": GeoConfig(), "map_review_client": client})
    result = await GeoMapReviewTool().execute(GeoMapReviewInput(
        file_path=str(image), task="Review actual pixels", auto_repair=False), context)
    assert not result.is_error, result.output
    assert result.metadata["receipt_status"] == "unregistered"
    assert result.metadata["quality_status"] == "draft"
    assert Path(result.metadata["review_path"]).is_file()
    assert len(client.requests) == 1


async def test_cleanup_failure_keeps_original_exception_and_releases_owner(tmp_path, monkeypatch):
    import pytest

    from geoharness.cartography.mutation_control import (
        current_owner,
        map_mutation,
        mutation_active,
    )

    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    identity = jobs.ensure_map_job(context.metadata, cwd=tmp_path, config=GeoConfig())
    original = jobs.record_map_mutation
    def record(*args, **kwargs):
        if kwargs['value']['status'] != 'running':
            raise ValueError('injected cleanup failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(jobs, 'record_map_mutation', record)
    with pytest.raises(RuntimeError, match='original native failure'):
        async with map_mutation(context, map_id=identity, operation='test'):
            raise RuntimeError('original native failure')
    assert current_owner() is None
    assert not mutation_active(tmp_path, identity)


async def test_recovery_waits_for_live_owner_and_malformed_legacy_is_structured(tmp_path):
    from geoharness.cartography.mutation_control import map_mutation
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': config})
    identity = jobs.ensure_map_job(context.metadata, cwd=tmp_path, config=config)
    async with map_mutation(context, map_id=identity, operation='test'):
        busy = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
            action='recover', map_id=identity), context)
        assert busy.is_error and 'active' in busy.output
    receipt = tmp_path/config.cartography.jobs_dir/identity/'receipt.json'
    receipt.write_text('[]', encoding='utf-8')
    invalid = await GeoQgisProjectTool().execute(GeoQgisProjectInput(
        action='recover', map_id=identity), context)
    assert invalid.is_error and 'identities' in invalid.output
    assert receipt.read_text(encoding='utf-8') == '[]'


async def test_delivery_registration_failure_preserves_copied_manifest(tmp_path, monkeypatch):
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    path = tmp_path/'preview.png'
    Image.new('RGB', (8, 8), 'red').save(path)
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    identity = jobs.ensure_map_job(context.metadata, cwd=tmp_path, config=GeoConfig(),
                                    artifact_path=path)
    def fail(*args, **kwargs):
        raise ValueError('injected registration failure after copying')
    monkeypatch.setattr(jobs, 'record_map_delivery', fail)
    result = await GeoDeliverTool().execute(GeoDeliverInput(map_id=identity,
        primary_map=str(path), version_policy='selected'), context)
    assert not result.is_error, result.output
    assert result.metadata['quality_status'] == 'draft'
    assert result.metadata['receipt_status'] == 'failed'
    assert result.metadata['delivery_status'] == 'copied_unregistered'
    assert 'delivery files exist' in result.metadata['next_action']
    assert 'receipt_path' not in result.metadata
    assert result.metadata['map_receipt']['quality_status'] == 'draft'
    assert Path(result.metadata['primary_map']).is_file()
    manifest = json.loads(Path(result.metadata['manifest_path']).read_text(encoding='utf-8'))
    assert 'injected registration failure' in manifest['ledger_errors'][0]


async def test_health_read_calls_preserve_history_window_and_root_pagination(tmp_path):
    from geoharness.cartography import ledger_store
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

    config = GeoConfig()
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': config})
    identity = jobs.ensure_map_job(context.metadata, cwd=tmp_path, config=config)
    directory = tmp_path/config.cartography.jobs_dir
    receipt = directory/identity/'receipt.json'
    job = jobs._job(directory, identity)
    job.update(current_version=12, versions=[{'version': i+1,
        'image_path': f'actual-{i+1}/' + '路径'*1500 + '/preview.png', 'stages': {}}
        for i in range(12)])
    ledger_store.write(receipt, job)
    before = receipt.read_bytes()
    tool = GeoQgisProjectTool()

    async def read(call):
        result = await tool.execute(GeoQgisProjectInput(**call), context)
        assert not result.is_error, result.output
        assert len(result.output) <= config.qgis.inspection_max_chars
        return json.loads(result.output)['diagnostic']

    page = await read({'action': 'inspect', 'map_id': identity, 'offset': 3, 'limit': 3})
    for _ in range(8):
        if 'history' in page['value']:
            history = page['value']['history']
            break
        assert page['next_call']['history_offset'] == 3
        assert page['next_call']['history_limit'] == 3
        page = await read(page['next_call'])
    else:
        raise AssertionError('Root diagnostic pagination did not reach history')
    page = await read(history['read_call'])
    record = await read(page['value'][0]['read_call'])
    assert record['value']['version'] == 4
    image = await read(record['value']['image_path']['read_call'])
    text = image['value']
    while image.get('next_call'):
        image = await read(image['next_call'])
        text += image['value']
    assert text == job['versions'][3]['image_path']
    assert receipt.read_bytes() == before


async def test_delivery_postcopy_read_failure_returns_copied_unregistered_files(
    tmp_path, monkeypatch,
):
    from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool

    path = tmp_path / "preview.png"
    Image.new("RGB", (8, 8), "red").save(path)
    context = ToolExecutionContext(cwd=tmp_path, metadata={"geoharness_config": GeoConfig()})
    original = jobs.map_job_receipt
    reads = 0

    def read(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 3:
            raise ValueError("injected postcopy receipt read failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(jobs, "map_job_receipt", read)
    result = await GeoDeliverTool().execute(GeoDeliverInput(primary_map=str(path)), context)
    assert reads == 3
    assert not result.is_error, result.output
    assert result.metadata["delivery_status"] == "copied_unregistered"
    assert result.metadata["receipt_status"] == "failed"
    assert result.metadata["quality_status"] == "draft"
    assert "receipt_path" not in result.metadata
    assert Path(result.metadata["primary_map"]).read_bytes() == path.read_bytes()
    manifest = json.loads(Path(result.metadata["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["ledger_errors"] == ["injected postcopy receipt read failure"]
    assert manifest["map_receipt"]["quality_status"] == "draft"
