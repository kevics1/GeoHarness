import json

import geopandas as gpd
from openharness.tools.base import ToolExecutionContext
from shapely.geometry import Point

from geoharness.config.settings import GeoConfig
from geoharness.data.profiles import file_signature, record_profiles
from geoharness.task_state import begin_task
from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool


def setup_context(tmp_path):
    source = tmp_path / 'points.geojson'
    gpd.GeoDataFrame({'id': [1], 'value': [2]}, geometry=[Point(0, 0)], crs=3857).to_file(source)
    context = ToolExecutionContext(cwd=tmp_path, metadata={'geoharness_config': GeoConfig()})
    begin_task(context.metadata, cwd=tmp_path, goal='统计数据')
    record_profiles(context.metadata, [{'path': str(source), 'name': 'points',
        'kind': 'vector', 'crs': 'EPSG:3857', 'feature_count': 1,
        'file_stat': file_signature(source), 'freshness': 'current'}])
    return context, source


async def test_default_managed_tool_binds_literal_source_without_optional_fields(tmp_path):
    context, source = setup_context(tmp_path)
    result = await GeoPythonTool().execute(GeoPythonInput(name='count',
        code="p=read_vector('points')\nprint(len(p))"), context)
    assert not result.is_error, result.output
    body = json.loads(result.output)
    assert list(body['inputs'].values()) == [str(source)]
    assert body['input_versions']
    assert context.metadata['analysis_workflow_state']['count']['status'] == 'succeeded'


async def test_dynamic_input_without_declaration_stops_before_child(tmp_path):
    context, _ = setup_context(tmp_path)
    result = await GeoPythonTool().execute(GeoPythonInput(name='dynamic',
        code="name=input()\np=read_vector(name)"), context)
    assert result.is_error and not result.metadata['script_executed']
    assert 'input_refs' in result.output


async def test_declaring_one_input_does_not_authorize_hidden_business_read(tmp_path):
    context, source = setup_context(tmp_path)
    other = tmp_path / 'other.csv'
    other.write_text('id\n99', encoding='utf-8')
    # An indirect open bypasses static read recognition, but not the managed runtime guard.
    result = await GeoPythonTool().execute(GeoPythonInput(name='hidden',
        input_refs={'source': str(source)}, code=(
            f"reader=open\nprint(reader({str(other)!r}).read())")), context)
    assert result.is_error and '未绑定的业务输入' in result.output
