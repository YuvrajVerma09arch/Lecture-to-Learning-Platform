"""The LiveKit agent worker.

This is a **separate process** from the API. It has to be: a LiveKit agent
holds a long-lived session in a room, and does not belong in the same
process as the request handler.

It shares the SQLite file with the API, so the tutor reads the same
transcript and the same notes the notes reader shows.

Run it with the thin CLI, which discovers the ``server`` object in this
module:

    .venv/bin/python -m livekit.agents start agent/worker.py

or, during development, through the LiveKit CLI:

    lk agent dev
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time
from pathlib import Path
from typing import Any

# The LiveKit worker reads its configuration from the environment rather
# than from the settings object, so the .env file has to be loaded before
# the framework starts. It is a no-op when the variables are already set.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_ENV_FILE = _PROJECT_ROOT / ".env"
if _ENV_FILE.exists():
    with contextlib.suppress(ImportError):
        from dotenv import load_dotenv

        load_dotenv(_ENV_FILE, override=False)

from livekit.agents import AgentServer, JobContext, metrics  # noqa: E402
from livekit.agents.voice import AgentSession  # noqa: E402
from livekit.agents.voice.room_io import RoomOptions  # noqa: E402

from app.logging_setup import configure_logging, get_logger  # noqa: E402

from .tutor import (  # noqa: E402
    AGENT_NAME,
    TutorState,
    build_agent,
    build_session,
    language_for_room,
    lecture_id_from_room,
)

log = get_logger(__name__)

#: How long a worker process may take to start.
#:
#: The default is 10 seconds, and importing the framework and the plugins
#: takes about 8.5 seconds on this machine -- with several processes
#: starting at once, they all missed the deadline and the worker
#: registered but could not serve a session.
INITIALIZE_PROCESS_TIMEOUT = 90.0

#: Never report the worker as full because of system load.
#:
#: The framework's default gate is 0.7 of system CPU, measured across
#: the whole machine. This machine's CPU is held between 0.5 and 0.99 by
#: processes unrelated to the worker, so the gate flapped: the worker
#: told LiveKit it was full, dispatch was withheld for turns that were
#: already queued, and 14 of 24 benchmark turns failed with "the agent
#: never became ready". The worker serves one session at a time, so
#: system load says nothing about whether it can accept a job, and the
#: gate only delays that job. It reverts to the cloud default when a
#: worker token is configured; this deployment mints tokens itself.
server = AgentServer(
    initialize_process_timeout=INITIALIZE_PROCESS_TIMEOUT,
    load_threshold=math.inf,
)

#: The data-channel topic the latency markers are published on, so the
#: client can correlate them with its own readings. The two processes do
#: not share a clock, so the correlation has to travel through the room.
MARKER_TOPIC = "lecture-tutor-markers"


@server.rtc_session(agent_name=AGENT_NAME)
async def tutor_entrypoint(ctx: JobContext) -> None:
    """Run one tutoring session.

    The room name says which lecture the session is about; that is what
    grounds the answers.
    """
    configure_logging("INFO")

    state = TutorState()
    state.lecture_id = lecture_id_from_room(ctx.room.name)
    state.language = language_for_room(ctx.room.name, dict(ctx.room.metadata or {}))

    log.info(
        "tutor session starting",
        extra={
            "room": ctx.room.name,
            "lecture_id": state.lecture_id,
            "language": state.language,
            "participant": ctx.room.name,
        },
    )

    session: AgentSession = build_session(state)
    agent = build_agent(state)

    await start_session(session, agent, ctx.room)

    # The benchmark will not publish its question until this arrives, so
    # the agent cannot miss the first seconds of the audio.
    _publish(ctx, {"type": "session_ready"})

    await publish_markers(session, state, ctx)


async def start_session(session: AgentSession, agent: Any, room: Any) -> None:
    await session.start(
        agent=agent,
        room=room,
        room_options=RoomOptions(),
    )


async def publish_markers(
    session: AgentSession, state: TutorState, ctx: JobContext
) -> None:
    """Publish the latency markers into the room.

    The client cannot read the server's log lines, so the markers are
    published on a data channel. Both sides then have the same timeline
    without assuming their clocks agree.
    """

    @session.on("user_input_transcribed")
    def _on_transcribed(ev: Any) -> None:
        if ev.transcript:
            state.timings.final_transcript = time.monotonic()
            state.question = ev.transcript
            log.info(
                "student turn transcribed",
                extra={
                    "lecture_id": state.lecture_id,
                    "transcript_length": len(ev.transcript),
                },
            )
            _publish(ctx, {"type": "transcript", "at": state.timings.final_transcript})

    @session.on("conversation_item_added")
    def _on_item(ev: Any) -> None:
        if getattr(ev, "item", None) is None:
            return
        item = ev.item
        if getattr(item, "type", "") != "message":
            return
        if getattr(item, "role", "") != "assistant":
            return
        if state.timings.llm_first_token is None:
            state.timings.llm_first_token = time.monotonic()
        state.timings.llm_complete = time.monotonic()
        _publish(ctx, {"type": "turn_timings", **state.timings.to_dict()})
        if text := getattr(item, "text_content", None):
            _publish(ctx, {"type": "assistant_text", "text": text})

    @session.on("metrics_collected")
    def _on_metrics(ev: Any) -> None:
        for metric in ev.metrics or []:
            if isinstance(metric, metrics.TTSMetrics):
                if state.timings.tts_first_byte is None:
                    state.timings.tts_first_byte = time.monotonic()
                state.timings.tts_complete = time.monotonic()
                _publish(ctx, {"type": "turn_timings", **state.timings.to_dict()})
            elif isinstance(metric, metrics.LLMMetrics) and getattr(metric, "ttft", None):
                if state.timings.llm_first_token is None:
                    state.timings.llm_first_token = time.monotonic()
                _publish(ctx, {"type": "turn_timings", **state.timings.to_dict()})
            elif isinstance(metric, metrics.STTMetrics):
                log.info(
                    "stt metric",
                    extra={
                        "duration": metric.duration,
                        "audio_duration": metric.audio_duration,
                        "streamed": metric.streamed,
                    },
                )


def _publish(ctx: JobContext, payload: dict[str, Any]) -> None:
    """Publish a marker into the room.

    The session event handlers are synchronous callbacks, so the publish
    is scheduled rather than awaited. A failure here must not break the
    session: the markers are diagnostic.
    """
    try:
        asyncio.create_task(
            ctx.room.local_participant.publish_data(
                json.dumps(payload).encode("utf-8"), topic=MARKER_TOPIC
            )
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not publish a marker", extra={"error": str(exc)[:200]})


def main() -> None:
    """Run the worker.

    The thin CLI is the supported way to run an agent in 1.8.8: it
    discovers the ``server`` object and wires up the process lifecycle.
    """
    from livekit.agents.__main__ import main as agents_main

    configure_logging("INFO")
    raise SystemExit(agents_main(["start", "agent/worker.py"]))


if __name__ == "__main__":
    main()
