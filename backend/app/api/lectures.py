"""The notes API: upload, list, and read.

The upload endpoint runs the pipeline in the background and reports
progress, so a 45-minute lecture does not hold an HTTP request open for
six minutes. The job state lives in the database, so a browser reload
does not lose it.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile

from .. import db as db_mod
from ..config import get_settings
from ..logging_setup import get_logger
from ..pipeline import orchestrator

log = get_logger(__name__)

router = APIRouter(prefix="/api/lectures", tags=["lectures"])

#: Uploads are written here and removed once the pipeline has read them.
UPLOAD_DIR = Path(tempfile.gettempdir()) / "gnani-lecture-uploads"

#: Extensions the upload endpoint accepts. The pipeline validates the
#: file properly; this is the cheap first gate.
ALLOWED_SUFFIXES = {".wav", ".mp3", ".mp4", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm", ".amr"}


def _serialize_lecture(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "original_filename": row["original_filename"],
        "audio_seconds": row["audio_seconds"],
        "language_code": row["language_code"],
        "status": row["status"],
        "failed_stage": row["failed_stage"],
        "failure_reason": row["failure_reason"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


@router.post("/upload")
async def upload_lecture(
    background: BackgroundTasks,
    file: UploadFile = File(...),  # noqa: B008 - FastAPI dependency
    title: str = Form(...),
    language_code: str = Form("en-IN"),
) -> dict[str, Any]:
    """Accept a lecture recording and start the pipeline.

    Returns immediately with the lecture id. Poll
    ``GET /api/lectures/{id}`` for the status.
    """
    settings = get_settings()
    if not settings.has_gnani():
        raise HTTPException(status_code=503, detail="GNANI_API_KEY is not configured")

    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{suffix or file.filename}'. "
            f"Supported: {', '.join(sorted(ALLOWED_SUFFIXES))}.",
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # A unique name per upload: two students can upload the same
    # filename at the same time.
    stem = f"{int(time.time() * 1000)}-{abs(hash(file.filename or '')) % 100000}"
    target = UPLOAD_DIR / f"{stem}{suffix}"

    size = 0
    try:
        with target.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File is too large. The limit is "
                        f"{settings.max_upload_bytes // (1024 * 1024)} MB.",
                    )
                out.write(chunk)
    except HTTPException:
        target.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    log.info(
        "lecture upload accepted",
        extra={"size_bytes": size, "language_code": language_code, "title": title[:120]},
    )

    background.add_task(_run_pipeline, target, title, language_code)
    return {"status": "accepted", "filename": target.name, "size_bytes": size}


def _run_pipeline(path: Path, title: str, language_code: str) -> None:
    """Run the pipeline off the request thread."""
    from ..logging_setup import configure_logging

    configure_logging("INFO")

    async def go() -> None:
        try:
            result = await orchestrator.run_notes_pipeline(
                path, title=title, language_code=language_code
            )
            log.info(
                "lecture processed",
                extra={
                    "lecture_id": result.lecture_id,
                    "cached": result.cached,
                    "elapsed_seconds": round(result.elapsed_seconds, 1),
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.error("lecture pipeline failed", extra={"error": str(exc)[:300]})
        finally:
            path.unlink(missing_ok=True)

    try:
        asyncio.run(go())
    except Exception as exc:  # noqa: BLE001
        log.error("could not start the pipeline", extra={"error": str(exc)[:300]})
        path.unlink(missing_ok=True)


@router.get("")
async def list_lectures() -> dict[str, Any]:
    with db_mod.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM lectures ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
    return {"lectures": [_serialize_lecture(r) for r in rows]}


@router.get("/{lecture_id}")
async def get_lecture(lecture_id: str) -> dict[str, Any]:
    with db_mod.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM lectures WHERE id=?", (lecture_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such lecture")
        stages = db_mod.get_stage_states(conn, lecture_id)
        segments = conn.execute(
            "SELECT COUNT(*) AS n, SUM(visual_dependent) AS v FROM segments "
            "WHERE lecture_id=?",
            (lecture_id,),
        ).fetchone()
        usage = conn.execute(
            "SELECT kind, estimated, actual_seconds FROM credit_usage ORDER BY id DESC LIMIT 5"
        ).fetchall()

    payload = _serialize_lecture(row)
    payload["stages"] = stages
    payload["segment_count"] = segments["n"] or 0
    payload["visual_dependent_count"] = segments["v"] or 0
    payload["recent_credit_usage"] = [
        {
            "kind": u["kind"],
            "estimated": u["estimated"],
            "actual_seconds": u["actual_seconds"],
        }
        for u in usage
    ]
    return payload


@router.get("/{lecture_id}/notes")
async def get_notes(lecture_id: str) -> dict[str, Any]:
    """The merged notes document, plus the source segments."""
    with db_mod.get_conn() as conn:
        lecture = conn.execute(
            "SELECT * FROM lectures WHERE id=?", (lecture_id,)
        ).fetchone()
        if lecture is None:
            raise HTTPException(status_code=404, detail="No such lecture")
        note = conn.execute(
            "SELECT document, summary, created_at FROM notes WHERE lecture_id=?",
            (lecture_id,),
        ).fetchone()
        segments = conn.execute(
            "SELECT seq, start_seconds, end_seconds, text, visual_dependent "
            "FROM segments WHERE lecture_id=? ORDER BY seq",
            (lecture_id,),
        ).fetchall()

    return {
        "lecture_id": lecture_id,
        "title": lecture["title"],
        "status": lecture["status"],
        "document": note["document"] if note else None,
        "summary": note["summary"] if note else "",
        "segments": [
            {
                "seq": s["seq"],
                "start_seconds": s["start_seconds"],
                "end_seconds": s["end_seconds"],
                "text": s["text"],
                "visual_dependent": bool(s["visual_dependent"]),
            }
            for s in segments
        ],
    }
