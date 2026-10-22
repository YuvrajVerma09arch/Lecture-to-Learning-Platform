#!/usr/bin/env python
"""Run the notes pipeline over a lecture file and report what happened.

This is the reproducible entry point for the end-to-end run. It prints
the measured timings, the credit estimate, the segment count, and the
resulting notes, and it writes the notes to a file for inspection.

Usage:
    .venv/bin/python scripts/run_pipeline.py <audio-file> [--lang ta-IN] [--title "..."]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app import db as db_mod  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.pipeline import orchestrator  # noqa: E402
from app.pipeline import segment as segment_mod  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("audio", type=Path)
    parser.add_argument("--lang", default="ta-IN")
    parser.add_argument("--title", default=None)
    parser.add_argument("--diarization", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    configure_logging("INFO")
    settings = get_settings()

    if not args.audio.exists():
        print(f"No such file: {args.audio}", file=sys.stderr)
        return 1

    title = args.title or args.audio.stem
    started = time.monotonic()
    progress_events: list[tuple[str, str, float]] = []

    def on_progress(stage: str, detail: str) -> None:
        progress_events.append((stage, detail, time.monotonic() - started))

    try:
        result = await orchestrator.run_notes_pipeline(
            args.audio,
            title=title,
            language_code=args.lang,
            with_diarization=args.diarization,
            on_progress=on_progress,
        )
    except orchestrator.PipelineError as exc:
        print(f"\nPIPELINE FAILED: {exc}", file=sys.stderr)
        return 1

    elapsed = time.monotonic() - started

    print()
    print("=" * 74)
    print("PIPELINE RESULT")
    print("=" * 74)
    print(f"lecture id      : {result.lecture_id}")
    print(f"status          : {result.status}")
    print(f"cached          : {result.cached}")
    print(f"total elapsed   : {elapsed:.1f}s")
    print(f"credits est.    : {result.credits_estimated:.1f}")
    print(f"transcript segs : {len(result.transcript)}")
    for stage, seconds in result.timings.items():
        print(f"  {stage:12} : {seconds:.1f}s")

    print("\nprogress events:")
    for stage, detail, at in progress_events:
        print(f"  t={at:7.1f}s  {stage:12} {detail}")

    with db_mod.get_conn() as conn:
        lecture = conn.execute(
            "SELECT * FROM lectures WHERE id=?", (result.lecture_id,)
        ).fetchone()
        print(f"\nduration        : {lecture['audio_seconds']:.1f}s")
        print(f"content hash    : {lecture['content_hash'][:16]}")

        segs = conn.execute(
            "SELECT COUNT(*) AS n, SUM(visual_dependent) AS v FROM segments WHERE lecture_id=?",
            (result.lecture_id,),
        ).fetchone()
        print(f"topic segments  : {segs['n']} ({segs['v']} visual-dependent)")

        usage = conn.execute(
            "SELECT kind, estimated, actual_seconds, reference FROM credit_usage"
        ).fetchall()
        print("\ncredit usage recorded:")
        for u in usage:
            print(
                f"  {u['kind']:12} est={u['estimated']:.1f} "
                f"audio={u['actual_seconds']:.0f}s ref={u['reference']}"
            )

        note_row = conn.execute(
            "SELECT document, summary FROM notes WHERE lecture_id=?", (result.lecture_id,)
        ).fetchone()

    if note_row is None:
        print("\nNO NOTES were produced.")
        return 1

    document = note_row["document"]
    out_path = args.out or (settings.data_dir / "notes" / f"{result.lecture_id}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(document, encoding="utf-8")

    print(f"\nnotes written to : {out_path}")
    print(f"summary          : {note_row['summary'][:300]}")
    print("=" * 74)
    print("NOTES (first 4000 characters)")
    print("=" * 74)
    print(document[:4000])

    # A quick look at the first few topic segments, for the quality check.
    with db_mod.get_conn() as conn:
        rows = conn.execute(
            "SELECT seq, start_seconds, end_seconds, text, visual_dependent "
            "FROM segments WHERE lecture_id=? ORDER BY seq LIMIT 5",
            (result.lecture_id,),
        ).fetchall()
    print()
    print("=" * 74)
    print("FIRST SEGMENTS (source of truth for the notes)")
    print("=" * 74)
    for r in rows:
        flag = " [VISUAL-DEPENDENT]" if r["visual_dependent"] else ""
        print(
            f"\n[{r['seq']}] "
            f"{segment_mod.format_timestamp(r['start_seconds'])}-"
            f"{segment_mod.format_timestamp(r['end_seconds'])}{flag}"
        )
        print(f"    {r['text'][:400]}")

    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
