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

import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import get_settings
from .logging_setup import get_logger

log = get_logger(__name__)

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

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

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

-- One row per distinct *transcription request*, not per file. The
-- key includes the language code, because the language is not a hint:
-- it decides the script the transcript is written in, so a Telugu
-- lecture transcribed as `ta-IN` comes back in Tamil script. The UNIQUE
-- key is what makes re-uploading the same file free: we find the cached
-- transcript and skip the Gnani job entirely.
CREATE TABLE IF NOT EXISTS transcripts (
    id             TEXT PRIMARY KEY,
    content_hash   TEXT NOT NULL UNIQUE,
    source_hash    TEXT NOT NULL,
    source_path    TEXT NOT NULL,
    language_code  TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT '',
    audio_seconds  REAL NOT NULL,
    segments_json  TEXT NOT NULL,
    full_text      TEXT NOT NULL,
    gnani_job_id   TEXT,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transcripts_source ON transcripts(source_hash);

-- The RAG corpus: the source transcript segments, one per contiguous
-- span of speech. The brief asks for these specifically: "Preserve the
-- source segments separately because they will serve as
-- retrieval-augmented generation (RAG) chunks."
--
-- They are deliberately fine-grained. A topic grouping is several
-- thousand characters, and putting five of those in a prompt is
-- measured to exceed the provider's per-minute input-token budget. A
-- source segment is a few hundred characters, so a useful amount of
-- context fits comfortably.
CREATE TABLE IF NOT EXISTS segments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lecture_id    TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds   REAL NOT NULL,
    text          TEXT NOT NULL,
    speaker_id    INTEGER,
    visual_dependent INTEGER NOT NULL DEFAULT 0,
    topic_seq     INTEGER,
    UNIQUE (lecture_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_segments_lecture ON segments(lecture_id, seq);
CREATE INDEX IF NOT EXISTS idx_segments_topic ON segments(lecture_id, topic_seq);

-- The topic groupings. These are the unit of note generation and the
-- unit the merged document cites; a topic spans a range of source
-- segments.
CREATE TABLE IF NOT EXISTS note_topics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    lecture_id    TEXT NOT NULL REFERENCES lectures(id) ON DELETE CASCADE,
    seq           INTEGER NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds   REAL NOT NULL,
    text          TEXT NOT NULL,
    visual_dependent INTEGER NOT NULL DEFAULT 0,
    heading       TEXT,
    explanation   TEXT,
    UNIQUE (lecture_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_topics_lecture ON note_topics(lecture_id, seq);

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
    """A connection with the schema ensured and migrated."""
    settings = get_settings()
    conn = _connect(settings.db_path)
    try:
        _ensure_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


#: Bump this when the derived tables change shape.
#:
#: A schema change drops and rebuilds the *derived* tables -- segments,
#: topics, notes, embeddings -- but never the ``transcripts`` table.
#: The transcript is the expensive artefact: it cost a Gnani job and
#: real credits to produce, and everything downstream can be rebuilt
#: from it for free. Losing it to a schema change would be the worst
#: possible outcome.
SCHEMA_VERSION = 2

#: Tables that can be rebuilt from a transcript.
DERIVED_TABLES = (
    "segment_embeddings",
    "note_topics",
    "segments",
    "notes",
    "flashcard_reviews",
    "flashcards",
    "stage_state",
)


def init_db() -> None:
    """Create or migrate the schema. Idempotent."""
    with get_conn() as conn:
        conn.commit()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Bring the database up to the current schema version.

    The order matters. The derived tables have to be dropped *before*
    the schema script runs, because the script creates indexes over
    columns that the old tables do not have, and SQLite would refuse.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    current = int(row["version"]) if row else 0

    if current > SCHEMA_VERSION:
        # Written by a newer build. Leave it alone rather than destroying
        # data we do not understand.
        log.warning(
            "database schema is newer than this build",
            extra={"db_version": current, "build_version": SCHEMA_VERSION},
        )
        return

    if current != SCHEMA_VERSION:
        # A database with no version row but with the derived tables in it
        # was written by a build from before versioning existed. Its
        # tables have the old shape, so they have to go too.
        stale = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='segments'"
        ).fetchone()[0]
        if current > 0 or stale:
            log.info(
                "rebuilding the derived tables for the new schema",
                extra={"from_version": current, "to_version": SCHEMA_VERSION},
            )
            # The FTS index is an external-content table over `segments`,
            # so it and its triggers go first.
            for trigger in ("segments_ai", "segments_ad", "segments_au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
            conn.execute("DROP TABLE IF EXISTS segments_fts")
            for table in DERIVED_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")

    conn.executescript(SCHEMA)
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
    conn.commit()


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


def transcript_key(
    source_hash: str,
    *,
    language_code: str,
    model: str = "",
) -> str:
    """The cache key for a transcription.

    It includes the language code and the model, not just the file.
    That is not a nicety: the language code decides the script the
    transcript comes back in, so a Telugu lecture transcribed as
    ``ta-IN`` is a *different* transcript from the same audio
    transcribed as ``te-IN``. Caching on the file alone would serve
    the wrong script.
    """
    raw = f"{source_hash}|{language_code}|{model}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cached_transcript(conn: sqlite3.Connection, content_hash: str) -> sqlite3.Row | None:
    """Find a cached transcript by its full cache key."""
    return conn.execute(
        "SELECT * FROM transcripts WHERE content_hash=?", (content_hash,)
    ).fetchone()


def store_transcript(
    conn: sqlite3.Connection,
    *,
    content_hash: str,
    source_hash: str,
    source_path: str,
    language_code: str,
    audio_seconds: float,
    segments: list[dict[str, Any]],
    full_text: str,
    model: str = "",
    gnani_job_id: str | None = None,
) -> str:
    transcript_id = new_id("tr")
    conn.execute(
        """INSERT INTO transcripts
             (id, content_hash, source_hash, source_path, language_code, model,
              audio_seconds, segments_json, full_text, gnani_job_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            transcript_id,
            content_hash,
            source_hash,
            source_path,
            language_code,
            model,
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
    """Replace a lecture's source segments. Idempotent, so a re-run is safe."""
    conn.execute("DELETE FROM segments WHERE lecture_id=?", (lecture_id,))
    for seq, seg in enumerate(segments):
        conn.execute(
            """INSERT INTO segments
                 (lecture_id, seq, start_seconds, end_seconds, text, speaker_id,
                  visual_dependent, topic_seq)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                lecture_id,
                seq,
                float(seg.get("start_seconds", 0.0)),
                float(seg.get("end_seconds", 0.0)),
                seg.get("text", ""),
                seg.get("speaker_id"),
                1 if seg.get("visual_dependent") else 0,
                seg.get("topic_seq"),
            ),
        )
    return len(segments)


def replace_topics(
    conn: sqlite3.Connection,
    lecture_id: str,
    topics: list[dict[str, Any]],
) -> int:
    """Replace a lecture's topic groupings and link the source segments.

    Each topic records the range of source segments it covers, so a note
    can always be traced back to the exact span of audio it came from.
    """
    conn.execute("DELETE FROM note_topics WHERE lecture_id=?", (lecture_id,))
    conn.execute("UPDATE segments SET topic_seq=NULL WHERE lecture_id=?", (lecture_id,))

    for topic_seq, topic in enumerate(topics):
        conn.execute(
            """INSERT INTO note_topics
                 (lecture_id, seq, start_seconds, end_seconds, text, visual_dependent)
               VALUES (?,?,?,?,?,?)""",
            (
                lecture_id,
                topic_seq,
                float(topic.get("start_seconds", 0.0)),
                float(topic.get("end_seconds", 0.0)),
                topic.get("text", ""),
                1 if topic.get("visual_dependent") else 0,
            ),
        )
        # Link the source segments that fall inside this topic.
        conn.execute(
            """UPDATE segments SET topic_seq=?
               WHERE lecture_id=? AND start_seconds >= ? AND start_seconds < ?""",
            (
                topic_seq,
                lecture_id,
                float(topic.get("start_seconds", 0.0)),
                float(topic.get("end_seconds", 0.0)),
            ),
        )
    return len(topics)


def save_topic_notes(
    conn: sqlite3.Connection,
    lecture_id: str,
    notes: list[dict[str, Any]],
) -> None:
    """Store the generated note for each topic."""
    for note in notes:
        conn.execute(
            """UPDATE note_topics SET heading=?, explanation=?
               WHERE lecture_id=? AND seq=?""",
            (
                note.get("heading", ""),
                note.get("explanation", ""),
                lecture_id,
                int(note.get("segment_index", -1)),
            ),
        )


def get_topics(conn: sqlite3.Connection, lecture_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM note_topics WHERE lecture_id=? ORDER BY seq", (lecture_id,)
    ).fetchall()


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
