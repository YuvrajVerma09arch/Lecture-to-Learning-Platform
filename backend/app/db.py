"""SQLite storage.

One file holds the whole application state. That keeps the deployment to a
single artifact and makes the "resumable, never re-transcribe" guarantee
easy to reason about: the transcript is durable on disk before any
downstream stage runs.

Schema notes:
  * ``lectures.status`` is a state machine. Each stage writes its own
    completion so a failure in notes cannot lose a finished transcript.
  * ``transcripts.content_hash`` is UNIQUE. A cache hit is a lookup, not a
    re-transcription, and the hash survives restarts.
  * ``segments`` are the RAG corpus: timestamped, ordered, and traceable
    back to the exact source span.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import get_settings

# --- Lecture lifecycle ---------------------------------------------------
# A lecture moves forward through these; a failure parks it in a *_FAILED
# state that names the stage that failed, so the stage can be retried alone.
STATUS_UPLOADED = "uploaded"
STATUS_TRANSCRIBING = "transcribing"
STATUS_TRANSCRIBED = "transcribed"
STATUS_SEGMENTING = "segmenting"
STATUS_SEGMENTED = "segmented"
STATUS_NOTING = "noting"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

STAGE_INGEST = "ingest"
STAGE_TRANSCRIBE = "transcribe"
STAGE_SEGMENT = "segment"
STAGE_NOTE = "note"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS lectures (
    id                TEXT PRIMARY KEY,
    title             TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    audio_seconds     REAL NOT NULL DEFAULT 0,
    language_code     TEXT NOT NULL DEFAULT 'en-IN',
    status            TEXT NOT NULL DEFAULT 'uploaded',
    failed_stage      TEXT,
    failure_reason    TEXT,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

-- One row per distinct audio content. The UNIQUE hash is what makes
-- re-uploading the same file free: we find the cached transcript and skip
-- the Gnani job entirely.
CREATE TABLE IF NOT EXISTS transcripts (
    id             TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL UNIQUE,
    source_path    TEXT NOT NULL,
    language_code  TEXT NOT NULL,
    audio_seconds  REAL NOT NULL,
    segments_json  TEXT NOT NULL,
    full_text      TEXT NOT NULL,
    gnani_job_id   TEXT,
    created_at     REAL NOT NULL
);

-- RAG corpus + provenance. Every note and every answer cites segment ids
-- from this table.
CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lecture_id    TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds   REAL NOT NULL,
    text          TEXT NOT NULL,
    speaker_id    INTEGER,
    visual_dependent INTEGER NOT NULL DEFAULT 0,
    UNIQUE (lecture_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_segments_lecture ON segments(lecture_id, seq);

-- The merged, human-facing notes document.
CREATE TABLE IF NOT EXISTS notes (
    id          TEXT PRIMARY KEY,
    lecture_id  TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    document    TEXT NOT NULL,
    summary     TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_lecture ON notes(lecture_id);

-- Per-stage checkpoints. A stage is re-runnable in isolation.
CREATE TABLE IF NOT EXISTS stage_state (
    lecture_id  TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    detail      TEXT,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (lecture_id, stage)
);

-- Credit accounting. Kept locally so we can enforce a daily ceiling without
-- querying Gnani.
CREATE TABLE IF NOT EXISTS credit_usage (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    day            TEXT NOT NULL,
    kind           TEXT NOT NULL,
    estimated      REAL NOT NULL,
    actual_seconds REAL NOT NULL DEFAULT 0,
    reference      TEXT,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_credit_day ON credit_usage(day);

-- Flashcards (Phase 3) and their spaced-repetition state.
CREATE TABLE IF NOT EXISTS flashcards (
    id           TEXT PRIMARY KEY,
    lecture_id   TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    question     TEXT NOT NULL,
    answer       TEXT NOT NULL,
    segment_ids  TEXT NOT NULL DEFAULT '[]',
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flashcards_lecture ON flashcards(lecture_id);

CREATE TABLE IF NOT EXISTS flashcard_reviews (
    flashcard_id TEXT NOT NULL REFERENCES flashcards(id) ON DELETE CASCADE,
    ease         REAL NOT NULL DEFAULT 2.5,
    interval_days REAL NOT NULL DEFAULT 0,
    repetitions  INTEGER NOT NULL DEFAULT 0,
    lapses       INTEGER NOT NULL DEFAULT 0,
    due_at       REAL NOT NULL,
    last_reviewed_at REAL,
    PRIMARY KEY (flashcard_id)
);

-- FTS5 index over the RAG corpus. unicode61 tokenises Devanagari and
-- Gujarati correctly (verified against real text during Phase 0), and
-- matches the romanised Hinglish vocabulary a student actually types.
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
    text,
    content='segments',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS segments_ai AFTER INSERT ON segments BEGIN
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_ad AFTER DELETE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS segments_au AFTER UPDATE ON segments BEGIN
    INSERT INTO segments_fts(segments_fts, rowid, text) VALUES ('delete', old.id, old.text);
    INSERT INTO segments_fts(rowid, text) VALUES (new.id, new.text);
END;
"""


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """A connection with the schema ensured. Used for migrations and CLI tools."""
    settings = get_settings()
    conn = _connect(settings.db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    """Create the schema. Idempotent."""
    with get_conn() as conn:
        conn.executescript(SCHEMA)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def now() -> float:
    return time.time()


def set_status(
    conn: sqlite3.Connection,
    lecture_id: str,
    status: str,
    *,
    failed_stage: str | None = None,
    failure_reason: str | None = None,
) -> None:
    conn.execute(
        "UPDATE lectures SET status=?, failed_stage=?, failure_reason=?, updated_at=? WHERE id=?",
        (status, failed_stage, failure_reason, now(), lecture_id),
    )


def set_stage(
    conn: sqlite3.Connection,
    lecture_id: str,
    stage: str,
    status: str,
    detail: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO stage_state (lecture_id, stage, status, detail, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(lecture_id, stage) DO UPDATE SET
             status=excluded.status, detail=excluded.detail, updated_at=excluded.updated_at""",
        (lecture_id, stage, status, detail, now()),
    )


def get_stage_states(conn: sqlite3.Connection, lecture_id: str) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT stage, status, detail FROM stage_state WHERE lecture_id=?", (lecture_id,)
    ).fetchall()
    return {r["stage"]: {"status": r["status"], "detail": r["detail"]} for r in rows}


def cached_transcript(conn: sqlite3.Connection, content_hash: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM transcripts WHERE content_hash=?", (content_hash,)
    ).fetchone()


def store_transcript(
    conn: sqlite3.Connection,
    *,
    content_hash: str,
    source_path: str,
    language_code: str,
    audio_seconds: float,
    segments: list[dict[str, Any]],
    full_text: str,
    gnani_job_id: str | None = None,
) -> str:
    transcript_id = new_id("tr")
    conn.execute(
        """INSERT INTO transcripts
             (id, content_hash, source_path, language_code, audio_seconds,
              segments_json, full_text, gnani_job_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            transcript_id,
            content_hash,
            source_path,
            language_code,
            audio_seconds,
            json.dumps(segments, ensure_ascii=False),
            full_text,
            gnani_job_id,
            now(),
        ),
    )
    return transcript_id


def replace_segments(
    conn: sqlite3.Connection,
    lecture_id: str,
    segments: list[dict[str, Any]],
) -> int:
    """Replace a lecture's segments. Idempotent, so a re-run is safe."""
    conn.execute("DELETE FROM segments WHERE lecture_id=?", (lecture_id,))
    for seq, seg in enumerate(segments):
        conn.execute(
            """INSERT INTO segments
                 (lecture_id, seq, start_seconds, end_seconds, text, speaker_id, visual_dependent)
               VALUES (?,?,?,?,?,?,?)""",
            (
                lecture_id,
                seq,
                float(seg.get("start_seconds", 0.0)),
                float(seg.get("end_seconds", 0.0)),
                seg.get("text", ""),
                seg.get("speaker_id"),
                1 if seg.get("visual_dependent") else 0,
            ),
        )
    return len(segments)


def record_credit(
    conn: sqlite3.Connection,
    *,
    kind: str,
    estimated: float,
    actual_seconds: float = 0.0,
    reference: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO credit_usage (day, kind, estimated, actual_seconds, reference, created_at)
           VALUES (?,?,?,?,?,?)""",
        (time.strftime("%Y-%m-%d", time.gmtime()), kind, estimated, actual_seconds, reference, now()),
    )


def credits_used_today(conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(estimated), 0) AS total FROM credit_usage WHERE day=?",
        (time.strftime("%Y-%m-%d", time.gmtime()),),
    ).fetchone()
    return float(row["total"] or 0.0)
