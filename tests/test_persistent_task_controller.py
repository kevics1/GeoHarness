import json
from pathlib import Path

import pytest
from openharness.tools.base import ToolResult
from pydantic import BaseModel
from test_managed_inputs import setup_context

from geoharness.performance import MeasuredTool
from geoharness.task_controller import TaskNode, plan_status, run_plan, submit_plan
from geoharness.task_store import read_evidence, task_directory
from geoharness.tools.geo_task import GeoTaskInput, GeoTaskTool


def extraction(source):
    return TaskNode(id='extract', tool='geo_python', arguments={
        'name': 'extract', 'input_refs': {'source': str(source)},
        'expected_outputs': ['rows.csv'], 'code': (
            "p=read_vector('points')\n"
            "p.drop(columns='geometry').to_csv(RUN_DIR/'rows.csv',index=False)\n")})


def publication():
    return TaskNode(id='facts', tool='geo_task', depends_on=['extract'], arguments={
        'action': 'publish_analysis', 'source_ref': {'$ref': 'extract#/files/0/path'},
        'facts': {'key': 'id', 'numeric_fields': ['value'], 'units': {'value': '人'},
                  'method': {'operation': 'attribute_extract'}}})


async def test_processing_output_bytes_are_checked_even_with_appended_receipt(tmp_path):
    from types import SimpleNamespace

    context, _ = setup_context(tmp_path)
    target = tmp_path / 'processing-output.csv'

    class Inputs(BaseModel):
        action: str = 'run'

    class Processing:
        name = 'geo_qgis'
        input_model = Inputs

        def is_read_only(self, arguments):
            return True

        async def execute(self, arguments, context):
            target.write_text('id,value\n1,42\n', encoding='utf-8')
            output = json.dumps({'result': {'outputs': {'OUTPUT': str(target)}}})
            return ToolResult(output=output + '\nMap receipt: saved')

    context.metadata['tool_registry'] = SimpleNamespace(get=lambda name: Processing())
    submit_plan(context, [TaskNode(id='buffer', tool='geo_qgis', arguments={'action': 'run'})])
    assert (await run_plan(context))['complete']
    assert (await run_plan(context))['executed'] == []
    target.write_text('id,value\n1,999\n', encoding='utf-8')
    assert plan_status(context)['nodes'][0]['status'] == 'stale'


async def test_changed_implementation_invalidates_committed_nodes(tmp_path, monkeypatch):
    context, source = setup_context(tmp_path)
    submit_plan(context, [extraction(source), publication()])
    assert (await run_plan(context))['complete']
    monkeypatch.setattr('geoharness.task_controller._implementation_identity',
                        lambda: 'new-runtime')
    assert [item['status'] for item in plan_status(context)['nodes']] == ['stale', 'stale']


