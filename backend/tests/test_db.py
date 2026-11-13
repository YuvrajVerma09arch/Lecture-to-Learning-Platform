"""Schema-migration tests.

The derived tables -- segments, topics, notes, flashcards -- can be
dropped and rebuilt; the transcript cannot, because it cost real credits
to produce. A schema bump has to keep two invariants:

* the transcript survives;
* a lecture that loses its derived rows stops claiming to be ready, so
  the UI never shows a "ready" lecture with no notes behind it.
"""

from __future__ import annotations

from app import db as db_mod


def _seed_ready_lecture(conn) -> str:
    """A finished lecture: transcript, segments, and a notes document."""
    lecture_id = db_mod.new_id("lec")
    conn.execute(
        """INSERT INTO lectures
             (id, title, original_filename, content_hash, audio_seconds,
              language_code, status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            lecture_id,
            "Lecture",
            "lecture.mp3",
            "src-hash",
            12.0,
            "en-IN",
            db_mod.STATUS_READY,
            db_mod.now(),
            db_mod.now(),
        ),
    )
    db_mod.store_transcript(
        conn,
        content_hash=db_mod.transcript_key("src-hash", language_code="en-IN", model="m"),
        source_hash="src-hash",
        source_path="/tmp/lecture.mp3",
        language_code="en-IN",
        audio_seconds=12.0,
        segments=[{"start_seconds": 0.0, "end_seconds": 4.0, "text": "hello"}],
        full_text="hello",
        model="m",
        gnani_job_id="job-1",
    )
    db_mod.replace_segments(
        conn, lecture_id, [{"start_seconds": 0.0, "end_seconds": 4.0, "text": "hello"}]
    )
    conn.execute(
        "INSERT INTO notes (id, lecture_id, document, summary, created_at) "
        "VALUES (?,?,?,?,?)",
        (db_mod.new_id("nt"), lecture_id, "# Notes", "", db_mod.now()),
    )
    conn.commit()
    return lecture_id


def _claim_previous_schema() -> None:
    """Record that the database was written by the previous schema."""
    with db_mod.get_conn() as conn:
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version (version) VALUES (1)")


class TestSchemaBump:
    def test_the_transcript_survives_and_ready_lectures_are_parked(self):
        db_mod.init_db()
        with db_mod.get_conn() as conn:
            lecture_id = _seed_ready_lecture(conn)

        _claim_previous_schema()

        # The next connection runs the migration.
        with db_mod.get_conn() as conn:
            lecture = conn.execute(
                "SELECT * FROM lectures WHERE id=?", (lecture_id,)
            ).fetchone()
            assert lecture["status"] == db_mod.STATUS_FAILED
            assert lecture["failed_stage"] == db_mod.STAGE_SEGMENT
            assert "Upload the original recording again" in lecture["failure_reason"]

            # The expensive artefact is untouched...
            assert conn.execute("SELECT COUNT(*) c FROM transcripts").fetchone()["c"] == 1
            # ...and the rebuildable ones are gone.
            assert conn.execute("SELECT COUNT(*) c FROM segments").fetchone()["c"] == 0
            assert conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"] == 0

            version = conn.execute("SELECT version FROM schema_version").fetchone()["version"]
            assert version == db_mod.SCHEMA_VERSION

    def test_a_lecture_that_already_failed_keeps_its_reason(self):
        db_mod.init_db()
        with db_mod.get_conn() as conn:
            lecture_id = db_mod.new_id("lec")
            conn.execute(
                """INSERT INTO lectures
                     (id, title, original_filename, content_hash, audio_seconds,
                      language_code, status, failed_stage, failure_reason,
                      created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lecture_id,
                    "Lecture",
                    "lecture.mp3",
                    "src-hash",
                    12.0,
                    "en-IN",
                    db_mod.STATUS_FAILED,
                    db_mod.STAGE_NOTE,
                    "the model exploded",
                    db_mod.now(),
                    db_mod.now(),
                ),
            )
            conn.commit()

        _claim_previous_schema()

        with db_mod.get_conn() as conn:
            lecture = conn.execute(
                "SELECT * FROM lectures WHERE id=?", (lecture_id,)
            ).fetchone()
            assert lecture["status"] == db_mod.STATUS_FAILED
            assert lecture["failed_stage"] == db_mod.STAGE_NOTE
            assert lecture["failure_reason"] == "the model exploded"
