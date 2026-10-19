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


async def test_minimal_spec_and_plan_refs_survive_direct_controller_dispatch(tmp_path):
    """Regression: model_dump() must not expand defaults into frozen references."""
    from openharness.tools.base import ToolResult

    from geoharness.task_controller import execute_direct
    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput

    class Registry:
        def __init__(self, tool):
            self.tool = tool

        def get(self, name):
            return self.tool if name == self.tool.name else None

    class ProjectTool:
        name = 'geo_qgis_project'
        input_model = GeoQgisProjectInput

        def __init__(self):
            self.seen = []

        async def execute(self, arguments, context):
            # Simulates the real tool's frozen-reference validation after the
            # controller has revalidated the stored node.
            GeoQgisProjectInput.model_validate(arguments.model_dump(
                mode='json', exclude_unset=True))
            self.seen.append((arguments.action, set(arguments.model_fields_set)))
            return ToolResult(output=json.dumps({'status': 'succeeded'}), metadata={})

    class RealToolProxy:
        """Use the real tool only for its input contract, not for QGIS I/O."""

        name = 'geo_qgis_project'
        input_model = GeoQgisProjectInput

        def __init__(self, target):
            self.target = target

        def is_read_only(self, arguments):
            return False

        async def execute(self, arguments, context):
            return await self.target.execute(arguments, context)

    context, source = setup_context(tmp_path)
    tool = ProjectTool()
    context.metadata['tool_registry'] = Registry(RealToolProxy(tool))
    from geoharness.qgis.prepared import save_preflight
    from geoharness.qgis.specification import save_spec

    config = context.metadata['geoharness_config']
    spec_ref, _ = save_spec({
        'layers': [{'file_path': str(source), 'name': 'points', 'layer_key': 'points'}]},
        cwd=tmp_path, settings=config.qgis)
    plan_ref, _ = save_preflight(
        {'layers': [{'file_path': str(source), 'name': 'points', 'layer_key': 'points'}],
         'title': 'test'},
        {'status': 'passed'}, cwd=tmp_path, settings=config.qgis)
    for arguments in (GeoQgisProjectInput(action='build', spec_ref=spec_ref),
                      GeoQgisProjectInput(action='create', plan_ref=plan_ref)):
        result = await execute_direct(context, tool, arguments)
        assert not result.is_error, result.output
    # execute_direct's single-step path is the entry under test; the fake tool
    # above performs the same explicit-field revalidation without render I/O.
    assert tool.seen == [('build', {'action', 'spec_ref'}),
                         ('create', {'action', 'plan_ref'})]


async def test_controller_run_plan_preserves_explicit_ref_fields(tmp_path):
    """The stored node must validate again without treating defaults as explicit."""
    from openharness.tools.base import ToolResult

    from geoharness.tools.geo_qgis_project import GeoQgisProjectInput

    class ProjectTool:
        name = 'geo_qgis_project'
        input_model = GeoQgisProjectInput

        def __init__(self):
            self.seen = []

        def is_read_only(self, arguments):
            return False

        async def execute(self, arguments, context):
            self.seen.append(set(arguments.model_fields_set))
            return ToolResult(output=json.dumps({'status': 'succeeded'}), metadata={})

    class RealToolProxy:
        name = 'geo_qgis_project'
        input_model = GeoQgisProjectInput

        def __init__(self, target):
            self.target = target

        def is_read_only(self, arguments):
            return False

        async def execute(self, arguments, context):
            return await self.target.execute(arguments, context)

    context, source = setup_context(tmp_path)
    tool = ProjectTool()
    context.metadata['tool_registry'] = type('Registry', (), {
        'get': lambda self, name: RealToolProxy(tool) if name == RealToolProxy.name else None})()
    from geoharness.qgis.specification import save_spec

    spec_ref, _ = save_spec({
        'layers': [{'file_path': str(source), 'name': 'points', 'layer_key': 'points'}]},
        cwd=tmp_path, settings=context.metadata['geoharness_config'].qgis)
    node = TaskNode(id='build', tool='geo_qgis_project', arguments={
        'action': 'build', 'spec_ref': spec_ref})
    submit_plan(context, [node])
    state = await run_plan(context, node_id='build')
    assert state['nodes'][0]['status'] == 'succeeded', state
    assert tool.seen == [{'action', 'spec_ref'}]
