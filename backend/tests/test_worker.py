"""Worker tests.

**These are mocked.** No room is opened and no provider is called. What is
tested is the shape of the worker's calls against the installed LiveKit
agent API: two stale arguments reached production before any offline test
ran them, and each crashed every dispatched job while the suite stayed
green.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from livekit.agents.voice.agent_session import AgentSession
from livekit.agents.voice.room_io import RoomOptions

from agent import tutor, worker
from app import db as db_mod


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


class TestSessionState:
    def test_prepare_state_grounds_the_state_from_the_room(self):
        now = time.time()
        with db_mod.get_conn() as conn:
            conn.execute(
                "INSERT INTO lectures (id, title, original_filename, content_hash, "
                "language_code, status, owner_id, created_at, updated_at) "
                "VALUES (?, ?, 'lecture.mp3', 'hash-worker', 'en-IN', 'ready', "
                "'anonymous', ?, ?)",
                ("lec_worker", "Worker Lecture", now, now),
            )

        state = worker.prepare_state(
            "lecture-lec_worker-deadbeef", '{"language": "te-IN"}'
        )
        assert state.lecture_id == "lec_worker"
        assert state.language == "te-IN"
        assert state.lecture_title == "Worker Lecture"

    async def test_the_entrypoint_builds_the_agent_from_the_prepared_state(self, monkeypatch):
        state = tutor.TutorState(lecture_id="lec_worker", lecture_title="Worker Lecture")
        prepared = []

        def prepare(room_name, metadata):
            prepared.append((room_name, metadata))
            return state

        monkeypatch.setattr(worker, "configure_logging", lambda *_: None)
        monkeypatch.setattr(worker, "prepare_state", prepare)
        monkeypatch.setattr(worker, "build_session", lambda s: f"session-{s.lecture_id}")
        monkeypatch.setattr(worker, "build_agent", lambda s: f"agent-{s.lecture_id}")
        start = AsyncMock()
        markers = AsyncMock()
        monkeypatch.setattr(worker, "start_session", start)
        monkeypatch.setattr(worker, "_publish", lambda *_: None)
        monkeypatch.setattr(worker, "publish_markers", markers)

        room = SimpleNamespace(name="lecture-lec_worker-deadbeef", metadata="")
        await worker.tutor_entrypoint(SimpleNamespace(room=room))

        assert prepared == [(room.name, room.metadata)]
        start.assert_awaited_once_with("session-lec_worker", "agent-lec_worker", room)
        markers.assert_awaited_once()
        assert markers.await_args.args[1] is state
