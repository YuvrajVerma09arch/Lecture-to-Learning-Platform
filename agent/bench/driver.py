"""The scripted LiveKit client.

This is a real voice conversation through the real pipeline: a question
is synthesised with Gnani Timbre, published as an audio track into the
LiveKit room, and the agent's answer is captured as it arrives.

What it records, all on a monotonic clock:

* ``question_end`` -- when the last question frame was published. This is
  the student's end of speech, from the student's side.
* ``first_audio`` -- when the first frame of the agent's answer arrived.
  This is the first thing the student could hear.
* ``agent_end`` -- when the agent's audio stopped, so the whole answer
  can be timed.
* the server's own markers, which arrive on the room's data channel.

The two sides of the conversation do not share a clock, so the
server-side markers are correlated with the client's readings through the
data channel rather than by comparing timestamps.
"""

from __future__ import annotations

import asyncio
import json
import time
import wave
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any

from livekit import rtc

from app.config import get_settings
from app.logging_setup import get_logger

from .questions import Question

log = get_logger(__name__)

#: The sample rate the agent's STT expects, and the rate the questions
#: are synthesised at.
SAMPLE_RATE = 16000

#: How long to wait for the agent to fall silent before the turn is over.
AGENT_SILENCE_SECONDS = 1.2

#: The data-channel topic the agent publishes its markers on.
MARKER_TOPIC = "lecture-tutor-markers"


@dataclass
class TurnRecord:
    """What one measured turn produced."""

    question: str
    language: str
    expected_covered: bool
    question_end: float | None = None
    first_audio: float | None = None
    agent_end: float | None = None
    agent_text: str = ""
    server_markers: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.first_audio is not None

    @property
    def time_to_first_audio(self) -> float | None:
        """End of speech to the first audible answer."""
        if self.question_end is None or self.first_audio is None:
            return None
        return self.first_audio - self.question_end

    @property
    def answer_duration(self) -> float | None:
        if self.first_audio is None or self.agent_end is None:
            return None
        return self.agent_end - self.first_audio

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "language": self.language,
            "expected_covered": self.expected_covered,
            "question_end": self.question_end,
            "first_audio": self.first_audio,
            "agent_end": self.agent_end,
            "time_to_first_audio_s": self.time_to_first_audio,
            "answer_duration_s": self.answer_duration,
            "agent_text": self.agent_text,
            "server_markers": self.server_markers,
            "error": self.error,
            "ok": self.ok,
        }


# --------------------------------------------------------------------------
# Synthesising the question
# --------------------------------------------------------------------------


async def synthesise_question(
    question: Question, *, cache_dir: Path | None = None
) -> bytes:
    """Synthesise a question as 16 kHz mono s16le PCM.

    The synthesis is cached, so re-running the harness does not spend
    TTS credits on the same questions.
    """
    settings = get_settings()

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key = f"{abs(hash((question.text, question.language, question.pause_seconds))) % (10**12)}"
        cached = cache_dir / f"{key}.raw"
        if cached.exists():
            return cached.read_bytes()

    payload = {
        "text": question.text,
        "voice": question.voice or _voice_for(question.language),
        "model": "timbre-v2.5",
        "language": question.language,
        "speed": 1.0,
        "audio_config": {
            "sample_rate": SAMPLE_RATE,
            "num_channels": 1,
            "sample_width": 2,
            "encoding": "linear_pcm",
            "container": "wav",
        },
    }
    headers = {
        "X-API-Key-ID": settings.gnani_api_key,
        "Content-Type": "application/json",
    }

    import aiohttp

    url = f"{settings.gnani_base_url}/api/v1/tts/inference"
    timeout = aiohttp.ClientTimeout(connect=15, total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"TTS failed ({resp.status}): {body[:200]}")
            audio = await resp.read()

    pcm = _wav_to_pcm(audio)
    if cache_dir is not None:
        cached.write_bytes(pcm)
    return pcm


def _voice_for(language: str) -> str:
    from agent.tutor import voice_for_language

    return voice_for_language(language)


def _wav_to_pcm(audio: bytes) -> bytes:
    """The raw 16-bit mono samples from a WAV body."""
    try:
        with wave.open(BytesIO(audio), "rb") as wav:
            if wav.getsampwidth() != 2:
                raise RuntimeError("expected 16-bit samples")
            if wav.getnchannels() != 1:
                raise RuntimeError("expected mono audio")
            return wav.readframes(wav.getnframes())
    except wave.Error:
        # Not a WAV: the API may have returned raw PCM.
        return audio


