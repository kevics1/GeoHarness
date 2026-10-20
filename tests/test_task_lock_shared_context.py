import asyncio
from dataclasses import replace

from test_managed_inputs import setup_context

from geoharness.task_controller import _in_process_task_lock


async def test_sibling_tool_contexts_queue_instead_of_conflicting(tmp_path):
    original, _ = setup_context(tmp_path)
    contexts = [replace(original, metadata=dict(original.metadata)) for _ in range(2)]
    active = 0
    maximum = 0

    async def work(context):
        nonlocal active, maximum
        async with _in_process_task_lock(context):
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0)
            active -= 1

    await asyncio.gather(*(work(context) for context in contexts))
    assert maximum == 1, '同一任务的浅复制工具上下文必须共享排队锁'
