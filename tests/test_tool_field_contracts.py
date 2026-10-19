"""Public model fields must have semantics, including nested data contracts."""

import re

import pytest

from geoharness.config.settings import GeoConfig
from geoharness.tools import build_geo_tool_registry


@pytest.mark.parametrize('tool', [tool for tool in build_geo_tool_registry(
    config=GeoConfig()).list_tools() if tool.name.startswith('geo_')], ids=lambda tool: tool.name)
def test_every_domain_input_field_has_help_and_examples_validate(tool):
    schema = tool.input_model.model_json_schema()
    assert re.search('[\u4e00-\u9fff]', tool.description), tool.name
    for name, model in [('input', schema), *schema.get('$defs', {}).items()]:
        for field, definition in model.get('properties', {}).items():
            assert definition.get('description'), f'{tool.name}.{name}.{field} lacks field meaning'
            assert re.search('[\u4e00-\u9fff]', definition['description']), (
                f'{tool.name}.{name}.{field} lacks Chinese guidance')
    for example in schema.get('examples', []):
        tool.input_model.model_validate(example)


def test_progressive_schema_preserves_exact_field_help_without_duplicate_nested_manuals():
    import json

    from geoharness.qgis.specification import contract_help

    registry = build_geo_tool_registry(config=GeoConfig())
    tool = registry.get('geo_qgis_project')
    full = tool.input_model.model_json_schema()
    wire = tool.to_api_schema()['input_schema']
    assert set(wire['properties']) == set(full['properties'])
    assert len(json.dumps(wire)) < len(json.dumps(full)) / 2
    assert contract_help('layout')['schema']['properties']['width_mm']['description']
    assert contract_help('style')['schema']['properties']['size_mm']['description']
    cartography = registry.get('geo_cartography')
    assert len(json.dumps(cartography.to_api_schema())) < len(json.dumps(
        cartography.input_model.model_json_schema()))
