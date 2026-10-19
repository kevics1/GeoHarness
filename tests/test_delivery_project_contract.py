import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from openharness.tools.base import ToolExecutionContext
from test_delivery import _script_project
from test_managed_inputs import setup_context

from geoharness.config.settings import GeoConfig
from geoharness.tools.geo_deliver import GeoDeliverInput, GeoDeliverTool


async def test_bare_map_files_cannot_silently_deliver_without_editable_project(tmp_path):
    from PIL import Image

    image = tmp_path / 'map.png'
    Image.new('RGB', (10, 10), 'red').save(image)
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    missing = await GeoDeliverTool().execute(GeoDeliverInput(files=[str(image)]), context)
    assert missing.is_error and json.loads(missing.output)['code'] == 'missing_project'
    bypass = await GeoDeliverTool().execute(GeoDeliverInput(primary_map=str(image),
                                            allow_report_only=True), context)
    assert bypass.is_error


async def test_delivery_materializes_explicit_spec_before_packaging(tmp_path, monkeypatch):
    image, project = _script_project(tmp_path)
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    calls = []

    async def materialize(context, spec_ref):
        calls.append(spec_ref)
        return {'project_dir': project['project_dir']}

    monkeypatch.setattr('geoharness.cartography.delivery_project.materialize_project', materialize)
    result = await GeoDeliverTool().execute(GeoDeliverInput(
        spec_ref='qgis-spec:actual-returned-reference', version_policy='selected'), context)
    assert not result.is_error, result.output
    assert calls == ['qgis-spec:actual-returned-reference']
    assert Path(result.metadata['qgis_project']).is_file()
    assert image.is_file()


async def test_required_analysis_cannot_bypass_published_facts_through_legacy_project(tmp_path):
    context, _ = setup_context(tmp_path)
    context.metadata['geo_task_record']['analysis_requirement'] = 'required'
    image, project = _script_project(tmp_path)
    result = await GeoDeliverTool().execute(GeoDeliverInput(primary_map=str(image),
        project_dir=project['project_dir'], version_policy='selected'), context)
    assert result.is_error
    assert json.loads(result.output)['code'] == 'missing_analysis_package'


async def test_delivery_project_generation_obeys_native_tool_permission(tmp_path):
    from geoharness.cartography.delivery_project import materialize_project

    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig(),
        'geoharness_permission_checker': SimpleNamespace(evaluate=lambda *a, **k:
            SimpleNamespace(allowed=False, requires_confirmation=False))})
    with pytest.raises(ValueError, match='权限未授权'):
        await materialize_project(context, 'qgis-spec:' + 'a' * 64)
