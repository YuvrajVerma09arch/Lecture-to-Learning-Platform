#!/usr/bin/env python
"""Measure the Gnani STT turn-taking configurations.

The tuning has two knobs that pull against each other:

  * ``x-min-silence-ms`` -- how much silence the server needs before it
    closes a segment. Higher means a mid-sentence pause is less likely to
    split the utterance, and a transcript is later.
  * the silence injection -- the server closes a segment only when it
    *hears* silence, so something has to send it. Injecting too eagerly
    turns a mid-sentence pause into an end-of-utterance.

This sweeps both and reports, for each configuration:
  * how long after the student stops speaking the transcript arrives;
  * whether a pause in the middle splits the utterance into two
    transcripts.

Usage:
    .venv/bin/python scripts/probe_tuned_stt.py
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from livekit import rtc  # noqa: E402

from agent.stt_gnani import TunedGnaniSTT  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.probe_stt import make_pcm  # noqa: E402

#: The length of the mid-utterance gap. It is short enough that a
#: student pausing before a technical term would do it, and long enough
#: to be a real test of the endpointing.
PAUSE_SECONDS = 0.6


@dataclass
class Config:
    label: str
    min_silence_ms: int
    silence_timeout: float
    silence_fill: float

    @property
    def vad_headers(self) -> dict[str, str]:
        return {
            "x-vad-threshold": "0.7",
            "x-min-silence-ms": str(self.min_silence_ms),
            "x-min-speech-ms": "150",
            "x-min-segment-ms": "150",
        }


CONFIGS = [
    Config("server-default", 500, 0.50, 0.60),
    Config("patient", 700, 0.80, 0.80),
    # The injection timeout equal to the server's min-silence: the
    # injected silence can never itself be long enough to close a
    # segment that the timeout has not already justified.
    Config("matched", 700, 0.70, 0.60),
    Config("eager", 500, 0.20, 0.60),
    Config("no-injection", 500, 99.0, 0.0),
]


async def run_once(pcm: bytes, *, config: Config, lang: str, pause_at: float | None) -> dict:
    settings = get_settings()
    stt = TunedGnaniSTT(
        language=lang,
        api_key=settings.gnani_api_key,
        vad_headers=config.vad_headers,
        silence_timeout=config.silence_timeout,
        silence_fill=config.silence_fill,
    )
    stream = stt.stream()
    finals: list[tuple[float, str]] = []
    started = time.monotonic()
    got_final = asyncio.Event()

    async def reader() -> None:
        try:
            async for event in stream:
                now = time.monotonic() - started
                if event.type.name == "FINAL_TRANSCRIPT":
                    text = event.alternatives[0].text if event.alternatives else ""
                    finals.append((now, text))
                    got_final.set()
        except Exception:
            pass

    async def sender() -> None:
        frame_size = 1024
        frames = [pcm[i : i + frame_size] for i in range(0, len(pcm), frame_size)]
        frame_seconds = frame_size / (2 * 16000)
        bytes_per_second = 2 * 16000
        paused = False
        for i, frame in enumerate(frames):
            offset = (i * frame_size) / bytes_per_second
            if pause_at is not None and not paused and offset >= pause_at:
                await asyncio.sleep(PAUSE_SECONDS)
                paused = True
            stream.push_frame(
                rtc.AudioFrame(
                    data=frame,
                    sample_rate=16000,
                    num_channels=1,
                    samples_per_channel=len(frame) // 2,
                )
            )
            await asyncio.sleep(frame_seconds)

    reader_task = asyncio.create_task(reader())
    await sender()
    try:
        await asyncio.wait_for(got_final.wait(), timeout=8.0)
    except TimeoutError:
        pass
    await asyncio.sleep(1.5)
    reader_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await reader_task
    await stream.aclose()

    audio_end = len(pcm) / (2 * 16000)
    return {
        "final_count": len(finals),
        "first_final_at": finals[0][0] if finals else None,
        "latency_after_audio_end": (finals[0][0] - audio_end) if finals else None,
        "transcripts": [t for _, t in finals],
    }


async def main() -> int:
    settings = get_settings()
    if not settings.has_gnani():
        print("GNANI_API_KEY is not set", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        pcm, duration = make_pcm("hi-IN", Path(tmp))
    audio_end = len(pcm) / (2 * 16000)
    print(f"audio: {duration:.2f}s of Hindi speech, ends at t={audio_end:.2f}s\n")

    header = f"{'config':16} {'min-sil':>8} {'inject':>7} | {'clean: n':>9} {'latency':>9} | {'pause: n':>9} {'latency':>9}"
    print(header)
    print("-" * len(header))

    for config in CONFIGS:
        clean = await run_once(pcm, config=config, lang="hi-IN", pause_at=None)
        paused = await run_once(pcm, config=config, lang="hi-IN", pause_at=4.0)

        def fmt(r: dict) -> str:
            lat = r["latency_after_audio_end"]
            return f"{r['final_count']:>9} {'n/a' if lat is None else f'{lat:+.3f}s'}"

        print(
            f"{config.label:16} {config.min_silence_ms:>8} "
            f"{config.silence_timeout:>7.2f} | {fmt(clean)} | {fmt(paused)}"
        )

    print()
    print("A good configuration: one final transcript in both cases, and the")
    print("shortest latency in the clean case. 'pause: n' above 1 means the")
    print("mid-sentence pause split the utterance into several transcripts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