async def test_unverified_review_is_not_a_reusable_success_and_model_change_recovers(tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    context, _ = setup_context(tmp_path)
    calls = []

    class Inputs(BaseModel):
        file_path: str

    class Review:
        name = 'geo_map_review'
        input_model = Inputs

        def is_read_only(self, arguments):
            return True

        async def execute(self, arguments, context):
            calls.append(context.metadata['geoharness_config'].map_review.model)
            return ToolResult(output=json.dumps({'status': 'unverified' if len(calls) == 1
                                                  else 'passed'}))

    context.metadata['tool_registry'] = SimpleNamespace(get=lambda name: Review())
    page = tmp_path / 'review.png'
    page.write_bytes(b'image fixture')
    submit_plan(context, [TaskNode(id='review', tool='geo_map_review',
                                  arguments={'file_path': str(page)})])
    first = await run_plan(context)
    assert first['nodes'][0]['status'] == 'failed' and not first['complete']
    await run_plan(context)
    assert len(calls) == 1  # No blind retry with unchanged configuration.
    config = context.metadata['geoharness_config']
    context.metadata['geoharness_config'] = replace(config, map_review=
        config.map_review.model_copy(update={'model': 'configured-reviewer-v2'}))
    assert (await run_plan(context))['complete'] and len(calls) == 2
    config = context.metadata['geoharness_config']
    context.metadata['geoharness_config'] = replace(config, map_review=
        config.map_review.model_copy(update={'model': 'configured-reviewer-v3'}))
    assert plan_status(context)['nodes'][0]['status'] == 'stale'


async def test_real_dependency_chain_reuses_restores_and_invalidates_bytes(tmp_path):
    context, source = setup_context(tmp_path)
    submit_plan(context, [extraction(source), publication()])
    first = await run_plan(context)
    assert first['complete'], first
    assert first['executed'] == ['extract', 'facts']
    again = await run_plan(context)
    assert again['reused'] and again['executed'] == []
    assert len(read_evidence(context.metadata, cwd=tmp_path)['operations']) == 2
    operation = first['nodes'][0]['operation_id']
    original = read_evidence(context.metadata, cwd=tmp_path, operation_id=operation,
                             section='arguments', limit=12000)
    assert 'drop' in original['content']
    # Independent reconstruction from SQLite, no in-memory plan/session required.
    task_id = context.metadata['geo_task_record']['task_id']
    fresh = type(context)(cwd=tmp_path, metadata={
        'geoharness_config': context.metadata['geoharness_config']})
    restored = await GeoTaskTool().execute(GeoTaskInput(action='resume', task_id=task_id), fresh)
    assert not restored.is_error and json.loads(restored.output)['complete']
    evidence = read_evidence(fresh.metadata, cwd=tmp_path, operation_id=operation)
    chunks = [evidence['content']]
    while evidence['next_offset'] is not None:
        evidence = read_evidence(fresh.metadata, cwd=tmp_path, operation_id=operation,
                                 offset=evidence['next_offset'])
        chunks.append(evidence['content'])
    output = json.loads(''.join(chunks))
    Path(output['files'][0]['path']).write_text('id,value\n1,999\n', encoding='utf-8')
    assert plan_status(fresh)['nodes'][0]['status'] == 'stale'
    assert plan_status(fresh)['nodes'][1]['status'] == 'stale'


async def test_resume_before_execution_restores_measured_input_aliases(tmp_path):
    from geoharness.task_controller import resume_task

    context, source = setup_context(tmp_path)
    submit_plan(context, [extraction(source), publication()])
    fresh = type(context)(cwd=tmp_path, metadata={
        'geoharness_config': context.metadata['geoharness_config']})
    resume_task(fresh, context.metadata['geo_task_record']['task_id'])
    result = await run_plan(fresh)
    assert result['complete'], result


async def test_identical_failure_does_not_repeat_and_fixed_node_keeps_upstream(tmp_path):
    context, source = setup_context(tmp_path)
    broken = TaskNode(id='second', tool='geo_python', depends_on=['extract'], arguments={
        'name': 'second', 'code': "raise ValueError('intentional-contract-failure')"})
    submit_plan(context, [extraction(source), broken])
    first = await run_plan(context)
    assert [n['status'] for n in first['nodes']] == ['succeeded', 'failed']
    count = len(read_evidence(context.metadata, cwd=tmp_path)['operations'])
    second = await run_plan(context)
    assert second['executed'] == []
    assert len(read_evidence(context.metadata, cwd=tmp_path)['operations']) == count
    fixed = broken.model_copy(update={'arguments': {'name': 'second', 'code': 'print(42)'}})
    submit_plan(context, [extraction(source), fixed])
    corrected = await run_plan(context)
    assert corrected['complete'] and corrected['executed'] == ['second']
    assert corrected['nodes'][0]['operation_id'] == first['nodes'][0]['operation_id']


def test_plan_rejects_cycles_hidden_dependencies_and_recursive_execution(tmp_path):
    context, _ = setup_context(tmp_path)
    with pytest.raises(ValueError, match='依赖'):
        submit_plan(context, [TaskNode(id='a', tool='geo_qgis', arguments={
            'action': 'run', 'parameters': {'INPUT': {'$ref': 'b#/x'}}})])
    with pytest.raises(ValueError, match='循环'):
        submit_plan(context, [TaskNode(id='a', tool='geo_qgis', depends_on=['b']),
                              TaskNode(id='b', tool='geo_qgis', depends_on=['a'])])
    with pytest.raises(ValueError, match='递归'):
        submit_plan(context, [TaskNode(id='a', tool='geo_task', arguments={'action': 'run'})])


async def test_evidence_is_committed_before_return_and_redacts_configured_secret(tmp_path):
    from dataclasses import replace

    context, _ = setup_context(tmp_path)
    secret = 'test-private-credential-123456789'
    config = context.metadata['geoharness_config']
    context.metadata['geoharness_config'] = replace(config, api_key=secret)

    class Input(BaseModel):
        action: str = 'probe'

    class Tool:
        name = 'geo_probe'

        async def execute(self, arguments, context):
            return ToolResult(output=json.dumps({'status': 'ok', 'message': secret}),
                              metadata={'api_key': secret})

    result = await MeasuredTool(Tool(), context.metadata).execute(Input(), context)
    assert Path(result.metadata['evidence_path']).is_file()
    raw = Path(result.metadata['evidence_path']).read_text(encoding='utf-8')
    assert secret not in raw and '[REDACTED]' in raw
    read = read_evidence(context.metadata, cwd=tmp_path,
                         operation_id=result.metadata['operation_id'], offset=1, limit=5)
    assert len(read['content']) == 5 and read['next_offset'] == 6
    Path(result.metadata['evidence_path']).write_text('changed', encoding='utf-8')
    with pytest.raises(ValueError, match='changed'):
        read_evidence(context.metadata, cwd=tmp_path,
                      operation_id=result.metadata['operation_id'])
    assert task_directory(context.metadata, cwd=tmp_path)


def test_running_request_retains_arguments_before_any_result(tmp_path):
    from geoharness.task_store import begin_operation

    context, _ = setup_context(tmp_path)
    handle = begin_operation(context.metadata, cwd=tmp_path, tool='geo_python',
                             arguments={'code': "print('before-crash')"})
    value = read_evidence(context.metadata, cwd=tmp_path, operation_id=handle['id'],
                          section='arguments')
    assert value['status'] == 'running' and 'before-crash' in value['content']
    assert '尚无持久化结果' in read_evidence(context.metadata, cwd=tmp_path,
                                      operation_id=handle['id'])['note']


async def test_invalid_schema_is_failed_once_with_recoverable_arguments(tmp_path):
    context, _ = setup_context(tmp_path)
    submit_plan(context, [TaskNode(id='invalid', tool='geo_python',
                                   arguments={'unsupported': True})])
    first = await run_plan(context)
    assert first['nodes'][0]['status'] == 'failed'
    operation = first['nodes'][0]['operation_id']
    value = read_evidence(context.metadata, cwd=tmp_path, operation_id=operation,
                          section='arguments')
    assert 'unsupported' in value['content']
    await run_plan(context)
    assert len(read_evidence(context.metadata, cwd=tmp_path)['operations']) == 1


async def test_post_hook_runs_for_returned_failure(tmp_path):
    from types import SimpleNamespace

    from openharness.hooks import HookEvent

    context, _ = setup_context(tmp_path)
    events = []

    class Hooks:
        async def execute(self, event, data):
            events.append((event, data))
            return SimpleNamespace(blocked=False)

    context.hook_executor = Hooks()
    submit_plan(context, [TaskNode(id='failure', tool='geo_python',
        arguments={'code': "raise ValueError('expected test failure')"})])
    result = await run_plan(context)
    assert result['nodes'][0]['status'] == 'failed'
    post = [data for event, data in events if event == HookEvent.POST_TOOL_USE]
    assert len(post) == 1 and post[0]['tool_is_error']


async def test_resume_unknown_operation_keeps_evidence_without_blind_replay(tmp_path):
    from geoharness.task_controller import resume_task
    from geoharness.task_store import begin_operation, connect

    context, _ = setup_context(tmp_path)
    node = TaskNode(id='interrupted', tool='geo_python', arguments={'code': 'print(42)'})
    submit_plan(context, [node])
    directory = task_directory(context.metadata, cwd=tmp_path)
    with connect(directory) as db:
        db.execute("UPDATE nodes SET status='running',fingerprint='unknown' WHERE id=?", (node.id,))
    handle = begin_operation(context.metadata, cwd=tmp_path, tool=node.tool,
                             arguments=node.arguments, node_id=node.id)
    restored = resume_task(context, context.metadata['geo_task_record']['task_id'])
    assert restored['nodes'][0]['status'] == 'interrupted'
    assert 'process_outcome_unknown' in restored['nodes'][0]['error']
    arguments = read_evidence(context.metadata, cwd=tmp_path, operation_id=handle['id'],
                              section='arguments')
    assert 'print(42)' in arguments['content']
    assert (await run_plan(context))['executed'] == []
    assert len(read_evidence(context.metadata, cwd=tmp_path)['operations']) == 1
