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
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput, GeoQgisProjectTool

    registry = build_geo_tool_registry(config=GeoConfig())
    # The model-facing QGIS project tool is read-only inspect; the compiler's
    # full progressive schema is still exercised directly, never re-exposed as
    # a cartography entry point.
    assert registry.get('geo_qgis_project').input_model.__name__ == 'QgisInspectInput'
    tool = GeoQgisProjectTool()
    assert tool.input_model is GeoQgisProjectInput
    full = tool.input_model.model_json_schema()
    wire = tool.to_api_schema()['input_schema']
    assert set(wire['properties']) == set(full['properties'])
    assert len(json.dumps(wire)) < len(json.dumps(full)) / 2
    assert contract_help('layout')['schema']['properties']['width_mm']['description']
    assert contract_help('style')['schema']['properties']['size_mm']['description']
    cartography = registry.get('geo_cartography')
    # Native scene fields are part of the model-facing contract; they must not
    # be compressed away. Keep the compactness check only for the progressive
    # QGIS compiler schema, whose nested contracts are served by help.
    assert set(cartography.to_api_schema()['input_schema']['properties']) == {
        'action', 'scene', 'scene_ref', 'edits'}
    assert len(json.dumps(full)) > len(json.dumps(wire))


def test_native_wire_schema_contains_detailed_fields_and_no_dangling_refs():
    from geoharness.tools.geo_native_map import GeoNativeMapTool

    schema = GeoNativeMapTool().to_api_schema()['input_schema']
    def visit(node):
        if isinstance(node, dict):
            if '$ref' in node:
                target = schema
                for part in node['$ref'].removeprefix('#/').split('/'):
                    target = target[part]
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    assert schema['$defs']['Layer']['properties']['select_keys']['description']
    assert schema['$defs']['Symbol']['properties']['width_mm']['description']
