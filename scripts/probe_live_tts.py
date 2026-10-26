#!/usr/bin/env python
"""Live A/B probe: sentence-streaming TTS wrap vs the plugin's own stream.

The production path wraps the Gnani plugin in ``tts.StreamAdapter``
(``agent/tutor.py: build_tts``) so that each complete sentence becomes its
own WebSocket request as soon as the next sentence starts arriving. The
plugin's own ``stream()`` collects the whole answer first and only then
opens its WebSocket request. This probe drives both paths against the live
endpoint with the same simulated LLM deltas and measures:

1. does the first audio frame leave before the full answer has been
   pushed (wrap), versus only after it (plugin stream);
2. how many WebSocket requests each path opens -- one per sentence vs one
   per answer, and no runaway request count;
3. time-to-first-audio measured from the moment the first request opens.

Two runs of one short four-sentence answer each: roughly a third of a
credit of Gnani TTS (27 credits per audio hour).

Usage:
    .venv/bin/python scripts/probe_live_tts.py [--skip-baseline]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from agent import tutor  # noqa: E402

TEXT = (
    "The OSI model has seven layers, and each layer does one job well. "
    "The data link layer frames packets and hands them to the physical layer. "
    "The network layer chooses the route, while the transport layer keeps the "
    "conversation reliable. "
    "Together these layers move every byte you send across the internet."
)

CHUNK_CHARS = 28
PUSH_GAP_SECONDS = 0.3
DRAIN_TIMEOUT_SECONDS = 90.0
CREDITS_PER_AUDIO_HOUR = 27.0


def make_chunks(text: str, size: int) -> list[str]:
    """Split text at word boundaries into chunks of about ``size`` chars.

    The chunks stand in for LLM token deltas: the wrap can only start
    speaking once a sentence boundary has been seen, so the schedule has
    to give it a chance to speak before the answer finishes arriving.
    """
    chunks: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > size:
            chunks.append(current)
            current = word
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def run_once(
    stream,
    chunks: list[str],
    *,
    ws_events: list[dict],
    ws_start: int,
    synth_calls: list[dict],
    synth_start: int,
) -> dict:
    """Push chunks at a steady pace, then drain the stream to completion."""
    first_audio: float | None = None
    audio_seconds = 0.0
    frame_count = 0
    error: str | None = None

    async def consume() -> None:
        nonlocal first_audio, audio_seconds, frame_count, error
        try:
            async for audio in stream:
                if first_audio is None:
                    first_audio = time.monotonic()
                frame_count += 1
                audio_seconds += audio.frame.duration
        except Exception as exc:  # noqa: BLE001 - a probe reports failures, it does not crash
            error = f"{type(exc).__name__}: {exc}"

    consumer = asyncio.create_task(consume())
    t0 = time.monotonic()
    for chunk in chunks:
        stream.push_text(chunk)
        await asyncio.sleep(PUSH_GAP_SECONDS)
    t_last_push = time.monotonic()
    stream.end_input()
    t_end_input = time.monotonic()

    try:
        await asyncio.wait_for(consumer, timeout=DRAIN_TIMEOUT_SECONDS)
    except TimeoutError:
        error = error or f"no audio stream end within {DRAIN_TIMEOUT_SECONDS:.0f}s"
    finally:
        await stream.aclose()

    events = ws_events[ws_start:]
    requests = synth_calls[synth_start:]
    return {
        "t0": t0,
        "t_last_push": t_last_push,
        "t_end_input": t_end_input,
        "first_audio": first_audio,
        "audio_seconds": audio_seconds,
        "frame_count": frame_count,
        "error": error,
        "ws_events": events,
        "requests": requests,
    }


def offset(run: dict, moment: float | None) -> float | None:
    return None if moment is None else moment - run["t0"]


def show(label: str, run: dict) -> None:
    print(f"\n{label}")
    print(f"  error                    : {run['error']}")
    print(f"  websocket requests       : {len(run['ws_events'])}")
    for i, req in enumerate(run["requests"]):
        print(f"    request {i + 1}: {req['chars']:>4} chars  {req['head']!r}")
    print(f"  full answer pushed at    : {offset(run, run['t_last_push']):.2f}s")
    print(f"  end of input at          : {offset(run, run['t_end_input']):.2f}s")
    if run["first_audio"] is None:
        print("  first audio frame        : NEVER")
    else:
        margin = run["t_last_push"] - run["first_audio"]
        print(
            f"  first audio frame        : {offset(run, run['first_audio']):.2f}s "
            f"({margin:+.2f}s vs full answer pushed)"
        )
    if run["ws_events"] and run["first_audio"] is not None:
        ttfb = run["first_audio"] - run["ws_events"][0]["t"]
        print(f"  first request -> audio   : {ttfb:.2f}s")
    print(f"  audio synthesised        : {run['audio_seconds']:.1f}s in {run['frame_count']} frames")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--skip-baseline",
        action="store_true",
        help="only run the production wrap, not the plugin's own stream",
    )
    args = parser.parse_args()

    import websockets

    adapter = tutor.build_tts("en-IN")
    raw_tts = adapter._wrapped_tts
    chunks = make_chunks(TEXT, CHUNK_CHARS)
    expected_sentences = adapter._sentence_tokenizer.tokenize(TEXT)

    # One spy per layer: synthesize() shows what the wrap hands over (one
    # call per sentence), websockets.connect() counts the requests that
    # actually reach Gnani for both paths.
    synth_calls: list[dict] = []
    orig_synthesize = raw_tts.synthesize

    def spy_synthesize(text: str, **kwargs):
        synth_calls.append(
            {"t": time.monotonic(), "chars": len(text), "head": text[:60]}
        )
        return orig_synthesize(text, **kwargs)

    raw_tts.synthesize = spy_synthesize

    ws_events: list[dict] = []
    orig_ws_connect = websockets.connect

    def spy_ws_connect(url, **kwargs):
        ws_events.append({"t": time.monotonic(), "url": str(url)})
        return orig_ws_connect(url, **kwargs)

    websockets.connect = spy_ws_connect

    print(
        f"text: {len(TEXT)} chars, {len(chunks)} chunks of ~{CHUNK_CHARS} chars, "
        f"gap {PUSH_GAP_SECONDS}s -> ~{len(chunks) * PUSH_GAP_SECONDS:.1f}s of simulated "
        f"LLM generation; tokenizer sees {len(expected_sentences)} sentences"
    )

    ws_mark = len(ws_events)
    synth_mark = len(synth_calls)
    wrapped = await run_once(
        adapter.stream(),
        chunks,
        ws_events=ws_events,
        ws_start=ws_mark,
        synth_calls=synth_calls,
        synth_start=synth_mark,
    )
    show("PRODUCTION WRAP (StreamAdapter)", wrapped)

    raw = None
    if not args.skip_baseline:
        ws_mark = len(ws_events)
        synth_mark = len(synth_calls)
        raw = await run_once(
            raw_tts.stream(),
            chunks,
            ws_events=ws_events,
            ws_start=ws_mark,
            synth_calls=synth_calls,
            synth_start=synth_mark,
        )
        show("PLUGIN OWN STREAM (no wrap, baseline)", raw)

    verdicts: list[tuple[str, bool, str]] = []
    wrapped_audio = wrapped["first_audio"]
    verdicts.append(
        (
            "wrap speaks before the answer finishes",
            wrapped_audio is not None and wrapped_audio < wrapped["t_last_push"],
            "first audio arrived while chunks were still being pushed",
        )
    )
    verdicts.append(
        (
            "one websocket request per sentence",
            2 <= len(wrapped["ws_events"]) <= len(expected_sentences) + 1,
            f"{len(wrapped['ws_events'])} requests for "
            f"{len(expected_sentences)} sentences",
        )
    )
    verdicts.append(
        (
            "no request explosion",
            len(wrapped["ws_events"]) <= len(chunks),
            f"{len(wrapped['ws_events'])} requests for {len(chunks)} pushed chunks",
        )
    )
    if raw is not None:
        raw_audio = raw["first_audio"]
        verdicts.append(
            (
                "baseline opens exactly one request",
                len(raw["ws_events"]) == 1,
                f"{len(raw['ws_events'])} requests",
            )
        )
        verdicts.append(
            (
                "baseline only speaks after the full answer",
                raw_audio is not None and raw_audio >= raw["t_last_push"],
                "first audio at or after end of push",
            )
        )

    total_seconds = wrapped["audio_seconds"] + (raw["audio_seconds"] if raw else 0.0)
    credits = total_seconds / 3600 * CREDITS_PER_AUDIO_HOUR
    print("\n" + "=" * 72)
    print("PROBE VERDICTS")
    print("=" * 72)
    ok = True
    for name, passed, detail in verdicts:
        ok = ok and passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {name} -- {detail}")
    print(f"\n  total audio synthesised  : {total_seconds:.1f}s")
    print(f"  estimated credit cost    : {credits:.3f} (27 credits / audio hour)")
    if wrapped["first_audio"] is not None and wrapped["requests"]:
        ttfb = wrapped["first_audio"] - wrapped["ws_events"][0]["t"]
        print(f"  wrap TTFB (1st request)  : {ttfb:.2f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
