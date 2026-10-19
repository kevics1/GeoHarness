"""Behavior checks for managed execution, pixel identity and native page reuse."""
import json

import pytest
from openharness.tools.base import ToolResult
from pydantic import BaseModel
from test_managed_inputs import setup_context

from geoharness.cartography.composition import composition_from_anchors
from geoharness.cartography.review_contract import review_identity
from geoharness.cartography.review_progress import assess_repair_progress
from geoharness.performance import MeasuredTool
from geoharness.task_store import execution_lock, read_evidence, task_directory


async def test_direct_tools_are_managed_automatically_and_cannot_bypass_schema(tmp_path):
    from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool

    context, _ = setup_context(tmp_path)
    result = await MeasuredTool(GeoPythonTool(), context.metadata).execute(
        GeoPythonInput(code='print(42)'), context)
    assert not result.is_error, result.output
    assert result.metadata['managed_node_id']
    assert list(tmp_path.glob('.geoharness/runs/*/analysis.py'))

    class Input(BaseModel):
        pass

    class Tool:
        name = 'geo_python'

        def is_read_only(self, args):
            return False

        async def execute(self, args, context):
            return ToolResult(output=json.dumps({'stdout': 'x'*5000, 'status': 'succeeded',
                'execution_contract': {'large': 'preserved-contract'}}))
    # Dispatch still validates against the actual registered tool schema.
    result = await MeasuredTool(Tool(), context.metadata, node_id=None).execute(Input(), context)
    assert result.is_error


def test_task_execution_lease_prevents_second_owner_and_releases(tmp_path):
    context, _ = setup_context(tmp_path)
    directory = task_directory(context.metadata, cwd=tmp_path)
    with execution_lock(directory):
        with pytest.raises(ValueError, match='另一执行器'):
            with execution_lock(directory):
                pytest.fail('duplicate execution lease')
    with execution_lock(directory):
        pass
    with pytest.raises(ValueError, match='12000'):
        read_evidence(context.metadata, cwd=tmp_path, limit=200000)


def test_cache_does_not_depend_on_package_version_page_order_or_analysis_paths():
    first = {'backend': 'qgis', 'render_metadata': {'review_page_index': 1,
        'native_facts': {'frames': {'detail_map': {'page_index': 1, 'purpose': '详图'}}},
        'analysis': {'status': 'verified', 'analyses': [{'manifest_path': 'old'}]}}}
    second = json.loads(json.dumps(first))
    second['render_metadata']['review_page_index'] = 2
    second['render_metadata']['native_facts']['frames']['detail_map']['page_index'] = 2
    second['render_metadata']['analysis']['analyses'][0]['manifest_path'] = 'new'
    assert review_identity(first) == review_identity(second)
    second['review_checks'] = ['检查当前新增的单位说明是否可读']
    assert review_identity(first) != review_identity(second)


def test_unmatched_model_finding_is_not_resolved_by_a_changed_response():
    old = {'status': 'issues', 'image_sha256': 'a', 'page_index': 0,
           'issues': [{'severity': 'warning', 'issue_code': 'label_overlap', 'location': '图右侧'}]}
    current = {'status': 'passed', 'image_sha256': 'b', 'page_index': 0, 'issues': []}
    state = assess_repair_progress([old], current)
    assert state['resolved_concern_count'] == 0 and state['unmatched_concern_count'] == 1


def test_real_scale_symbol_overlap_cannot_be_hidden_by_small_label_font():
    anchors = [{'x': x, 'y': 0, 'layer_index': 0, 'feature_id': key,
                'radius_mm': 2, 'symbol_diameter_mm': 5.4}
               for x, key in [(0, 9), (160, 11)]]
    result = composition_from_anchors(anchors, labeled_point_count=2,
        bounds=[0, 0, 3398.1507, 3398.1507], frame_mm=[100, 100])
    assert result['hard_conflict_count'] == 1
    assert result['hard_conflicts'][0]['hard_pairs'][0]['anchor_feature_id'] == 9


def test_coincident_records_are_reported_without_inventing_separation_semantics():
    anchors = [{'x': 0, 'y': 0, 'layer_index': 0, 'feature_id': key,
                'radius_mm': 3, 'symbol_diameter_mm': 3} for key in (1, 2)]
    result = composition_from_anchors(anchors, labeled_point_count=2,
        bounds=[-1, -1, 1, 1], frame_mm=[100, 100])
    assert result['hard_conflict_count'] == 0
    assert result['warnings'][0]['coincident_anchor_pairs'] == 1


def test_page_key_normalizes_only_non_rendering_xml_variations():
    import xml.etree.ElementTree as ET

    from geoharness.qgis.page_cache import _canonical

    first = ET.fromstring('''<page><LayerStyles>
        <LayerStyle id="layer:0"><qgis><elevation color="red"/></qgis></LayerStyle>
        <LayerStyle id="layer:1"/>
        </LayerStyles><LayerSet><Layer>layer:0</Layer><Layer>layer:1</Layer></LayerSet>
        <Option value="&lt;symbol color='blue' size='2'/&gt;"/></page>''')
    second = ET.fromstring('''<page><LayerStyles>
        <LayerStyle id="layer:1"/>
        <LayerStyle id="layer:0"><qgis><elevation color="green"/></qgis></LayerStyle>
        </LayerStyles><LayerSet><Layer>layer:0</Layer><Layer>layer:1</Layer></LayerSet>
        <Option value="&lt;symbol size='2' color='blue'/&gt;"/></page>''')
    assert _canonical(first) == _canonical(second)
    second.find('LayerSet')[0].text = 'layer:1'
    assert _canonical(first) != _canonical(second)  # drawing order still matters


def test_review_checks_survive_checkpoint_and_request_reset():
    from geoharness.cartography.workflow import reset_map_workflow
    from geoharness.session_state import _workflow

    state = {'review_checks': {'map:test': ['检查名单表头单位']}}
    saved = _workflow(state, (), restoring=False)
    restored = _workflow(saved, (), restoring=True)
    metadata = {'map_workflow_state': restored}
    reset_map_workflow(metadata)
    assert metadata['map_workflow_state']['review_checks'] == state['review_checks']
