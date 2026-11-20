"""Per-user quota and the env-based operator role."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import config as config_mod
from app.api import lectures as lectures_api
from app.pipeline import ingest as ingest_mod
from app.pipeline import orchestrator

from .test_account import _seed
from .test_api import _make_audio_bytes
from .test_ownership import (  # noqa: F401 - shared sign-in fixtures
    Student,
    _sign_in_as,
    clean_jwks,
    oauth,
    signing_key,
)


@pytest.fixture
def students_fixture(request: Any) -> Any:
    request.getfixturevalue("oauth")
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


@contextmanager
def _setting(monkeypatch: Any, key: str, value: str) -> Any:
    monkeypatch.setenv(key, value)
    config_mod.reset_settings_cache()
    yield
    config_mod.reset_settings_cache()


class TestLectureQuota:
    def test_a_user_cannot_exceed_the_cap(
        self, students_fixture: Any, monkeypatch: Any, caplog: Any
    ):
        alice, bob = students_fixture
        with _setting(monkeypatch, "MAX_LECTURES_PER_USER", "2"):
            _seed(alice.id, "quotaa", hashlib.sha256(b"quotaa").hexdigest())
            _seed(alice.id, "quotab", hashlib.sha256(b"quotab").hexdigest())
            monkeypatch.setattr(
                lectures_api, "_run_pipeline", lambda *args: None
            )
            response = alice.client.post(
                "/api/lectures/upload",
                files={"file": ("lecture.mp3", _make_audio_bytes(seconds=6.0), "audio/mpeg")},
                data={"title": "Over quota"},
            )
            assert response.status_code == 429
            assert "quota reached" in response.json()["detail"].lower()
            # Another student's lectures do not consume Alice's quota.
            assert bob.client.get("/api/lectures").status_code == 200

    def test_zero_disables_the_cap(self, students_fixture: Any, monkeypatch: Any):
        alice, _ = students_fixture
        with _setting(monkeypatch, "MAX_LECTURES_PER_USER", "0"):
            _seed(alice.id, "unlimited", hashlib.sha256(b"unlimited").hexdigest())
            monkeypatch.setattr(lectures_api, "_run_pipeline", lambda *args: None)
            response = alice.client.post(
                "/api/lectures/upload",
                files={"file": ("lecture.mp3", _make_audio_bytes(seconds=6.0), "audio/mpeg")},
                data={"title": "Allowed"},
            )
            assert response.status_code == 200

    async def test_the_pipeline_rechecks_the_cap(
        self, students_fixture: Any, monkeypatch: Any, tmp_path: Any
    ):
        """Concurrent uploads cannot both pass the request-time check."""
        alice, _ = students_fixture
        with _setting(monkeypatch, "MAX_LECTURES_PER_USER", "1"):
            _seed(alice.id, "pipelinequota", hashlib.sha256(b"pipelinequota").hexdigest())
            audio = tmp_path / "quota.mp3"
            audio.write_bytes(_make_audio_bytes(seconds=6.0))
            with pytest.raises(ingest_mod.IngestError, match="quota reached"):
                await orchestrator.run_notes_pipeline(
                    audio, title="Over quota", owner_id=alice.id
                )

    def test_an_admin_bypasses_the_cap(self, students_fixture: Any, monkeypatch: Any):
        alice, _ = students_fixture
        with _setting(monkeypatch, "MAX_LECTURES_PER_USER", "1"):
            with _setting(monkeypatch, "ADMIN_EMAILS", "Alice@Example.com"):
                _seed(alice.id, "adminquota", hashlib.sha256(b"adminquota").hexdigest())
                monkeypatch.setattr(lectures_api, "_run_pipeline", lambda *args: None)
                response = alice.client.post(
                    "/api/lectures/upload",
                    files={
                        "file": ("lecture.mp3", _make_audio_bytes(seconds=6.0), "audio/mpeg")
                    },
                    data={"title": "Admin upload"},
                )
                assert response.status_code == 200


class TestAdminOverview:
    def test_only_the_configured_email_can_read_it(
        self, students_fixture: Any, monkeypatch: Any
    ):
        alice, bob = students_fixture
        with _setting(monkeypatch, "ADMIN_EMAILS", "Alice@Example.com, other@example.com"):
            _seed(alice.id, "adminseed", hashlib.sha256(b"adminseed").hexdigest())
            response = alice.client.get("/api/admin/overview")
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["totals"]["lectures"] == 1
            assert payload["limits"]["max_lectures_per_user"] == 25
            row = next(u for u in payload["users"] if u["id"] == alice.id)
            assert row["lecture_count"] == 1
            assert bob.client.get("/api/admin/overview").status_code == 403
            assert row["email"] == "alice@example.com"

    def test_open_mode_users_are_never_admins(self, monkeypatch: Any):
        with _setting(monkeypatch, "ADMIN_EMAILS", "alice@example.com"):
            from app.main import app

            with TestClient(app) as client:
                response = client.get("/api/admin/overview")
            assert response.status_code == 403

    def test_a_session_is_required_in_configured_mode(
        self, students_fixture: Any, monkeypatch: Any
    ):
        from app.main import app

        with TestClient(app) as client:
            response = client.get("/api/admin/overview")
        assert response.status_code == 401
