import json
from pathlib import Path

from test_native_scene_workflow import setup

from geoharness.cartography.scene import Coverage, Layer, Page
from geoharness.tools.geo_native_map import GeoNativeMapTool, NativeMapInput


async def test_table_pagination_uses_actual_rows_and_wraps_footer(tmp_path):
    context, scene = setup(tmp_path)
    scene.pages[1].footer = ('距离单位m，{row_start}至{row_end}，{page_number}/{page_count}。'
                             + '来源已核对。' * 18)
    result = await GeoNativeMapTool().execute(NativeMapInput(action='render', scene=scene), context)
    assert not result.is_error, result.output
    plan = json.loads((Path(result.metadata['project_dir']) / 'scene-plan.json').read_text('utf-8'))
    roster = [page for page in plan['pages'] if page['role'] == 'roster']
    assert len(roster) == 2
    assert any('本页第1—2行' in t['text'] for t in roster[0]['texts'])
    assert any('本页第3—3行' in t['text'] for t in roster[1]['texts'])
    assert any('\n' in t['text'] for t in roster[0]['texts'])
    assert not [p for page in roster for p in page['problems'] if p['code'] == 'text_overflow']
    wrong = await GeoNativeMapTool().execute(NativeMapInput(action='render',
        scene_ref=result.metadata['scene_ref'], edits=[{'target': 'page', 'id': 'roster',
            'changes': {'footer': '本页1—2行'}}]), context)
    assert not wrong.is_error, wrong.output
    assert any(p['code'] == 'pagination_footer_mismatch'
               for p in wrong.metadata['layout']['bounds_check']['problems'])


async def test_explicit_coverage_catches_missing_entities_and_legend_can_hide_overlay(tmp_path):
    context, scene = setup(tmp_path)
    scene.layers.append(Layer.model_validate({**scene.layers[0].model_dump(),
        'id': 'top', 'select_keys': [2], 'show_in_legend': False}))
    page = scene.pages[0]
    page.layers = ['top']
    page.title = '重点对象{subject_count}处'
    page.coverage = [Coverage(layer='places', drawn_layers=['top'])]
    result = await GeoNativeMapTool().execute(NativeMapInput(action='render', scene=scene), context)
    assert not result.is_error, result.output
    plan = json.loads((Path(result.metadata['project_dir']) / 'scene-plan.json').read_text('utf-8'))
    overview = plan['pages'][0]
    assert overview['coverage'][0]['expected_count'] == 3
    assert overview['coverage'][0]['drawn_count'] == 1
    assert overview['coverage'][0]['missing_keys'] == ['8', '6']
    assert any(p['code'] == 'subject_coverage_missing' for p in overview['problems'])
    assert any(t['text'] == '重点对象3处' for t in overview['texts'])
    assert not overview['legend_items']
    # A hidden expectation layer also affects this page's cache identity.
    changed = await GeoNativeMapTool().execute(NativeMapInput(action='render',
        scene_ref=result.metadata['scene_ref'], edits=[{'target': 'layer', 'id': 'places',
            'changes': {'select_keys': [2]}}]), context)
    assert not changed.is_error, changed.output
    plan = json.loads((Path(changed.metadata['project_dir']) / 'scene-plan.json').read_text(
        'utf-8'))
    assert plan['pages'][0]['coverage'][0]['expected_count'] == 1
    assert not plan['pages'][0]['coverage'][0]['missing_keys']


async def test_detail_numeric_promise_requires_explicit_subject_contract(tmp_path):
    context, scene = setup(tmp_path)
    scene.pages.insert(1, Page(id='detail', role='detail', title='35处重点对象', layers=['places']))
    native = GeoNativeMapTool()
    first = await native.execute(NativeMapInput(action='render', scene=scene), context)
    assert not first.is_error, first.output
    assert any(p['code'] == 'subject_coverage_contract_missing'
               for p in first.metadata['layout']['bounds_check']['problems'])
    second = await native.execute(NativeMapInput(action='render',
        scene_ref=first.metadata['scene_ref'],
        edits=[{'target': 'page', 'id': 'detail', 'changes': {'coverage': [
            {'layer': 'places', 'drawn_layers': ['places']}]}}]), context)
    assert not second.is_error, second.output
    assert any(p['code'] == 'title_count_mismatch'
               for p in second.metadata['layout']['bounds_check']['problems'])
