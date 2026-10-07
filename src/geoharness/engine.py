"""GeoHarness query lifecycle extensions without changes to OpenHarness."""

from __future__ import annotations

from typing import AsyncIterator

from openharness.engine.messages import ConversationMessage
from openharness.engine.query_engine import QueryEngine
from openharness.engine.stream_events import StreamEvent

from geoharness.cartography.workflow import reset_map_workflow


class GeoQueryEngine(QueryEngine):
    async def submit_message(self, prompt: str | ConversationMessage) -> AsyncIterator[StreamEvent]:
        # continue_pending uses the original budget. Only actual new user input resets it.
        reset_map_workflow(self.tool_metadata)
        async for event in super().submit_message(prompt):
            yield event
