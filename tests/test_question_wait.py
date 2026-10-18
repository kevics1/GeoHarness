"""Empty clarification answers cannot be turned into authorization by auto-continue."""

import pytest
from openharness.api.client import ApiMessageCompleteEvent
from openharness.api.usage import UsageSnapshot
from openharness.engine.messages import (
    ConversationMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from openharness.engine.stream_events import StatusEvent
from test_engine_auto_continue import engine

from geoharness.tools.geo_question import GeoAskUserQuestionTool


class QuestionClient:
    def __init__(self):
        self.calls = 0

    async def stream_message(self, request):
        self.calls += 1
        content = ([ToolUseBlock(id='ask-1', name='ask_user_question',
                                input={'question': '人口缺失采用什么政策？'})]
                   if self.calls == 1 else [TextBlock(text='按已确认政策执行。')])
        yield ApiMessageCompleteEvent(
            message=ConversationMessage(role='assistant', content=content),
            usage=UsageSnapshot(input_tokens=3, output_tokens=2),
            stop_reason='tool_calls' if self.calls == 1 else 'stop')


@pytest.mark.parametrize('turns', [1, 40])
async def test_question_without_answer_pauses_before_next_model_call(tmp_path, turns):
    client = QuestionClient()
    runtime = engine(tmp_path, client, GeoAskUserQuestionTool(), turns=turns)
    events = [event async for event in runtime.submit_message('评估居民点')]
    assert client.calls == 1
    pending = runtime.tool_metadata['geo_clarification']['pending']
    assert pending['question'] == '人口缺失采用什么政策？'
    assert any(isinstance(event, StatusEvent) and '等待用户回答' in event.message
               for event in events)
    results = [b for m in runtime.messages for b in m.content if isinstance(b, ToolResultBlock)]
    assert len(results) == 1  # Preserve the tool_use/result pair, even when we suspend.
    [event async for event in runtime.submit_message('继续')]
    assert client.calls == 1
    [event async for event in runtime.submit_message('取消')]
    assert client.calls == 1
    [event async for event in runtime.continue_pending()]
    assert client.calls == 1
    [event async for event in runtime.submit_message('保留未知，不重新分配人口权重')]
    assert client.calls == 2
    assert 'pending' not in runtime.tool_metadata['geo_clarification']
    assert runtime.tool_metadata['task_focus_state']['goal'] == '评估居民点'


async def test_explicit_prompt_answer_clears_gate(tmp_path):
    client = QuestionClient()
    runtime = engine(tmp_path, client, GeoAskUserQuestionTool(), turns=40)

    async def answer(question):
        return '保留未知'

    runtime._ask_user_prompt = answer
    [event async for event in runtime.submit_message('评估居民点')]
    assert client.calls == 2 and 'pending' not in runtime.tool_metadata['geo_clarification']
