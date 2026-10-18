import json

from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import ConversationMessage
from openharness.tools.base import ToolResult

from geoharness.config.settings import GeoConfig
from geoharness.session_state import GeoSessionBackend, restore_geo_session_state
from geoharness.task_state import begin_task, observe_operation, task_snapshot


def test_task_outcome_separates_execution_assessment_and_registration_and_restores(
        tmp_path, monkeypatch):
    monkeypatch.setattr('geoharness.session_state.get_config_dir', lambda: tmp_path / 'config')
    config = GeoConfig()
    metadata = {'geoharness_config': config}
    begin_task(metadata, cwd=tmp_path, goal='分析并制图，保留全部对象')
    report = tmp_path / 'report.json'
    report.write_text('{"status":"passed"}', encoding='utf-8')
    observe_operation(metadata, tool='geo_map_review', action=None, result=ToolResult(
        output='report retained but registration failed', is_error=True,
        metadata={'execution_status': 'completed', 'review_status': 'passed',
                  'registration_status': 'failed', 'review_path': str(report)}))
    saved = task_snapshot(metadata, cwd=tmp_path)
    outcome = saved['operations']['geo_map_review:execute']
    assert outcome['execution'] == 'completed'
    assert outcome['assessment'] == 'passed' and outcome['registration'] == 'failed'
    path = GeoSessionBackend().save_snapshot(cwd=tmp_path, model='offline', system_prompt='',
        messages=[ConversationMessage.from_user_text('测试')], usage=UsageSnapshot(),
        tool_metadata=metadata)
    fresh = {'geoharness_config': config}
    assert restore_geo_session_state(fresh, json.loads(path.read_text(encoding='utf-8'))[
        'tool_metadata'], cwd=tmp_path, config=config)
    assert task_snapshot(fresh, cwd=tmp_path) == saved


def test_continuous_shared_record_keeps_references_not_report_bodies(tmp_path):
    metadata = {}
    begin_task(metadata, cwd=tmp_path, goal='检查')
    bucket = metadata['geo_task_record']
    observe_operation(dict(metadata), tool='geo_python', action=None, result=ToolResult(
        output='large raw stdout', metadata={}))
    assert bucket['revision'] == 1
    assert 'large raw stdout' not in json.dumps(bucket)
