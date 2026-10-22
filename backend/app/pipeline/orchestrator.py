"""The notes-pipeline orchestrator.

Ties the stages together and owns the state machine:

    uploaded -> transcoding -> transcribed -> segmenting -> segmented
             -> noting -> ready

Two properties matter and are tested:

* **A later stage's failure never destroys an earlier stage's output.** The
  transcript is committed to the database before segmentation starts, and
  the notes stage is only allowed to fail after that. Re-running a failed
  stage resumes from the last good checkpoint.
* **Identical audio is never transcribed twice.** The cache key is the
  SHA-256 of the uploaded file, so a re-upload is a lookup.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import get_settings
from ..db import (
    STAGE_NOTE,
    STAGE_SEGMENT,
    STAGE_TRANSCRIBE,
    STATUS_FAILED,
    STATUS_NOTING,
    STATUS_READY,
    STATUS_SEGMENTED,
    STATUS_SEGMENTING,
    STATUS_TRANSCRIBED,
    STATUS_TRANSCRIBING,
    STATUS_UPLOADED,
    cached_transcript,
    credits_used_today,
    get_conn,
    new_id,
    now,
    record_credit,
    replace_segments,
    set_stage,
    set_status,
    store_transcript,
)
from ..logging_setup import get_logger
from . import ingest as ingest_mod
from . import notes as notes_mod
from . import segment as segment_mod
from . import transcribe as transcribe_mod

log = get_logger(__name__)


class PipelineError(Exception):
    """A pipeline failure with a user-facing message."""


@dataclass
class PipelineResult:
    lecture_id: str
    status: str
    transcript: list[dict[str, Any]]
    notes_document: notes_mod.NotesDocument | None = None
    cached: bool = False
    credits_estimated: float = 0.0
    elapsed_seconds: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)


async def run_notes_pipeline(
    source_path: Path,
    *,
    title: str,
    language_code: str = "en-IN",
    with_diarization: bool = False,
    num_speakers: int | None = None,
    with_denoise: bool = False,
    bias_list: list[str] | None = None,
    on_progress: Any = None,
) -> PipelineResult:
    """Ingest a lecture and produce notes.

    ``on_progress`` is called with ``(stage, detail)`` so an API layer can
    report progress to the user.
    """
    settings = get_settings()
    started = time.monotonic()
    timings: dict[str, float] = {}

    def progress(stage: str, detail: str) -> None:
        log.info("pipeline progress", extra={"stage": stage, "detail": detail})
        if on_progress is not None:
            on_progress(stage, detail)

    # --- Ingest -----------------------------------------------------------
    progress("ingest", "validating upload")
    t0 = time.monotonic()
    media = ingest_mod.validate_upload(source_path)
    digest = ingest_mod.content_hash(source_path)
    work_dir = ingest_mod.work_dir_for(digest)
    timings["ingest"] = time.monotonic() - t0

    # --- Cache check ------------------------------------------------------
    with get_conn() as conn:
        cached = cached_transcript(conn, digest)
        lecture_id = None

        if cached is not None:
            log.info(
                "transcript cache hit, skipping transcription",
                extra={"content_hash": digest[:12]},
            )
            progress("transcribe", "cache hit, no transcription needed")
            timings["transcribe"] = 0.0

            # Find or create the lecture row for this content.
            row = conn.execute(
                "SELECT id FROM lectures WHERE content_hash=? ORDER BY created_at LIMIT 1",
                (digest,),
            ).fetchone()
            if row is not None:
                lecture_id = row["id"]
            else:
                lecture_id = new_id("lec")
                conn.execute(
                    """INSERT INTO lectures
                         (id, title, original_filename, content_hash, audio_seconds,
                          language_code, status, created_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        lecture_id,
                        title,
                        source_path.name,
                        digest,
                        media.duration_seconds,
                        language_code,
                        STATUS_UPLOADED,
                        now(),
                        now(),
                    ),
                )
            transcript_segments = json.loads(cached["segments_json"])
        else:
            lecture_id = new_id("lec")
            conn.execute(
                """INSERT INTO lectures
                     (id, title, original_filename, content_hash, audio_seconds,
                      language_code, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    lecture_id,
                    title,
                    source_path.name,
                    digest,
                    media.duration_seconds,
                    language_code,
                    STATUS_UPLOADED,
                    now(),
                    now(),
                ),
            )

    if cached is not None:
        return await _continue_after_transcript(
            lecture_id=lecture_id,
            title=title,
            language_code=language_code,
            transcript_segments=transcript_segments,
            timings=timings,
            started=started,
            progress=progress,
            cached=True,
        )

    # --- Transcode and chunk ---------------------------------------------
    progress("ingest", "transcoding and chunking")
    t0 = time.monotonic()
    try:
        work = ingest_mod.ingest(source_path, work_dir)
    except ingest_mod.IngestError as exc:
        await _mark_failed(lecture_id, STAGE_TRANSCRIBE, str(exc))
        raise PipelineError(str(exc)) from exc
    timings["ingest"] = timings.get("ingest", 0.0) + (time.monotonic() - t0)

    # --- Credit ceiling ---------------------------------------------------
    estimated = settings.estimated_credits(media.duration_seconds)
    with get_conn() as conn:
        used = credits_used_today(conn)
    if used + estimated > settings.daily_credit_ceiling:
        await _mark_failed(
            lecture_id,
            STAGE_TRANSCRIBE,
            f"Daily credit ceiling reached ({used:.0f} used, "
            f"{estimated:.0f} needed, cap {settings.daily_credit_ceiling:.0f})",
        )
        raise PipelineError(
            f"This transcription would exceed the daily credit ceiling of "
            f"{settings.daily_credit_ceiling:.0f}. {used:.0f} credits have been "
            f"used today. Raise GNANI_CREDIT_CEILING or wait until tomorrow."
        )

    # --- Transcribe -------------------------------------------------------
    progress("transcribe", f"submitting {len(work.chunks)} chunk(s) to Gnani")
    t0 = time.monotonic()
    with get_conn() as conn:
        set_status(conn, lecture_id, STATUS_TRANSCRIBING)
        set_stage(conn, lecture_id, STAGE_TRANSCRIBE, "running",
                  f"{len(work.chunks)} chunk(s)")

    try:
        client = transcribe_mod.GnaniBatchClient()
        result = await client.transcribe(
            [c.path for c in work.chunks],
            language_code=language_code,
            chunk_offsets=[c.start_seconds for c in work.chunks],
            with_diarization=with_diarization,
            num_speakers=num_speakers,
            with_denoise=with_denoise,
            bias_list=bias_list,
            on_progress=lambda state, p: progress("transcribe", f"job {state}"),
        )
    except transcribe_mod.TranscribeError as exc:
        await _mark_failed(lecture_id, STAGE_TRANSCRIBE, str(exc))
        raise PipelineError(str(exc)) from exc

    timings["transcribe"] = time.monotonic() - t0

    if not result.segments:
        await _mark_failed(lecture_id, STAGE_TRANSCRIBE, "no transcript segments returned")
        raise PipelineError("Gnani returned no transcript for this recording")

    # --- Commit the transcript -------------------------------------------
    # This happens before any downstream stage, so a later failure cannot
    # lose it.
    segment_dicts = [s.to_dict() for s in result.segments]
    with get_conn() as conn:
        store_transcript(
            conn,
            content_hash=digest,
            source_path=str(work.source_path),
            language_code=language_code,
            audio_seconds=media.duration_seconds,
            segments=segment_dicts,
            full_text=result.full_text,
            gnani_job_id=result.job_id,
        )
        record_credit(
            conn,
            kind="batch_stt",
            estimated=estimated,
            actual_seconds=media.duration_seconds,
            reference=result.job_id,
        )
        set_status(conn, lecture_id, STATUS_TRANSCRIBED)
        set_stage(conn, lecture_id, STAGE_TRANSCRIBE, "done", f"{len(segment_dicts)} segments")
        conn.commit()

    progress("transcribe", f"transcribed {len(segment_dicts)} segments")

    return await _continue_after_transcript(
        lecture_id=lecture_id,
        title=title,
        language_code=language_code,
        transcript_segments=segment_dicts,
        timings=timings,
        started=started,
        progress=progress,
        cached=False,
        credits_estimated=estimated,
    )


async def _continue_after_transcript(
    *,
    lecture_id: str,
    title: str,
    language_code: str,
    transcript_segments: list[dict[str, Any]],
    timings: dict[str, float],
    started: float,
    progress: Any,
    cached: bool,
    credits_estimated: float = 0.0,
) -> PipelineResult:
    """Segment, note, and merge. Safe to re-run on its own."""
    # --- Segment ----------------------------------------------------------
    progress("segment", "building topic segments")
    t0 = time.monotonic()
    with get_conn() as conn:
        set_status(conn, lecture_id, STATUS_SEGMENTING)
        set_stage(conn, lecture_id, STAGE_SEGMENT, "running")

    normalised = segment_mod.normalise_segments(transcript_segments)
    segments = segment_mod.segment_transcript(normalised)

    with get_conn() as conn:
        replace_segments(conn, lecture_id, [s.to_dict() for s in segments])
        set_status(conn, lecture_id, STATUS_SEGMENTED)
        set_stage(conn, lecture_id, STAGE_SEGMENT, "done", f"{len(segments)} topic segments")
        conn.commit()
    timings["segment"] = time.monotonic() - t0

    # --- Notes ------------------------------------------------------------
    progress("note", f"generating notes for {len(segments)} segments")
    t0 = time.monotonic()
    with get_conn() as conn:
        set_status(conn, lecture_id, STATUS_NOTING)
        set_stage(conn, lecture_id, STAGE_NOTE, "running")

    try:
        segment_notes = await notes_mod.generate_segment_notes(segments)
        if segment_notes:
            try:
                document = await notes_mod.merge_notes(segments, segment_notes, title=title)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "merge failed, falling back to per-segment document",
                    extra={"error": str(exc)[:200]},
                )
                document = notes_mod.build_fallback_document(segments, segment_notes, title=title)
        else:
            raise notes_mod.LLMError("No segment notes could be generated")
    except Exception as exc:  # noqa: BLE001
        # The transcript and segments survive. Only this lecture's notes are
        # missing, and the stage is marked failed so it can be retried.
        await _mark_failed(lecture_id, STAGE_NOTE, str(exc))
        raise PipelineError(
            f"Note generation failed: {exc}. The transcript is saved and can be "
            f"reprocessed without transcribing again."
        ) from exc

    timings["note"] = time.monotonic() - t0

    with get_conn() as conn:
        conn.execute("DELETE FROM notes WHERE lecture_id=?", (lecture_id,))
        conn.execute(
            "INSERT INTO notes (id, lecture_id, document, summary, created_at) VALUES (?,?,?,?,?)",
            (
                new_id("nt"),
                lecture_id,
                document.to_markdown(),
                document.summary,
                now(),
            ),
        )
        set_status(conn, lecture_id, STATUS_READY)
        set_stage(conn, lecture_id, STAGE_NOTE, "done", f"{len(document.sections)} sections")
        conn.commit()

    progress("note", "notes ready")

    return PipelineResult(
        lecture_id=lecture_id,
        status=STATUS_READY,
        transcript=transcript_segments,
        notes_document=document,
        cached=cached,
        credits_estimated=credits_estimated,
        elapsed_seconds=time.monotonic() - started,
        timings=timings,
    )


async def _mark_failed(lecture_id: str, stage: str, reason: str) -> None:
    with get_conn() as conn:
        set_status(conn, lecture_id, STATUS_FAILED, failed_stage=stage, failure_reason=reason[:500])
        set_stage(conn, lecture_id, stage, "failed", reason[:500])
        conn.commit()
    log.error("pipeline stage failed", extra={"stage": stage, "reason": reason[:200]})
