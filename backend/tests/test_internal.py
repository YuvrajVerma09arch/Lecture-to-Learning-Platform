"""The service-token endpoint the hosted agent uses for lecture context."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import config as config_mod
from app import db as db_mod


@pytest.fixture
def service(monkeypatch: Any) -> Any:
    monkeypatch.setenv("AGENT_API_TOKEN", "test-service-token")
    config_mod.reset_settings_cache()
    from app.main import app

    with TestClient(app) as client:
        yield client
    config_mod.reset_settings_cache()


def _seed(owner_id: str) -> str:
    now = time.time()
    lecture_id = "lec_internal"
    with db_mod.get_conn() as conn:
        conn.execute(
            "INSERT INTO lectures (id, title, original_filename, content_hash, "
            "language_code, status, owner_id, created_at, updated_at) "
            "VALUES (?, 'Internal Lecture', 'x.mp3', ?, 'en-IN', 'ready', ?, ?, ?)",
            (lecture_id, "a" * 64, owner_id, now, now),
        )
        conn.execute(
            "INSERT INTO notes (id, lecture_id, document, summary, created_at) "
            "VALUES ('note_internal', ?, ?, ?, ?)",
            (lecture_id, "# Internal Lecture\n\n## Topic One\nBody", "A short summary", now),
        )
    return lecture_id


class TestAgentContext:
    def test_the_token_is_required(self, service: TestClient):
        lecture_id = _seed("usr_alice")
        assert service.get(f"/api/internal/lectures/{lecture_id}/context").status_code == 401
        response = service.get(
            f"/api/internal/lectures/{lecture_id}/context",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert response.status_code == 401

    def test_it_returns_context_for_the_owner(self, service: TestClient):
        lecture_id = _seed("usr_alice")
        response = service.get(
            f"/api/internal/lectures/{lecture_id}/context",
            params={"owner_id": "usr_alice"},
            headers={"Authorization": "Bearer test-service-token"},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["title"] == "Internal Lecture"
        assert payload["summary"] == "A short summary"
        assert "## Topic One" in payload["document"]

    def test_a_foreign_owner_is_indistinguishable_from_unknown(self, service: TestClient):
        lecture_id = _seed("usr_alice")
        headers = {"Authorization": "Bearer test-service-token"}
        foreign = service.get(
            f"/api/internal/lectures/{lecture_id}/context",
            params={"owner_id": "usr_bob"},
            headers=headers,
        )
        unknown = service.get(
            "/api/internal/lectures/lec_unknown/context",
            params={"owner_id": "usr_bob"},
            headers=headers,
        )
        assert foreign.status_code == unknown.status_code == 404
        assert foreign.json() == unknown.json()

    def test_an_unconfigured_server_refuses_the_route(self, monkeypatch: Any):
        monkeypatch.setenv("AGENT_API_TOKEN", "")
        config_mod.reset_settings_cache()
        from app.main import app

        try:
            with TestClient(app) as client:
                response = client.get("/api/internal/lectures/lec_x/context")
            assert response.status_code == 503
        finally:
            config_mod.reset_settings_cache()
