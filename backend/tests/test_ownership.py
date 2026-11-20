"""Per-user lecture isolation: one student's data stays invisible to another.

Sign-in means every lecture, segment, note and flashcard has an owner.
These tests pin the boundary from the outside, through the HTTP API:

* a foreign lecture id answers **404**, never 403 — the response must be
  byte-identical to an unknown id so it never confirms that another
  student's lecture exists;
* retrieval is scoped in SQL, so another student's segments cannot leak
  into search, chat context, or the deck view;
* the pipeline is stamped with the uploader's user id at queue time;
* open mode (no Google credentials) still behaves exactly as before for
  the shared ``anonymous`` user.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import config as config_mod
from app import db as db_mod
from app.api import auth as auth_mod
from app.api import chat as chat_api
from app.api import lectures as lectures_api
from app.pipeline import flashcards as cards_mod
from app.pipeline.flashcards import Flashcard
from app.retrieval import embeddings

from .test_api import _claims as _decode_claims
from .test_api import _make_audio_bytes
from .test_auth import (
    CLIENT_ID,
    JWKS_URL,
    TOKEN_URL,
    _begin,
    _callback,
    _claims,
    _jwks,
    _mint,
    _private_key,
    fake_google,
)

# Distinctive single-token markers. Each lecture's segment contains only
# its own marker, so a hit for the wrong marker is an isolation leak.
ALICE_MARKER = "quantumvalve"
BOB_MARKER = "entropyflange"


# --------------------------------------------------------------------------
# Sign-in plumbing: two real sessions against the real callback
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_jwks() -> Any:
    """Every test starts with an empty signing-key cache."""
    auth_mod.reset_jwks_cache()
    yield
    auth_mod.reset_jwks_cache()


@pytest.fixture
def oauth(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Turn sign-in on, the way a deployment with Google credentials is."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    config_mod.reset_settings_cache()
    yield
    config_mod.reset_settings_cache()


@pytest.fixture(scope="module")
def signing_key() -> Any:
    return _private_key()


def _sign_in_as(client: TestClient, key: Any, *, sub: str, email: str) -> str:
    """Complete a full Google sign-in for ``sub``; return the local user id."""
    state, _ = _begin(client)
    with fake_google() as router:
        router.get(JWKS_URL).respond(200, json=_jwks(key))
        router.post(TOKEN_URL).respond(
            200, json={"id_token": _mint(key, _claims(sub=sub, email=email))}
        )
        response = _callback(client, state, code="auth-code")
    assert response.status_code == 302, response.text
    with db_mod.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE google_sub=?", (sub,)
        ).fetchone()
    assert row is not None, "the callback should have created the account"
    return str(row["id"])


class Student:
    """A signed-in browser: its user id and its own cookie jar."""

    def __init__(self, user_id: str, client: TestClient) -> None:
        self.id = user_id
        self.client = client


@pytest.fixture
def students(oauth: Any, signing_key: Any) -> tuple[Student, Student]:
    """Alice and Bob, each signed in on a separate client."""
    from app.main import app

    with TestClient(app) as alice_client, TestClient(app) as bob_client:
        alice = Student(
            _sign_in_as(
                alice_client, signing_key, sub="sub-alice", email="alice@example.com"
            ),
            alice_client,
        )
        bob = Student(
            _sign_in_as(
                bob_client, signing_key, sub="sub-bob", email="bob@example.com"
            ),
            bob_client,
        )
        yield alice, bob


def _seed_lecture(owner_id: str, *, marker: str) -> tuple[str, str]:
    """Insert a ready lecture with a segment, a note and a flashcard.

    Returns ``(lecture_id, flashcard_id)``. The segment insert fires the
    ``segments_ai`` trigger, so the FTS index is populated too.
    """
    lecture_id = f"lec_{marker}"
    card_id = f"card_{marker}"
    now = time.time()
    with db_mod.get_conn() as conn:
        conn.execute(
            "INSERT INTO lectures (id, title, original_filename, content_hash, "
            "language_code, status, owner_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'en-IN', 'ready', ?, ?, ?)",
            (
                lecture_id,
                f"The {marker} lecture",
                f"{marker}.mp3",
                f"hash-{marker}",
                owner_id,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO segments (lecture_id, seq, start_seconds, end_seconds, text) "
            "VALUES (?, 0, 0.0, 4.0, ?)",
            (lecture_id, f"the {marker} regulates the flow through the chamber"),
        )
        conn.execute(
            "INSERT INTO notes (id, lecture_id, document, summary, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                f"note_{marker}",
                lecture_id,
                f"Notes about the {marker}.",
                f"{marker} summary",
                now,
            ),
        )
        conn.execute(
            "INSERT INTO flashcards (id, lecture_id, question, answer, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                card_id,
                lecture_id,
                f"What is the {marker}?",
                f"The {marker} regulates the flow through the chamber.",
                now,
            ),
        )
    return lecture_id, card_id


# --------------------------------------------------------------------------
# Lecture visibility
# --------------------------------------------------------------------------


class TestLectureVisibility:
    def test_the_list_shows_only_my_own_lectures(self, students: Any) -> None:
        alice, bob = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)
        bob_lec, _ = _seed_lecture(bob.id, marker=BOB_MARKER)

        mine = alice.client.get("/api/lectures").json()["lectures"]
        assert [row["id"] for row in mine] == [alice_lec]

        theirs = bob.client.get("/api/lectures").json()["lectures"]
        assert [row["id"] for row in theirs] == [bob_lec]

    def test_a_foreign_lecture_is_404_and_identical_to_an_unknown_one(
        self, students: Any
    ) -> None:
        """403 would confirm the id exists; 404 keeps it unknowable."""
        alice, bob = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)

        foreign = bob.client.get(f"/api/lectures/{alice_lec}")
        unknown = bob.client.get("/api/lectures/lec_does_not_exist")

        assert foreign.status_code == 404
        assert unknown.status_code == 404
        assert foreign.json() == unknown.json()
        assert foreign.json()["detail"] == "No such lecture"

    def test_my_own_lecture_still_opens(self, students: Any) -> None:
        alice, _ = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)

        payload = alice.client.get(f"/api/lectures/{alice_lec}")
        assert payload.status_code == 200
        assert payload.json()["id"] == alice_lec
        assert payload.json()["status"] == "ready"

    def test_notes_are_404_for_a_foreign_lecture(self, students: Any) -> None:
        alice, bob = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)

        assert alice.client.get(f"/api/lectures/{alice_lec}/notes").status_code == 200

        response = bob.client.get(f"/api/lectures/{alice_lec}/notes")
        assert response.status_code == 404
        assert response.json()["detail"] == "No such lecture"


# --------------------------------------------------------------------------
# Chat
# --------------------------------------------------------------------------


class TestChatIsolation:
    def test_a_foreign_lecture_is_404_before_retrieval_runs(
        self, students: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alice, bob = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)

        calls: list[str] = []

        async def hybrid(*args: Any, **kwargs: Any) -> list:
            calls.append("hybrid")
            return []

        async def grounded(*args: Any, **kwargs: Any) -> Any:
            calls.append("grounded")
            raise AssertionError("the model must not run for a foreign lecture")

        monkeypatch.setattr(chat_api.fts, "hybrid_search", hybrid)
        monkeypatch.setattr(chat_api.rerank, "answer_grounded", grounded)

        response = bob.client.post(
            "/api/chat",
            json={"question": f"What is the {ALICE_MARKER}?", "lecture_id": alice_lec},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "No such lecture"
        assert calls == [], "nothing may be retrieved for a lecture you do not own"

    def test_my_own_lecture_is_searched_with_my_scope(
        self, students: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alice, _ = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)

        seen: dict[str, Any] = {}

        async def hybrid(conn: Any, query: str, **kwargs: Any) -> list:
            seen.update(kwargs)
            return []

        async def grounded(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("an empty result must abstain without the model")

        monkeypatch.setattr(chat_api.fts, "hybrid_search", hybrid)
        monkeypatch.setattr(chat_api.rerank, "answer_grounded", grounded)

        payload = alice.client.post(
            "/api/chat",
            json={"question": f"Explain the {ALICE_MARKER}", "lecture_id": alice_lec},
        ).json()

        # Ownership passed (200, not 404) and the scope carried through.
        assert payload["covered"] is False
        assert payload["used_llm"] is False
        assert seen["owner_id"] == alice.id
        assert seen["lecture_id"] == alice_lec


# --------------------------------------------------------------------------
# Search: real FTS index, embeddings stubbed to keep the lexical stage only
# --------------------------------------------------------------------------


class TestSearchIsolation:
    @pytest.fixture(autouse=True)
    def lexical_only(self, monkeypatch: pytest.MonkeyPatch) -> Any:
        """No network: the question embedding stage returns "unavailable"."""

        async def no_embedding(question: str) -> None:
            return None

        monkeypatch.setattr(embeddings, "embed_question", no_embedding)

    def test_search_only_returns_my_own_segments(self, students: Any) -> None:
        alice, bob = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)
        bob_lec, _ = _seed_lecture(bob.id, marker=BOB_MARKER)

        mine = alice.client.get(
            "/api/chat/search", params={"q": ALICE_MARKER}
        ).json()
        assert [r["lecture_id"] for r in mine["results"]] == [alice_lec]
        assert all(ALICE_MARKER in r["text"] for r in mine["results"])

        # Bob's marker exists in the shared index but not in Alice's corpus.
        leaked = alice.client.get(
            "/api/chat/search", params={"q": BOB_MARKER}
        ).json()
        assert leaked["results"] == []
        assert leaked["answerable"] is False

        theirs = bob.client.get(
            "/api/chat/search", params={"q": BOB_MARKER}
        ).json()
        assert [r["lecture_id"] for r in theirs["results"]] == [bob_lec]

    def test_naming_a_foreign_lecture_matches_nothing(self, students: Any) -> None:
        alice, bob = students
        bob_lec, _ = _seed_lecture(bob.id, marker=BOB_MARKER)

        # 200 with no results — the same answer an unknown id gets.
        response = alice.client.get(
            "/api/chat/search",
            params={"q": BOB_MARKER, "lecture_id": bob_lec},
        )
        assert response.status_code == 200
        assert response.json()["results"] == []


# --------------------------------------------------------------------------
# Flashcards
# --------------------------------------------------------------------------


class TestFlashcardIsolation:
    def test_the_deck_view_only_lists_my_cards(self, students: Any) -> None:
        alice, bob = students
        _, alice_card = _seed_lecture(alice.id, marker=ALICE_MARKER)
        _, bob_card = _seed_lecture(bob.id, marker=BOB_MARKER)

        mine = alice.client.get("/api/flashcards").json()
        assert [c["id"] for c in mine["cards"]] == [alice_card]

        theirs = bob.client.get("/api/flashcards").json()
        assert [c["id"] for c in theirs["cards"]] == [bob_card]

    def test_filtering_by_a_foreign_lecture_is_404(self, students: Any) -> None:
        alice, bob = students
        bob_lec, _ = _seed_lecture(bob.id, marker=BOB_MARKER)

        response = alice.client.get(
            "/api/flashcards", params={"lecture_id": bob_lec}
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "No such lecture"

    def test_reviewing_a_foreign_card_is_404(self, students: Any) -> None:
        alice, bob = students
        _, alice_card = _seed_lecture(alice.id, marker=ALICE_MARKER)

        foreign = bob.client.post(
            f"/api/flashcards/{alice_card}/review", json={"quality": 5}
        )
        assert foreign.status_code == 404
        assert foreign.json()["detail"] == "No such flashcard"

        # The owner's review still works.
        own = alice.client.post(
            f"/api/flashcards/{alice_card}/review", json={"quality": 5}
        )
        assert own.status_code == 200

    def test_generating_for_a_foreign_lecture_never_calls_the_model(
        self, students: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alice, bob = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)

        calls: list[str] = []

        async def generate(*args: Any, **kwargs: Any) -> list:
            calls.append("generate")
            return []

        monkeypatch.setattr(cards_mod, "generate_flashcards", generate)

        response = bob.client.post(f"/api/flashcards/lecture/{alice_lec}")

        assert response.status_code == 404
        assert response.json()["detail"] == "No such lecture"
        assert calls == []

    def test_generating_for_my_own_lecture_stores_the_cards(
        self, students: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alice, _ = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)

        async def generate(
            conn: Any, lecture_id: str, *, count: int = 20, **kwargs: Any
        ) -> list:
            return [
                Flashcard(
                    question=f"How does the {ALICE_MARKER} work?",
                    answer=f"The {ALICE_MARKER} regulates flow through the chamber.",
                )
            ]

        monkeypatch.setattr(cards_mod, "generate_flashcards", generate)

        payload = alice.client.post(f"/api/flashcards/lecture/{alice_lec}").json()
        assert payload["added"] == 1
        assert payload["total"] == 2  # the seeded card plus the new one


# --------------------------------------------------------------------------
# Upload: the pipeline runs under the uploader's id
# --------------------------------------------------------------------------


class TestUploadOwnership:
    def test_the_pipeline_is_stamped_with_my_user_id(
        self, students: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alice, _ = students
        started: list[tuple] = []
        monkeypatch.setattr(
            lectures_api, "_run_pipeline", lambda *args: started.append(args)
        )

        response = alice.client.post(
            "/api/lectures/upload",
            files={
                "file": ("lecture.mp3", _make_audio_bytes(seconds=6.0), "audio/mpeg")
            },
            data={"title": "Alice Lecture", "language_code": "en-IN"},
        )

        assert response.status_code == 200
        assert started, "the pipeline should have been queued"
        # (path, title, language_code, owner_id)
        assert started[0][3] == alice.id


# --------------------------------------------------------------------------
# LiveKit: the room opens only for the owner
# --------------------------------------------------------------------------


class TestLiveKitOwnership:
    def test_i_can_mint_a_token_for_my_own_lecture(self, students: Any) -> None:
        alice, _ = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)

        payload = alice.client.post(
            "/api/livekit/token",
            json={"lecture_id": alice_lec, "participant_name": "Student"},
        ).json()

        assert payload["room"].startswith(f"lecture-{alice_lec}-")
        assert payload["token"].count(".") == 2
        claims = _decode_claims(payload["token"])
        assert claims["sub"] == alice.id
        assert json.loads(claims["metadata"])["user_id"] == alice.id
        assert json.loads(claims["roomConfig"]["metadata"])["user_id"] == alice.id

    def test_a_foreign_lecture_is_404_before_anything_else(
        self, students: Any
    ) -> None:
        alice, bob = students
        alice_lec, _ = _seed_lecture(alice.id, marker=ALICE_MARKER)

        response = bob.client.post(
            "/api/livekit/token",
            json={"lecture_id": alice_lec, "participant_name": "Student"},
        )

        assert response.status_code == 404
        assert response.json()["detail"] == "No such lecture"


# --------------------------------------------------------------------------
# Open mode: no sign-in, everyone is `anonymous` — and still scoped
# --------------------------------------------------------------------------


class TestOpenModeOwnership:
    @pytest.fixture
    def open_client(self) -> Any:
        from app.main import app

        with TestClient(app) as client:
            yield client

    def test_only_the_anonymous_lectures_are_visible(self, open_client: Any) -> None:
        shared, _ = _seed_lecture("anonymous", marker=ALICE_MARKER)
        other, _ = _seed_lecture("some-google-user", marker=BOB_MARKER)

        visible = [
            row["id"]
            for row in open_client.get("/api/lectures").json()["lectures"]
        ]
        assert visible == [shared]

        assert open_client.get(f"/api/lectures/{shared}").status_code == 200
        assert open_client.get(f"/api/lectures/{other}").status_code == 404

    def test_open_mode_still_requires_no_session(self, open_client: Any) -> None:
        assert open_client.get("/api/lectures").status_code == 200
        assert open_client.get("/api/auth/me").json()["user"] is None
