"""Exercise the measured model-facing read path, including repeated pagination."""

import json

from openharness.tools.base import ToolResult
from pydantic import BaseModel
from test_managed_inputs import setup_context

from geoharness.performance import MeasuredTool
from geoharness.tools.geo_task import GeoTaskInput, GeoTaskTool


async def test_measured_evidence_pages_keep_original_target_and_exact_next_call(tmp_path):
    context, _ = setup_context(tmp_path)

    class Input(BaseModel):
        action: str = 'probe'

    class Tool:
        name = 'geo_probe'

        async def execute(self, arguments, context):
            return ToolResult(output=json.dumps({'value': 'business-result-' * 180}))

    original = await MeasuredTool(Tool(), context.metadata).execute(Input(), context)
    target = original.metadata['operation_id']
    arguments = GeoTaskInput(action='evidence', operation_id=target, limit=300)
    chunks = []
    while True:
        result = await MeasuredTool(GeoTaskTool(), context.metadata).execute(arguments, context)
        assert not result.is_error, result.output
        page = json.loads(result.output)
        assert page['operation_id'] == target
        assert page['execution_id'] != target
        chunks.append(page['content'])
        if page['next_offset'] is None:
            assert page['next_call'] is None
            break
        assert page['next_call']['operation_id'] == target
        arguments = GeoTaskInput.model_validate(page['next_call'])
    assert json.loads(''.join(chunks)) == {'value': 'business-result-' * 180}


async def test_wrapper_keeps_business_operation_identity_and_appended_receipt(tmp_path):
    context, _ = setup_context(tmp_path)

    class Input(BaseModel):
        action: str = 'probe'

    class Tool:
        name = 'geo_probe'

        async def execute(self, arguments, context):
            return ToolResult(output=json.dumps({'operation_id': 'business-id', 'value': 42})
                              + '\nMap receipt: saved')

    result = await MeasuredTool(Tool(), context.metadata).execute(Input(), context)
    packet, end = json.JSONDecoder().raw_decode(result.output)
    assert packet['operation_id'] == 'business-id'
    assert packet['target_operation_id'] == 'business-id'
    assert packet['execution_id'] == result.metadata['operation_id']
    assert result.output[end:] == '\nMap receipt: saved'
    # The read call points at the execution that produced this result; the business
    # identity stays separate and is never replaced by it.
    assert result.metadata['operation_id'] == packet['execution_id']
    assert packet['evidence_call']['operation_id'] == packet['execution_id']
