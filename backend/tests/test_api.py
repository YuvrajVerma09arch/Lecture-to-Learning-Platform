"""API tests.

These exercise the HTTP surface of the backend. Nothing here calls an
external service: the retrieval, the model and the LiveKit key are all
either stubbed or already local.

The ``isolated_settings`` fixture in ``conftest.py`` is autouse and points
the app at a temporary database, so these tests never touch the real one.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod
from app.api import chat as chat_api
from app.api import lectures as lectures_api
from app.retrieval import fts, rerank


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def seeded_lecture():
    """A lecture with a transcript, a note and some source segments."""
    with db_mod.get_conn() as conn:
        conn.execute(
            """INSERT INTO lectures (id, title, original_filename, content_hash,
                                    audio_seconds, language_code, status, created_at, updated_at)
               VALUES ('lec_test','A Test Lecture','t.mp3','hash1',120.0,'te-IN','ready',0,0)"""
        )
        for seq, text in enumerate(
            [
                "సో మనిషి ఆ మోక్షము పొందాలి అని ప్రయత్నించే దిశలో రెండు మార్గాలు.",
                "వివేకానంద సాంగ్స్ అయిపోకుండా తేడాని తెలుసుకుంటూ.",
                "కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది.",
            ]
        ):
            conn.execute(
                """INSERT INTO segments (lecture_id, seq, start_seconds, end_seconds,
                                         text, speaker_id, visual_dependent, topic_seq)
                   VALUES ('lec_test',?,?,?,?,1,0,0)""",
                (seq, seq * 40.0, (seq + 1) * 40.0, text),
            )
        conn.execute(
            "INSERT INTO notes (id, lecture_id, document, summary, created_at) "
            "VALUES ('nt_test','lec_test','# Test\n\n## Section\n\nBody text.','A summary.',0)"
        )
        conn.execute(
            "INSERT INTO flashcards (id, lecture_id, question, answer, segment_ids, created_at) "
            "VALUES ('fc_test','lec_test','What is Moksha?','Liberation.','[]',0)"
        )
        conn.commit()
    return "lec_test"


class TestHealth:
    def test_health_is_ok_and_depends_on_nothing(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_ready_reports_each_dependency_separately(self, client):
        payload = client.get("/api/ready").json()
        assert payload["status"] == "ready"
        checks = payload["checks"]
        assert checks["database"]["ok"] is True
        # A provider being unavailable is reported as data, not as a failure
        # of the app: the app can still serve cached notes.
        for name in ("gnani", "google", "groq", "livekit"):
            assert "ok" in checks[name]

    def test_ready_still_reports_when_a_provider_is_missing(self, client, monkeypatch):
        from app import config as config_mod

        settings = config_mod.get_settings()
        monkeypatch.setattr(settings, "groq_api_key", "")
        payload = client.get("/api/ready").json()
        assert payload["status"] == "ready"
        assert payload["checks"]["groq"]["ok"] is False


class TestLectures:
    def test_upload_rejects_an_unsupported_type(self, client):
        response = client.post(
            "/api/lectures/upload",
            files={"file": ("notes.txt", b"hello", "text/plain")},
            data={"title": "Test"},
        )
        assert response.status_code == 400
        assert "Unsupported file type" in response.json()["detail"]

    def test_upload_rejects_an_empty_file(self, client):
        response = client.post(
            "/api/lectures/upload",
            files={"file": ("lecture.mp3", b"", "audio/mpeg")},
            data={"title": "Test"},
        )
        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_upload_accepts_a_file_and_starts_the_pipeline(self, client, monkeypatch):
        # The pipeline would create a Gnani job, so it is replaced.
        started: list[tuple] = []
        monkeypatch.setattr(
            lectures_api, "_run_pipeline", lambda *args: started.append(args)
        )
        response = client.post(
            "/api/lectures/upload",
            files={"file": ("lecture.mp3", b"\x00" * 2048, "audio/mpeg")},
            data={"title": "Test Lecture", "language_code": "te-IN"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        assert started, "the pipeline should have been scheduled"

    def test_list_returns_the_lectures(self, client, seeded_lecture):
        payload = client.get("/api/lectures").json()
        ids = [lec["id"] for lec in payload["lectures"]]
        assert seeded_lecture in ids

    def test_get_returns_the_lecture_with_its_stage_state(self, client, seeded_lecture):
        payload = client.get(f"/api/lectures/{seeded_lecture}").json()
        assert payload["title"] == "A Test Lecture"
        assert payload["status"] == "ready"
        assert payload["segment_count"] == 3
        assert "stages" in payload

    def test_get_returns_404_for_an_unknown_lecture(self, client):
        assert client.get("/api/lectures/lec_missing").status_code == 404

    def test_notes_returns_the_document_and_the_source_segments(self, client, seeded_lecture):
        payload = client.get(f"/api/lectures/{seeded_lecture}/notes").json()
        assert payload["document"].startswith("# Test")
        assert payload["summary"] == "A summary."
        assert len(payload["segments"]) == 3
        first = payload["segments"][0]
        assert first["seq"] == 0
        assert first["start_seconds"] == 0.0
        assert "మోక్షము" in first["text"]

    def test_notes_returns_404_for_an_unknown_lecture(self, client):
        assert client.get("/api/lectures/lec_missing/notes").status_code == 404


class TestChat:
    def test_it_abstains_without_calling_the_model(
        self, client, monkeypatch, seeded_lecture
    ):
        """Nothing relevant found means the model is never asked."""
        called: list[str] = []

        async def no_results(*args: Any, **kwargs: Any) -> list:
            return []

        async def must_not_run(*args: Any, **kwargs: Any) -> Any:
            called.append("called")
            raise AssertionError("the model should not have been called")

        monkeypatch.setattr(chat_api.fts, "hybrid_search", no_results)
        monkeypatch.setattr(chat_api.rerank, "answer_grounded", must_not_run)

        payload = client.post(
            "/api/chat", json={"question": "How do I bake a cake?", "lecture_id": "lec_test"}
        ).json()

        assert payload["covered"] is False
        assert payload["used_llm"] is False
        assert payload["retrieved"] == []
        assert called == []
        assert "isn't covered" in payload["answer"]

    def test_it_answers_from_the_retrieved_segments(
        self, client, monkeypatch, seeded_lecture
    ):
        segment = fts.RetrievedSegment(
            id=1,
            lecture_id="lec_test",
            seq=2,
            start_seconds=80.0,
            end_seconds=120.0,
            text="కర్మ యోగం మరియు జ్ఞాన యోగం మధ్య తేడా ఉంది.",
            score=0.9,
            semantic_score=0.72,
        )

        async def with_results(*args: Any, **kwargs: Any) -> list:
            return [segment]

        async def grounded(*args: Any, **kwargs: Any) -> rerank.GroundedAnswer:
            return rerank.GroundedAnswer(
                covered=True,
                answer="Karma Yoga and Jnana Yoga are different paths.",
                cited_segments=[2],
                confidence=0.8,
                retrieved=[segment],
                used_llm=True,
            )

        monkeypatch.setattr(chat_api.fts, "hybrid_search", with_results)
        monkeypatch.setattr(chat_api.rerank, "answer_grounded", grounded)

        payload = client.post(
            "/api/chat",
            json={"question": "jnana yoga aur karma yoga me kya farak hai?", "lecture_id": "lec_test"},
        ).json()

        assert payload["covered"] is True
        assert payload["used_llm"] is True
        assert payload["cited_segments"] == [2]
        assert "different paths" in payload["answer"]

    def test_it_returns_404_for_an_unknown_lecture(self, client):
        response = client.post(
            "/api/chat", json={"question": "anything", "lecture_id": "lec_missing"}
        )
        assert response.status_code == 404

    def test_search_reports_the_results_and_whether_they_are_answerable(
        self, client, monkeypatch, seeded_lecture
    ):
        async def with_results(*args: Any, **kwargs: Any) -> list:
            return [
                fts.RetrievedSegment(
                    id=1,
                    lecture_id="lec_test",
                    seq=0,
                    start_seconds=0.0,
                    end_seconds=40.0,
                    text="text",
                    score=0.8,
                    semantic_score=0.71,
                )
            ]

        monkeypatch.setattr(chat_api.fts, "hybrid_search", with_results)
        payload = client.get("/api/chat/search?q=moksha").json()
        assert payload["answerable"] is True
        assert payload["results"][0]["semantic_score"] == pytest.approx(0.71)


class TestLiveKitToken:
    def test_it_mints_a_token_for_the_lecture_room(self, client, seeded_lecture):
        payload = client.post(
            "/api/livekit/token",
            json={"lecture_id": seeded_lecture, "participant_name": "Student"},
        ).json()

        assert payload["room"] == "lecture-lec_test"
        assert payload["url"]
        token = payload["token"]
        # Three dot-separated parts: a JWT.
        assert token.count(".") == 2

        # Assert on the decoded claims, never print the token itself.
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body))
        video = claims["video"]
        assert video["roomJoin"] is True
        assert video["room"] == "lecture-lec_test"
        assert video["canPublish"] is True
        assert video["canSubscribe"] is True
        assert claims["exp"] > claims["nbf"]

    def test_it_rejects_an_empty_lecture_id(self, client):
        response = client.post(
            "/api/livekit/token", json={"lecture_id": "", "participant_name": "S"}
        )
        assert response.status_code == 422


class TestFlashcards:
    def test_it_lists_the_cards(self, client, seeded_lecture):
        payload = client.get(f"/api/flashcards?lecture_id={seeded_lecture}").json()
        assert payload["count"] == 1
        card = payload["cards"][0]
        assert card["question"] == "What is Moksha?"
        assert card["is_due"] is True
        assert card["repetitions"] == 0

    def test_a_review_schedules_the_next_one(self, client, seeded_lecture):
        payload = client.post(
            "/api/flashcards/fc_test/review", json={"quality": 5}
        ).json()
        assert payload["repetitions"] == 1
        assert payload["interval_days"] == 1.0
        assert payload["ease"] == pytest.approx(2.6)
        assert payload["due_at"] > 0

    def test_a_wrong_answer_resets_the_schedule(self, client, seeded_lecture):
        # A good answer first, so the card is in a later interval.
        client.post("/api/flashcards/fc_test/review", json={"quality": 5})
        payload = client.post(
            "/api/flashcards/fc_test/review", json={"quality": 1}
        ).json()
        assert payload["repetitions"] == 0
        assert payload["interval_days"] == 1.0

    def test_a_review_of_an_unknown_card_is_404(self, client):
        response = client.post(
            "/api/flashcards/fc_missing/review", json={"quality": 4}
        )
        assert response.status_code == 404

    def test_the_quality_is_range_checked(self, client, seeded_lecture):
        response = client.post(
            "/api/flashcards/fc_test/review", json={"quality": 9}
        )
        assert response.status_code == 422


class TestScheduling:
    """The scheduling function is pure, so it can be checked exactly."""

    def test_the_first_two_intervals_are_fixed(self):
        from app.api.flashcards import schedule

        reps, ease, interval = schedule(0, 2.5, 5, 0.0)
        assert reps == 1
        assert interval == 1.0

        reps, ease, interval = schedule(1, ease, 5, interval)
        assert reps == 2
        assert interval == 6.0

    def test_the_third_interval_grows_by_the_ease(self):
        from app.api.flashcards import schedule

        reps, ease, interval = schedule(2, 2.6, 5, 6.0)
        assert reps == 3
        # The ease is raised before the interval is computed, so the
        # interval is 6.0 * 2.7.
        assert ease == pytest.approx(2.7)
        assert interval == pytest.approx(16.2)

    def test_a_wrong_answer_resets(self):
        from app.api.flashcards import schedule

        reps, ease, interval = schedule(5, 2.5, 1, 30.0)
        assert reps == 0
        assert interval == 1.0
        assert ease < 2.5

    def test_the_ease_never_falls_below_the_floor(self):
        from app.api.flashcards import schedule

        reps, ease, interval = schedule(0, 1.3, 0, 0.0)
        assert ease == pytest.approx(1.3)
