"""The LiveKit agent worker.

This is a **separate process** from the API. It has to be: a LiveKit agent
holds a long-lived session in a room, and does not belong in the same
process as the request handler.

It shares the SQLite file with the API, so the tutor reads the same
transcript and the same notes the notes reader shows.

Run it with:

    .venv/bin/python -m agent.worker

or, during development, through the LiveKit CLI:

    lk agent dev
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from livekit.agents import AgentServer, JobContext, RoomInputOptions, metrics
from livekit.agents.voice import AgentSession

from app.logging_setup import configure_logging, get_logger

from .tutor import (
    TutorState,
    build_agent,
    build_session,
    language_for_room,
    lecture_id_from_room,
)

log = get_logger(__name__)

server = AgentServer()

#: The data-channel topic the latency markers are published on, so the
#: client can correlate them with its own readings. The two processes do
#: not share a clock, so the correlation has to travel through the room.
MARKER_TOPIC = "lecture-tutor-markers"


@server.rtc_session(agent_name="lecture-tutor")
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

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=RoomInputOptions(),
    )

    await publish_markers(session, state, ctx)

    # The session runs until the room ends.
    await session.generate_reply()


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
        from livekit import rtc

        packet = rtc.DataPacket(
            topic=MARKER_TOPIC,
            payload=json.dumps(payload).encode("utf-8"),
        )
        asyncio.create_task(
            ctx.room.local_participant.publish_data(packet)
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not publish a marker", extra={"error": str(exc)[:200]})


def main() -> None:
    """Run the worker."""
    from livekit.agents import cli

    configure_logging("INFO")
    cli.run_app(server)


if __name__ == "__main__":
    main()