def splice_pause(pcm: bytes, *, at_seconds: float, seconds: float) -> bytes:
    """Insert a silence into the middle of a question.

    This is the "student pauses before a technical term" case. The silence
    has to be real, or the server's voice activity detection never sees a
    gap and the pause is not a test of anything.
    """
    if seconds <= 0:
        return pcm
    at_bytes = int(at_seconds * SAMPLE_RATE) * 2
    at_bytes = max(0, min(at_bytes, len(pcm)))
    silence = b"\x00" * int(seconds * SAMPLE_RATE) * 2
    return pcm[:at_bytes] + silence + pcm[at_bytes:]


# --------------------------------------------------------------------------
# The room
# --------------------------------------------------------------------------


async def mint_token(lecture_id: str, *, api_url: str) -> tuple[str, str]:
    """Ask the backend for a room token.

    Returns the token and the room name.
    """
    import aiohttp

    payload = {"lecture_id": lecture_id, "participant_name": "Benchmark"}
    timeout = aiohttp.ClientTimeout(connect=10, total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            f"{api_url.rstrip('/')}/api/livekit/token", json=payload
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"Could not mint a token ({resp.status}): {body[:200]}")
            data = await resp.json()
    return data["token"], data["room"]


async def run_turn(
    question: Question,
    *,
    lecture_id: str,
    api_url: str,
    livekit_url: str,
    cache_dir: Path | None = None,
    timeout: float = 60.0,
) -> TurnRecord:
    """Run one measured turn."""
    record = TurnRecord(
        question=question.text,
        language=question.language,
        expected_covered=question.expected_covered,
    )

    try:
        pcm = await synthesise_question(question, cache_dir=cache_dir)
        if question.has_pause:
            pcm = splice_pause(
                pcm,
                at_seconds=(len(pcm) / (2 * SAMPLE_RATE)) * question.pause_at,
                seconds=question.pause_seconds,
            )

        token, room_name = await mint_token(lecture_id, api_url=api_url)

        room = rtc.Room()
        source = rtc.AudioSource(SAMPLE_RATE, 1)

        @room.on("track_subscribed")
        def _on_track(track: rtc.Track, publication: Any) -> None:
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            asyncio.create_task(_consume_agent_audio(track, record))

        @room.on("data_received")
        def _on_data(
            packet: rtc.DataPacket, participant: Any, kind: Any, topic: str | None
        ) -> None:
            if topic != MARKER_TOPIC:
                return
            try:
                payload = json.loads(packet.payload.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return
            record.server_markers[payload.get("type", "unknown")] = payload

        await room.connect(livekit_url, token)

        track = rtc.LocalAudioTrack.create_audio_track("benchmark-question", source)
        await room.local_participant.publish_track(track)

        # Publish the question at real-time pace, which is what the server's
        # voice activity detection expects.
        frame_bytes = 1024
        for offset in range(0, len(pcm), frame_bytes):
            chunk = pcm[offset : offset + frame_bytes]
            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=len(chunk) // 2,
            )
            await source.capture_frame(frame)
            await asyncio.sleep(len(chunk) / (2 * SAMPLE_RATE))
        record.question_end = time.monotonic()

        # Wait for the agent to answer, then for it to fall silent.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if record.agent_end is not None:
                break
            await asyncio.sleep(0.05)

        await room.disconnect()
        if record.first_audio is None:
            record.error = "the agent produced no audio"
    except Exception as exc:  # noqa: BLE001
        record.error = f"{type(exc).__name__}: {exc}"

    return record


async def _consume_agent_audio(track: rtc.Track, record: TurnRecord) -> None:
    """Read the agent's audio and note when it starts and stops."""
    try:
        stream = rtc.AudioStream.from_track(
            track=track, sample_rate=48000, num_channels=1
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not open the agent audio", extra={"error": str(exc)[:200]})
        return

    try:
        async for _event in stream:
            now = time.monotonic()
            if record.first_audio is None:
                record.first_audio = now
            record.agent_end = now
    except Exception as exc:  # noqa: BLE001
        log.warning("the agent audio ended", extra={"error": str(exc)[:200]})
    finally:
        await stream.aclose()
