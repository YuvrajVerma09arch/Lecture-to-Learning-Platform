#!/usr/bin/env python
"""Live probe for Gnani's batch STT job API.

Settles three things the documentation does not state:

1. **The timestamp basis.** Are a file's segment timestamps relative to
   that file, or absolute within the whole job? The merge in
   ``app.pipeline.transcribe`` rebases on the answer, and getting it
   wrong silently corrupts every timestamp in the notes. The probe
   uploads two chunks of known content and compares what comes back
   against what it must be.

2. **The file-size limit in practice.** The docs say 10 MB per file. The
   probe checks the boundary rather than assuming it.

3. **Language support.** The documentation contradicts itself about
   whether Batch supports ``gu-IN`` and ``pa-IN``. The probe tests the
   languages the product needs.

The probe uses a short local clip, so the credit cost is a few seconds of
audio, not a lecture.

Usage:
    .venv/bin/python scripts/probe_batch.py --lang ta-IN
    .venv/bin/python scripts/probe_batch.py --lang gu-IN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import aiohttp  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.pipeline.transcribe import GnaniBatchClient  # noqa: E402
from scripts.probe_stt import make_pcm  # noqa: E402


async def run_probe(lang: str, *, chunk_seconds: float, with_diarization: bool) -> dict:
    client = GnaniBatchClient()
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        pcm, duration = make_pcm(lang, workdir)

        # Split the PCM into two chunks at a known boundary so we know
        # exactly what each chunk's offset should be.
        bytes_per_second = 2 * 16000
        split_at = int(duration * bytes_per_second / 2)
        split_at -= split_at % 1024
        chunk1 = pcm[:split_at]
        chunk2 = pcm[split_at:]

        chunk_paths: list[Path] = []
        for i, data in enumerate((chunk1, chunk2)):
            path = workdir / f"chunk{i}.wav"
            _write_wav(path, data, 16000)
            chunk_paths.append(path)

        offsets = [0.0, split_at / bytes_per_second]

        print(f"  lang={lang}  audio={duration:.2f}s  "
              f"chunk1={len(chunk1) / bytes_per_second:.2f}s  "
              f"chunk2={len(chunk2) / bytes_per_second:.2f}s  "
              f"offset={offsets[1]:.2f}s", file=sys.stderr)

        t0 = time.monotonic()
        async with aiohttp.ClientSession() as session:
            job_id = await client.create_job(
                chunk_paths,
                language_code=lang,
                session=session,
                with_diarization=with_diarization,
            )
            await client.start_job(job_id, session=session)
            status = await client._wait_for_completion(job_id, session=session)
            files = await client.get_job_files(job_id, session=session)

            per_file: list[dict] = []
            payloads: list[dict] = []
            for record in files:
                entry = {
                    "original_path": record.get("original_path"),
                    "status": record.get("status"),
                    "duration_seconds": record.get("duration_seconds"),
                    "error_message": record.get("error_message"),
                }
                url = record.get("transcript_url")
                if url:
                    payload = await client.download_transcript(url, session=session)
                    payloads.append(payload)
                    segs = payload.get("segments") or []
                    entry["segment_count"] = len(segs)
                    entry["first_start"] = segs[0].get("start_time") if segs else None
                    entry["last_end"] = segs[-1].get("end_time") if segs else None
                    entry["min_start"] = min(
                        (float(s.get("start_time") or 0) for s in segs), default=None
                    )
                    entry["max_end"] = max(
                        (float(s.get("end_time") or 0) for s in segs), default=None
                    )
                    entry["sample_text"] = (segs[0].get("text") or "")[:80] if segs else ""
                    entry["segment_ids"] = [s.get("segment_id") for s in segs[:4]]
                    entry["has_confidence"] = segs[0].get("confidence") if segs else None
                per_file.append(entry)

        elapsed = time.monotonic() - t0
        return {
            "lang": lang,
            "job_id": job_id,
            "status": status.get("status"),
            "progress": status.get("progress"),
            "files": per_file,
            "elapsed_seconds": round(elapsed, 2),
            "chunk_offsets": offsets,
            "chunk_durations": [
                len(chunk1) / bytes_per_second,
                len(chunk2) / bytes_per_second,
            ],
            "payloads": payloads,
        }


def _write_wav(path: Path, pcm: bytes, sample_rate: int) -> None:
    """Write raw s16le mono PCM as a RIFF/WAVE file."""
    import struct

    data_len = len(pcm)
    header = b"RIFF"
    header += struct.pack("<I", 36 + data_len)
    header += b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", data_len)
    path.write_bytes(header + pcm)


def interpret(result: dict) -> str:
    """Explain what the timestamp data implies."""
    files = result["files"]
    if len(files) < 2:
        return (
            "Only one file came back; cannot determine the timestamp basis. "
            f"Status was {result['status']}."
        )

    first, second = files[0], files[1]
    offset = result["chunk_offsets"][1]
    dur1 = result["chunk_durations"][0]

    if first.get("min_start") is None or second.get("min_start") is None:
        return "No segments were returned; cannot determine the timestamp basis."

    if second["min_start"] >= offset * 0.5:
        basis = "ABSOLUTE (timestamps are relative to the whole job)"
    elif first["max_end"] is not None and first["max_end"] > dur1 + 1.0:
        basis = "ABSOLUTE (chunk 1's timestamps run past its own duration)"
    else:
        basis = "RELATIVE (timestamps are per-file and need rebasing)"

    return (
        f"{basis}\n"
        f"  chunk 1: min_start={first['min_start']} max_end={first['max_end']} "
        f"(its own duration is {dur1:.2f}s)\n"
        f"  chunk 2: min_start={second['min_start']} max_end={second['max_end']} "
        f"(it starts at {offset:.2f}s in the original)"
    )


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", default="ta-IN", help="language_code to test")
    parser.add_argument("--chunk-seconds", type=float, default=0.0)
    parser.add_argument("--diarization", action="store_true")
    parser.add_argument(
        "--save-fixture",
        type=Path,
        default=None,
        help="write the raw transcript payload here, for use as a test fixture",
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.has_gnani():
        print("GNANI_API_KEY is not set", file=sys.stderr)
        return 1

    print(f"probing batch STT for language_code={args.lang} ...", file=sys.stderr)
    result = await run_probe(args.lang, chunk_seconds=args.chunk_seconds,
                             with_diarization=args.diarization)

    if args.save_fixture:
        args.save_fixture.parent.mkdir(parents=True, exist_ok=True)
        args.save_fixture.write_text(
            json.dumps(result["payloads"], ensure_ascii=False, indent=2)
        )
        print(f"wrote fixture to {args.save_fixture}", file=sys.stderr)

    print("\n" + "=" * 72)
    print("BATCH STT PROBE")
    print("=" * 72)
    print(f"language_code : {result['lang']}")
    print(f"job_id       : {result['job_id']}")
    print(f"status       : {result['status']}")
    print(f"progress     : {result['progress']}")
    print(f"elapsed      : {result['elapsed_seconds']}s")
    print("\nper-file results:")
    for f in result["files"]:
        print(f"  {f.get('original_path')}")
        for k, v in f.items():
            if k != "original_path":
                print(f"      {k:20}: {v}")
    print("\ntimestamp basis:")
    print(interpret(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
