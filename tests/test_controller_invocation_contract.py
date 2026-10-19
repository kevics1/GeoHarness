import json

from test_managed_inputs import setup_context

from geoharness.task_controller import TaskNode, plan_status, run_plan, submit_plan
from geoharness.tools.geo_task import GeoTaskInput, GeoTaskTool


async def test_incremental_plan_keeps_and_reuses_prior_node_and_returns_business_result(tmp_path):
    context, _ = setup_context(tmp_path)
    first = TaskNode(id='inspect', tool='geo_python', arguments={
        'name': 'inspect', 'code': "CONSOLE_RESULT={'fields':['id','value'],'rows':1}"})
    submit_plan(context, [first])
    done = await run_plan(context)
    original = done['nodes'][0]['operation_id']
    assert done['nodes'][0]['result']['result']['console_result']['rows'] == 1
    second = TaskNode(id='next', tool='geo_python', depends_on=['inspect'], arguments={
        'name': 'next', 'code': 'CONSOLE_RESULT=42'})
    submit_plan(context, [second])
    state = await run_plan(context)
    assert state['executed'] == ['next']
    assert state['nodes'][0]['operation_id'] == original
    assert state['nodes'][1]['result']['result']['console_result'] == 42
    submit_plan(context, [first], replace=True)
    assert [n['id'] for n in plan_status(context)['nodes']] == ['inspect']


async def test_one_call_execute_returns_structured_result_and_reuses_identical_request(tmp_path):
    context, _ = setup_context(tmp_path)
    args = GeoTaskInput(action='execute', node=TaskNode(id='inspect', tool='geo_python',
        arguments={'name': 'inspect', 'code': "CONSOLE_RESULT={'rows':13}"}))
    first = await GeoTaskTool().execute(args, context)
    assert not first.is_error, first.output
    result = json.loads(first.output)
    assert result['target_complete']
    assert result['nodes'][0]['result']['result']['console_result']['rows'] == 13
    again = await GeoTaskTool().execute(args, context)
    assert json.loads(again.output)['executed'] == []


async def test_evidence_sections_extract_payload_without_provenance_paging(tmp_path):
    from geoharness.task_store import read_evidence

    context, _ = setup_context(tmp_path)
    args = GeoTaskInput(action='execute', node=TaskNode(id='inspect', tool='geo_python',
        arguments={'name': 'inspect', 'code': "CONSOLE_RESULT={'count':13}"}))
    result = await GeoTaskTool().execute(args, context)
    operation = json.loads(result.output)['nodes'][0]['operation_id']
    page = read_evidence(context.metadata, cwd=tmp_path, operation_id=operation, section='result')
    assert json.loads(page['content']) == {'console_result': {'count': 13}}
    assert page['next_call'] is None


async def test_direct_native_call_is_automatically_registered_and_not_reexecuted(tmp_path):
    from geoharness.performance import MeasuredTool
    from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool

    context, _ = setup_context(tmp_path)
    tool = MeasuredTool(GeoPythonTool(), context.metadata)
    args = GeoPythonInput(code="CONSOLE_RESULT={'rows':13}")
    first = await tool.execute(args, context)
    assert not first.is_error, first.output
    assert json.loads(first.output)['result']['console_result']['rows'] == 13
    again = await tool.execute(args, context)
    assert not again.is_error and again.metadata['reused']
    assert again.metadata['managed_operation_id'] == first.metadata['managed_operation_id']


async def test_direct_failure_keeps_original_evidence_without_relaunching_script(tmp_path):
    from geoharness.performance import MeasuredTool
    from geoharness.tools.geo_python import GeoPythonInput, GeoPythonTool

    context, _ = setup_context(tmp_path)
    tool = MeasuredTool(GeoPythonTool(), context.metadata)
    args = GeoPythonInput(code="raise ValueError('controlled failure')")
    first = await tool.execute(args, context)
    assert first.is_error
    scripts = list(tmp_path.glob('.geoharness/runs/*/analysis.py'))
    assert len(scripts) == 1
    again = await tool.execute(args, context)
    assert again.is_error
    assert again.metadata['managed_operation_id'] == first.metadata['managed_operation_id']
    assert list(tmp_path.glob('.geoharness/runs/*/analysis.py')) == scripts
