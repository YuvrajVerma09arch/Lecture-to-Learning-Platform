"""Worker tests.

**These are mocked.** No room is opened and no provider is called. What is
tested is the shape of the worker's calls against the installed LiveKit
agent API: two stale arguments reached production before any offline test
ran them, and each crashed every dispatched job while the suite stayed
green.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

from livekit.agents.voice.agent_session import AgentSession
from livekit.agents.voice.room_io import RoomOptions

from agent import worker


class TestSessionStart:
    async def test_the_start_call_matches_the_installed_agent_api(self):
        # The spec'd mock rejects a stale kwarg name the way the real
        # start() would; the isinstance check below covers the crash that
        # passed a legacy options class under a valid kwarg name.
        session = AsyncMock(spec=AgentSession)
        agent = object()
        room = object()

        await worker.start_session(session, agent, room)

        session.start.assert_awaited_once()
        kwargs = session.start.call_args.kwargs
        assert kwargs["agent"] is agent
        assert kwargs["room"] is room
        assert isinstance(kwargs["room_options"], RoomOptions)
