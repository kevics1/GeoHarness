import json

from test_managed_inputs import setup_context

from geoharness.task_state import begin_task
from geoharness.tools.geo_task import GeoTaskInput, GeoTaskTool


def proposal():
    return GeoTaskInput(action='propose_score', policy={'purpose': '测试相对排序',
        'components': [{'field': 'value', 'weight': 1, 'minimum': 0,
                        'maximum': 10, 'direction': 'higher'}], 'missing_policy': 'unknown'})


def test_score_policy_classification_is_explicit_and_contiguous():
    from geoharness.analysis.policy import ScorePolicy

    values = proposal().policy.model_dump(mode='python')
    values['classification'] = [
        {'minimum': 0, 'maximum': .5, 'label': '低'},
        {'minimum': .5, 'maximum': 1, 'label': '高'},
    ]
    policy = ScorePolicy(**values)
    assert [band.label for band in policy.classification] == ['低', '高']


async def test_policy_cannot_be_self_confirmed_and_standard_score_uses_exact_parameters(tmp_path):
    from geoharness.analysis.policy import resolve_policy_answer

    context, source = setup_context(tmp_path)
    tool = GeoTaskTool()
    proposed = await tool.execute(proposal(), context)
    ref = json.loads(proposed.output)['policy_ref']
    args = GeoTaskInput(action='score', policy_ref=ref, source_ref='points', entity_key='id')
    blocked = await tool.execute(args, context)
    assert blocked.is_error and not blocked.metadata['script_executed']
    resolve_policy_answer(context.metadata, '请把归一化上限改为20')
    assert context.metadata['geo_task_record']['score_policies'][ref]['status'] == 'needs_revision'
    context.metadata['geo_clarification'].pop('pending')
    revised = proposal()
    revised.policy.components[0].maximum = 20
    ref = json.loads((await tool.execute(revised, context)).output)['policy_ref']
    args = args.model_copy(update={'policy_ref': ref})
    resolve_policy_answer(context.metadata, '同意')
    context.metadata['geo_clarification'].pop('pending')
    scored = await tool.execute(args, context)
    assert not scored.is_error, scored.output
    result = json.loads(scored.output)
    assert result['execution_policy']['components'][0]['maximum'] == 20
    assert list(result['inputs'].values()) == [str(source)]
    import geopandas as gpd

    frame = gpd.read_file(result['run_dir'] + '/scored.geojson')
    assert frame['risk_score'].tolist() == [0.1]
    report = json.loads(open(result['run_dir'] + '/score_facts.json', encoding='utf-8').read())
    assert report['method']['policy_ref'] == ref


async def test_proposal_uses_real_answer_callback_and_pending_survives_restore(
        tmp_path, monkeypatch):
    from openharness.api.usage import UsageSnapshot
    from openharness.engine.messages import ConversationMessage

    from geoharness.session_state import GeoSessionBackend, restore_geo_session_state

    context, _ = setup_context(tmp_path)
    begin_task(context.metadata, cwd=tmp_path, goal='评分并制图')
    await GeoTaskTool().execute(proposal(), context)
    monkeypatch.setattr('geoharness.session_state.get_config_dir', lambda: tmp_path / 'config')
    path = GeoSessionBackend().save_snapshot(cwd=tmp_path, model='offline', system_prompt='',
        messages=[ConversationMessage.from_user_text('评分')], usage=UsageSnapshot(),
        tool_metadata=context.metadata)
    fresh = {}
    assert restore_geo_session_state(fresh, json.loads(path.read_text(encoding='utf-8'))[
        'tool_metadata'], cwd=tmp_path, config=context.metadata['geoharness_config'])
    assert fresh['geo_clarification']['pending']['policy_ref']

    async def answer(question):
        return '同意'

    context.metadata['ask_user_prompt'] = answer
    result = await GeoTaskTool().execute(proposal(), context)
    assert json.loads(result.output)['status'] == 'confirmed'


async def test_real_engine_waits_for_policy_answer_and_keeps_task_identity(tmp_path):
    from openharness.api.client import ApiMessageCompleteEvent
    from openharness.api.usage import UsageSnapshot
    from openharness.config.settings import PermissionSettings
    from openharness.engine.messages import ConversationMessage, TextBlock, ToolUseBlock
    from openharness.permissions.checker import PermissionChecker
    from openharness.tools.base import ToolRegistry

    from geoharness.config.settings import GeoConfig
    from geoharness.engine import GeoQueryEngine

    class Client:
        calls = 0

        async def stream_message(self, request):
            self.calls += 1
            block = ToolUseBlock(id='proposal', name='geo_task', input=proposal().model_dump(
                mode='json')) if self.calls == 1 else TextBlock(text='已确认')
            yield ApiMessageCompleteEvent(message=ConversationMessage(role='assistant',
                content=[block]), usage=UsageSnapshot())

    client, registry = Client(), ToolRegistry()
    registry.register(GeoTaskTool())
    settings = PermissionSettings()
    settings.allowed_tools.append('geo_task')
    engine = GeoQueryEngine(api_client=client, tool_registry=registry,
        permission_checker=PermissionChecker(settings=settings), cwd=tmp_path, model='offline',
        system_prompt='', tool_metadata={'geoharness_config': GeoConfig()})
    _ = [event async for event in engine.submit_message('请评分')]
    assert client.calls == 1 and engine.tool_metadata['geo_clarification']['pending']
    identity = engine.tool_metadata['geo_task_record']['task_id']
    _ = [event async for event in engine.submit_message('继续')]
    assert client.calls == 1
    _ = [event async for event in engine.submit_message('同意')]
    assert client.calls == 2
    task = engine.tool_metadata['geo_task_record']
    assert task['task_id'] == identity
    assert next(iter(task['score_policies'].values()))['status'] == 'confirmed'
