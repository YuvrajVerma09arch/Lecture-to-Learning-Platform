"""Account export and erasure tests.

These use real sign-in sessions and a per-test SQLite file. The important
assertions are about *scope*: Alice's export and deletion never touch Bob's
rows, the shared transcript cache, or files outside the data directory.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod
from app.config import get_settings

from .test_ownership import (  # noqa: F401 - shared sign-in fixtures
    Student,
    _sign_in_as,
    clean_jwks,
    oauth,
    signing_key,
)


@pytest.fixture
def students_fixture(request: Any) -> Any:
    """Two signed-in students with separate cookie jars."""
    request.getfixturevalue("oauth")  # sign-in must be configured
    key = request.getfixturevalue("signing_key")
    from app.main import app

    with TestClient(app) as alice_client, TestClient(app) as bob_client:
        alice = Student(
            _sign_in_as(
                alice_client, key, sub="sub-alice", email="alice@example.com"
            ),
            alice_client,
        )
        bob = Student(
            _sign_in_as(bob_client, key, sub="sub-bob", email="bob@example.com"),
            bob_client,
        )
        yield alice, bob


def _seed(owner_id: str, marker: str, digest: str) -> str:
    """A ready lecture with a segment, note, card, markdown and cache dir."""
    settings = get_settings()
    lecture_id = "lec_" + hashlib.sha256(marker.encode()).hexdigest()[:16]
    now = time.time()
    with db_mod.get_conn() as conn:
        conn.execute(
            "INSERT INTO lectures (id, title, original_filename, content_hash, "
            "language_code, status, owner_id, created_at, updated_at) "
            "VALUES (?, ?, 'lecture.mp3', ?, 'en-IN', 'ready', ?, ?, ?)",
            (lecture_id, marker, digest, owner_id, now, now),
        )
        conn.execute(
            "INSERT INTO segments (lecture_id, seq, start_seconds, end_seconds, text) "
            "VALUES (?, 0, 0.0, 4.0, ?)",
            (lecture_id, marker),
        )
        conn.execute(
            "INSERT INTO notes (id, lecture_id, document, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"note_{marker}", lecture_id, f"# {marker}", marker, now),
        )
        conn.execute(
            "INSERT INTO flashcards (id, lecture_id, question, answer, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (f"card_{marker}", lecture_id, marker, marker, now),
        )
        conn.execute(
            "INSERT INTO transcripts (id, content_hash, source_hash, source_path, "
            "language_code, model, audio_seconds, segments_json, full_text, created_at) "
            "VALUES (?, ?, ?, 'shared.mp3', 'en-IN', 'test', 4.0, '[]', ?, ?)",
            (f"tr_{marker}", digest, digest, marker, now),
        )
        conn.execute(
            "INSERT INTO credit_usage (day, kind, estimated, reference, created_at) "
            "VALUES ('2026-11-20', 'upload', 1.0, ?, ?)",
            (marker, now),
        )

    notes_file = settings.data_dir / "notes" / f"{lecture_id}.md"
    notes_file.parent.mkdir(parents=True, exist_ok=True)
    notes_file.write_text(f"# {marker}", encoding="utf-8")
    cache_dir = settings.cache_dir / "ingest" / digest[:16]
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "source.mp3").write_bytes(b"audio")
    return lecture_id


def _fts_rows(marker: str) -> int:
    with db_mod.get_conn() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM segments_fts WHERE segments_fts MATCH ?", (marker,)
            ).fetchone()[0]
        )


class TestAccountExport:
    def test_it_exports_only_my_lectures_and_notes(self, students_fixture: Any):
        alice, bob = students_fixture
        alice_lec = _seed(alice.id, "exportmarker", "a" * 64)
        _seed(bob.id, "foreignmarker", "b" * 64)

        response = alice.client.get("/api/account/export")
        assert response.status_code == 200
        assert "attachment" in response.headers["content-disposition"]
        payload = response.json()

        assert payload["profile"]["email"] == "alice@example.com"
        ids = [lecture["id"] for lecture in payload["lectures"]]
        assert ids == [alice_lec]
        lecture = payload["lectures"][0]
        assert lecture["segments"][0]["text"] == "exportmarker"
        assert lecture["notes"][0]["document"] == "# exportmarker"
        assert lecture["notes_markdown_file"] == "# exportmarker"
        assert lecture["flashcards"][0]["question"] == "exportmarker"
        assert "foreignmarker" not in response.text

    def test_open_mode_has_no_account_to_export(self):
        from app.main import app

        with TestClient(app) as client:
            response = client.get("/api/account/export")
        assert response.status_code == 400
        assert "sign-in is configured" in response.json()["detail"]


class TestAccountDeletion:
    def test_it_erases_my_lectures_and_files_but_spares_other_students(self, students_fixture: Any):
        alice, bob = students_fixture
        alice_lec = _seed(alice.id, "deletemarker", "c" * 64)
        bob_lec = _seed(bob.id, "keepmarker", "d" * 64)
        settings = get_settings()
        alice_note = settings.data_dir / "notes" / f"{alice_lec}.md"
        bob_note = settings.data_dir / "notes" / f"{bob_lec}.md"
        alice_cache = settings.cache_dir / "ingest" / ("c" * 16)
        bob_cache = settings.cache_dir / "ingest" / ("d" * 16)

        response = alice.client.request("DELETE", "/api/account")
        assert response.status_code == 200, response.text
        assert response.json()["lectures_removed"] == 1

        with db_mod.get_conn() as conn:
            ids = {
                row["id"]
                for row in conn.execute("SELECT id FROM lectures").fetchall()
            }
            assert ids == {bob_lec}
            assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM transcripts WHERE content_hash=?", ("c" * 64,)
            ).fetchone()[0] == 1  # shared transcript cache survives
            assert conn.execute(
                "SELECT COUNT(*) FROM credit_usage WHERE reference='deletemarker'"
            ).fetchone()[0] == 1  # global ledger survives

        assert _fts_rows("deletemarker") == 0
        assert _fts_rows("keepmarker") == 1
        assert not alice_note.exists()
        assert bob_note.exists()
        assert not alice_cache.exists()
        assert bob_cache.exists()

    def test_it_revokes_another_signed_in_session(
        self, students_fixture: Any, request: Any
    ):
        alice, _ = students_fixture
        # The JWKS cache holds the key from the fixture's sign-ins; reuse it.
        key = request.getfixturevalue("signing_key")
        from app.main import app

        with TestClient(app) as second_client:
            _sign_in_as(
                second_client,
                key,
                sub="sub-alice",
                email="alice@example.com",
            )
            assert second_client.get("/api/lectures").status_code == 200
            assert alice.client.request("DELETE", "/api/account").status_code == 200
            assert second_client.get("/api/lectures").status_code == 401

    def test_open_mode_cannot_delete_the_shared_corpus(self):
        from app.main import app

        with TestClient(app) as client:
            response = client.request("DELETE", "/api/account")
        assert response.status_code == 400

    def test_an_unsafe_lecture_id_cannot_delete_files_outside_the_data_dir(
        self, students_fixture: Any, tmp_path: Any
    ):
        alice, _ = students_fixture
        outside = tmp_path / "outside.md"
        outside.write_text("must survive", encoding="utf-8")
        lecture_id = "lec_../../outside"
        now = time.time()
        with db_mod.get_conn() as conn:
            conn.execute(
                "INSERT INTO lectures (id, title, original_filename, content_hash, "
                "language_code, status, owner_id, created_at, updated_at) "
                "VALUES (?, 'unsafe', 'x.mp3', ?, 'en-IN', 'ready', ?, ?, ?)",
                (lecture_id, "e" * 64, alice.id, now, now),
            )

        response = alice.client.request("DELETE", "/api/account")
        assert response.status_code == 200
        assert outside.read_text(encoding="utf-8") == "must survive"
