#!/usr/bin/env python
"""Probe which ``lang_code`` values the streaming STT accepts, and what
script they come back in.

The tutor currently opens every session with ``en-IN``, because the
browser never sets room metadata and ``language_for_room`` falls back to
the default. The Gnani docs say ``en-IN`` accepts English-Hindi mixed
audio and emits **Latin script**, which is why a student speaking Hindi
sees a transliterated transcript and gets a Latin-script reply.

The plugin's type hints advertise one auto-detection value --
``"en-IN,hi-IN"`` -- but the plugin only forwards the string, so what
the *server* accepts and detects is not settled by the types. This
probe settles it:

  * control: each audio under ``en-IN`` (expected: Latin for everything);
  * the documented pair ``en-IN,hi-IN``;
  * a longer comma list, to see if the server accepts more than a pair;
  * ``auto``, which TTS documents but STT does not.

Audio sources (all free -- no synthesis credits):

  * macOS ``say`` clips in Devanagari, English and Telugu;
  * a cached bench clip (real Gnani Timbre audio) spoken in Hinglish.

Usage:
    .venv/bin/python scripts/probe_lang_detect.py
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from livekit import rtc  # noqa: E402

from agent.stt_gnani import TunedGnaniSTT  # noqa: E402
from app.config import get_settings  # noqa: E402
from scripts.probe_stt import SPEECH, VOICE  # noqa: E402

#: The lang_code values to try. ``en-IN`` is the control: it is what the
#: tutor sends today. The comma pair and ``auto`` were probed earlier and
#: rejected by the server with WebSocket close 1008 (policy violation),
#: so only single codes remain to be mapped.
LANG_CODES = [
    "hi-IN",
    "te-IN",
]

#: Which script ranges count as "the transcript came back in script X".
#: Latin is checked last because Latin letters appear inside Indic
#: transcripts as code-switched technical terms.
SCRIPT_RANGES = {
    "Deva": (0x0900, 0x097F),
    "Telu": (0x0C00, 0x0C7F),
    "Gujr": (0x0A80, 0x0AFF),
    "Taml": (0x0B80, 0x0BFF),
}


def classify(text: str) -> str:
    """Name the dominant script of a transcript."""
    found = set()
    latin = False
    for ch in text:
        o = ord(ch)
        for name, (lo, hi) in SCRIPT_RANGES.items():
            if lo <= o <= hi:
                found.add(name)
        if "a" <= ch.lower() <= "z":
            latin = True
    if latin:
        found.add("Latn")
    return "+".join(sorted(found)) or "empty"


def say_pcm(text: str, voice: str | None) -> bytes:
    """Synthesise speech locally with macOS ``say`` and return 16 kHz mono PCM."""
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "s.aiff"
        wav = Path(tmp) / "s.wav"
        cmd = ["say", "-o", str(aiff)]
        if voice:
            cmd += ["-v", voice]
        cmd += [text]
        subprocess.run(cmd, check=True, capture_output=True)
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(aiff), "-ar", "16000", "-ac", "1",
                "-f", "s16le", str(wav),
            ],
            check=True,
            capture_output=True,
        )
        return wav.read_bytes()


async def transcribe(pcm: bytes, lang_code: str) -> str:
    """Push one clip through the tuned STT; return transcripts or the error.

    The previous revision referenced ``asyncio.CancelError`` (which does
    not exist -- it is ``CancelledError``), so every row crashed after the
    audio was pushed, the reader task was never cancelled and its server
    session was never closed. The leaked sessions filled the key's
    concurrent-session budget and every later row failed to connect. The
    cleanup below runs in a ``finally`` so a crash can never leak a
    session again, and the reader's exceptions are reported instead of
    swallowed.
    """
    settings = get_settings()
    stt = TunedGnaniSTT(language=lang_code, api_key=settings.gnani_api_key)
    stream = stt.stream()
    final: list[str] = []
    errors: list[str] = []
    got_final = asyncio.Event()

    async def reader() -> None:
        try:
            async for event in stream:
                if event.type.name == "FINAL_TRANSCRIPT":
                    text = event.alternatives[0].text if event.alternatives else ""
                    final.append(text)
                    got_final.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the probe reports it
            errors.append(str(exc)[:200])

    reader_task = asyncio.create_task(reader())
    try:
        frame_size = 1024
        bytes_per_second = 2 * 16000
        for i in range(0, len(pcm), frame_size):
            chunk = pcm[i : i + frame_size]
            stream.push_frame(
                rtc.AudioFrame(
                    data=chunk,
                    sample_rate=16000,
                    num_channels=1,
                    samples_per_channel=len(chunk) // 2,
                )
            )
            await asyncio.sleep(len(chunk) / bytes_per_second)
        try:
            await asyncio.wait_for(got_final.wait(), timeout=10.0)
        except TimeoutError:
            pass
        await asyncio.sleep(1.0)
    finally:
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reader_task
        with contextlib.suppress(Exception):
            await stream.aclose()
    text = " ".join(final).strip()
    if not text and errors:
        return f"<ERROR {errors[0]}>"
    return text


async def main() -> int:
    settings = get_settings()
    if not settings.has_gnani():
        print("GNANI_API_KEY is not set", file=sys.stderr)
        return 1

    clips: dict[str, bytes] = {
        "say-deva(hi)": say_pcm(SPEECH["hi-IN"], VOICE["hi-IN"]),
        "say-en": say_pcm(SPEECH["en-IN"], VOICE["en-IN"]),
        "say-telu": say_pcm(SPEECH["te-IN"], VOICE["te-IN"]),
    }
    # Cached bench clips: real Gnani Timbre audio. Keys are the first 24
    # hex chars of sha256("text|language|pause_seconds") -- see
    # agent/bench/driver.py.
    bench_dir = PROJECT_ROOT / "data/cache/bench/questions"
    known = {
        "bench-hi": "6e0a4dd833553ccfe26f1eb7",
        "bench-en": "a33bae7486004f29e5eecfe5",
        "bench-telu": "3ee72d59306e7e9a23a96fb1",
    }
    for name, key in known.items():
        path = bench_dir / f"{key}.raw"
        if path.exists():
            clips[name] = path.read_bytes()

    print(f"{'lang_code':<34} {'clip':<20} {'script':<16} transcript")
    print("-" * 110)
    for lang in LANG_CODES:
        for name, pcm in clips.items():
            try:
                text = await transcribe(pcm, lang)
            except Exception as exc:  # noqa: BLE001
                text = f"<ERROR {str(exc)[:70]}>"
            print(f"{lang:<34} {name:<20} {classify(text):<16} {text[:70]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
