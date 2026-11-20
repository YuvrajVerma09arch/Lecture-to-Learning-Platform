"""Authentication, sessions and forged-token rejection.

The app runs in two modes and both are under test here:

* **Open** -- no Google credentials, which is what a fresh clone does.
  Nothing may regress: the API answers exactly as it did before sign-in
  existed, and the sign-in endpoints say they are unavailable.
* **Configured** -- sign-in is required. The OIDC flow runs end to end
  against a mocked Google, and every way of forging an ``id_token``
  ends at ``/?error=...`` with no session opened.

Nothing here talks to Google for real. ``respx`` intercepts the token
exchange and the JWKS fetch; every other request -- the ``TestClient``'s
own traffic -- passes straight through to the app.
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from joserfc import jwt as jose_jwt
from joserfc.jwk import RSAKey

from app import config as config_mod
from app import db as db_mod
from app.api import auth as auth_mod

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
TOKEN_URL = "https://oauth2.googleapis.com/token"
JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
AUTH_HOST = "accounts.google.com"
KID = "test-key"
# Every redirect in these tests lands on the app's own origin, because
# ``FRONTEND_URL`` is blank in the test environment.
APP = "http://testserver"


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


def _private_key() -> RSAKey:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return RSAKey.import_key(pem)


@pytest.fixture(scope="module")
def signing_key() -> RSAKey:
    """The key Google is supposed to have signed with."""
    return _private_key()


@pytest.fixture(scope="module")
def other_key() -> RSAKey:
    """A key Google never used, for signature-forgery cases."""
    return _private_key()


@pytest.fixture(autouse=True)
def clean_jwks():
    """Every test starts with an empty signing-key cache."""
    auth_mod.reset_jwks_cache()
    yield
    auth_mod.reset_jwks_cache()


@pytest.fixture
def oauth(monkeypatch: pytest.MonkeyPatch):
    """Turn sign-in on, the way a deployment with Google credentials is."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    config_mod.reset_settings_cache()
    yield
    config_mod.reset_settings_cache()


@pytest.fixture
def open_client():
    """The app with sign-in off (the fresh-clone default)."""
    from app.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def client(oauth):
    """The app with sign-in required."""
    from app.main import app

    with TestClient(app) as client:
        yield client


def _claims(**over: Any) -> dict[str, Any]:
    """A well-formed Google ``id_token`` payload. ``None`` deletes a claim."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "sub": "google-sub-1",
        "email": "student@example.com",
        "email_verified": True,
        "name": "Test Student",
        "iat": now - 60,
        "exp": now + 600,
    }
    for key, value in over.items():
        if value is None:
            claims.pop(key, None)
        else:
            claims[key] = value
    return claims


def _mint(key: RSAKey, claims: dict[str, Any], kid: str = KID) -> str:
    return jose_jwt.encode({"alg": "RS256", "kid": kid}, claims, key)


def _jwks(key: RSAKey, kid: str = KID) -> dict[str, Any]:
    """A JWKS document holding exactly one usable RSA key."""
    return {"keys": [dict(key.as_dict(), kid=kid)]}


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _alg_none(claims: dict[str, Any], kid: str = KID) -> str:
    """A token whose header claims ``alg: none`` -- the classic bypass."""
    header = _b64url(json.dumps({"alg": "none", "kid": kid}).encode())
    payload = _b64url(json.dumps(claims).encode())
    return f"{header}.{payload}."


@contextmanager
def fake_google() -> Iterator[Any]:
    """Mock the two Google endpoints, passing all other traffic through."""
    with respx.mock(assert_all_called=False) as rspx_router:
        yield rspx_router


def _begin(client: TestClient, next: str | None = "/lectures") -> tuple[str, str]:
    """Start the flow; return the state Google was handed and its URL."""
    params: dict[str, str] = {}
    if next is not None:
        params["next"] = next
    response = client.get("/api/auth/google", params=params, follow_redirects=False)
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"https://{AUTH_HOST}/o/oauth2/v2/auth?")
    query = parse_qs(urlparse(location).query)
    assert query["client_id"] == [CLIENT_ID]
    assert query["redirect_uri"] == [f"{APP}/api/auth/google/callback"]
    assert query["response_type"] == ["code"]
    return query["state"][0], location


def _callback(client: TestClient, state: str | None, **query: str):
    params = dict(query)
    if state is not None:
        params["state"] = state
    return client.get("/api/auth/google/callback", params=params, follow_redirects=False)


def _sign_in(client: TestClient, key: RSAKey) -> str:
    """Run a complete, successful sign-in; return where the browser lands."""
    state, _ = _begin(client)
    with fake_google() as rspx_router:
        rspx_router.get(JWKS_URL).respond(200, json=_jwks(key))
        rspx_router.post(TOKEN_URL).respond(
            200, json={"id_token": _mint(key, _claims())}
        )
        response = _callback(client, state, code="auth-code")
    assert response.status_code == 302, response.text
    return response.headers["location"]


def _location(response: Any) -> str:
    assert response.status_code in (302, 307), response.text
    return response.headers["location"]


# --------------------------------------------------------------------------
# Open mode: a fresh clone with no Google credentials
# --------------------------------------------------------------------------


class TestOpenMode:
    """Without credentials the app must behave exactly as it always has."""

    def test_me_returns_no_user_and_says_sign_in_is_off(self, open_client):
        assert open_client.get("/api/auth/me").json() == {
            "user": None,
            "google_oauth": False,
            "open_mode": True,
            "auth_required": False,
        }

    def test_config_reports_sign_in_is_off(self, open_client):
        assert open_client.get("/api/auth/config").json() == {
            "google_oauth": False,
            "open_mode": True,
            "auth_required": False,
            "authenticated": False,
        }

    def test_lectures_are_reachable(self, open_client):
        assert open_client.get("/api/lectures").status_code == 200

    def test_health_is_reachable(self, open_client):
        assert open_client.get("/api/health").json()["status"] == "ok"

    def test_logout_still_works(self, open_client):
        response = open_client.post("/api/auth/logout")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    def test_signing_in_says_it_is_unavailable(self, open_client):
        response = open_client.get("/api/auth/google", follow_redirects=False)
        assert response.status_code == 503


# --------------------------------------------------------------------------
# Configured mode: sign-in is required
# --------------------------------------------------------------------------


class TestSigninRequired:
    """With credentials set, a browser with no session is turned away."""

    def test_me_is_null_but_admits_sign_in_is_on(self, client):
        assert client.get("/api/auth/me").json() == {
            "user": None,
            "google_oauth": True,
            "open_mode": False,
            "auth_required": True,
        }

    def test_config_reports_sign_in_is_on_and_unauthenticated(self, client):
        assert client.get("/api/auth/config").json() == {
            "google_oauth": True,
            "open_mode": False,
            "auth_required": True,
            "authenticated": False,
        }

    @pytest.mark.parametrize("path", ["/api/lectures", "/api/flashcards"])
    def test_protected_routes_require_a_session(self, client, path):
        response = client.get(path)
        assert response.status_code == 401, response.text
        assert response.json()["detail"] == "Sign in required"

    def test_health_stays_open(self, client):
        assert client.get("/api/health").status_code == 200

    def test_the_auth_endpoints_stay_open(self, client):
        assert client.get("/api/auth/me").status_code == 200
        assert client.get("/api/auth/config").status_code == 200


class TestFailClosed:
    """No credentials and no opt-in: the API must not run open by accident."""

    @pytest.fixture
    def closed_client(self, monkeypatch):
        monkeypatch.delenv("OPEN_MODE", raising=False)
        monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
        config_mod.reset_settings_cache()
        from app.main import create_app

        with TestClient(create_app()) as client:
            yield client
        config_mod.reset_settings_cache()

    def test_data_routes_answer_401(self, closed_client):
        response = closed_client.get("/api/lectures")
        assert response.status_code == 401
        assert response.json()["detail"] == "Sign in required"

    def test_upload_answers_401_before_reading_the_body(self, closed_client):
        response = closed_client.post("/api/lectures/upload")
        assert response.status_code == 401

    def test_health_and_probes_stay_open(self, closed_client):
        assert closed_client.get("/api/health").status_code == 200
        me = closed_client.get("/api/auth/me").json()
        assert me["auth_required"] is True
        assert me["open_mode"] is False
        assert closed_client.get("/api/auth/config").json()["auth_required"] is True
        assert closed_client.get("/api/auth/google", follow_redirects=False).status_code == 503


# --------------------------------------------------------------------------
# Starting the flow
# --------------------------------------------------------------------------


class TestGoogleStart:
    def test_it_sends_the_browser_to_google_with_a_fresh_state(self, client):
        state, location = _begin(client)
        assert state
        assert "prompt=select_account" in location
        assert "scope=openid+email+profile" in location

    def test_two_starts_never_reuse_a_state(self, client):
        first, _ = _begin(client)
        second, _ = _begin(client)
        assert first != second

    @pytest.mark.parametrize(
        "hostile",
        ["https://evil.com/x", "//evil.com", "javascript:alert(1)", "/\\evil.com"],
    )
    def test_a_cross_site_next_is_neutralised(self, client, signing_key, hostile):
        """The stored return path must never be able to leave the site."""
        state, _ = _begin(client, next=hostile)
        with fake_google() as rspx_router:
            rspx_router.get(JWKS_URL).respond(200, json=_jwks(signing_key))
            rspx_router.post(TOKEN_URL).respond(
                200, json={"id_token": _mint(signing_key, _claims())}
            )
            response = _callback(client, state, code="auth-code")
        assert _location(response) == f"{APP}/"


# --------------------------------------------------------------------------
# Completing the flow
# --------------------------------------------------------------------------


class TestCallback:
    def test_it_signs_the_student_in_and_returns_to_next(
        self, client, signing_key
    ):
        state, _ = _begin(client, next="/lectures/lec_1")
        with fake_google() as rspx_router:
            keys_route = rspx_router.get(JWKS_URL).respond(
                200, json=_jwks(signing_key)
            )
            rspx_router.post(TOKEN_URL).respond(
                200, json={"id_token": _mint(signing_key, _claims())}
            )
            response = _callback(client, state, code="auth-code")

        assert _location(response) == f"{APP}/lectures/lec_1"
        assert keys_route.call_count == 1

        me = client.get("/api/auth/me").json()
        assert me["google_oauth"] is True
        assert me["user"]["email"] == "student@example.com"
        assert me["user"]["name"] == "Test Student"
        # The guard is satisfied now: the same request that returned 401
        # a moment ago answers200.
        assert client.get("/api/lectures").status_code == 200

    def test_it_lands_on_the_home_page_by_default(self, client, signing_key):
        state, _ = _begin(client, next=None)
        with fake_google() as rspx_router:
            rspx_router.get(JWKS_URL).respond(200, json=_jwks(signing_key))
            rspx_router.post(TOKEN_URL).respond(
                200, json={"id_token": _mint(signing_key, _claims())}
            )
            response = _callback(client, state, code="auth-code")
        assert _location(response) == f"{APP}/"

    def test_a_state_we_never_issued_is_rejected(self, client):
        state, _ = _begin(client)
        response = _callback(client, "not-the-state", code="auth-code")
        assert _location(response) == f"{APP}/?error=state_mismatch"
        assert client.get("/api/auth/me").json()["user"] is None

    def test_a_callback_without_a_start_is_rejected(self, client):
        response = _callback(client, "forged-state", code="auth-code")
        assert _location(response) == f"{APP}/?error=state_mismatch"

    def test_a_replayed_callback_is_rejected(self, client, signing_key):
        _sign_in(client, signing_key)
        state, _ = _begin(client)
        with fake_google() as rspx_router:
            token_route = rspx_router.post(TOKEN_URL).respond(
                200, json={"id_token": _mint(signing_key, _claims())}
            )
            first = _callback(client, state, code="auth-code")
        assert _location(first) == f"{APP}/lectures"
        # The state was consumed by the first callback, so replaying the
        # very same URL never reaches the token endpoint again.
        replayed = _callback(client, state, code="auth-code")
        assert _location(replayed) == f"{APP}/?error=state_mismatch"
        assert token_route.call_count == 1
        # The original session is untouched by the failed replay.
        assert client.get("/api/auth/me").json()["user"] is not None

    def test_the_provider_can_decline(self, client):
        state, _ = _begin(client)
        response = _callback(client, state, error="access_denied")
        assert _location(response) == f"{APP}/?error=access_denied"
        assert client.get("/api/auth/me").json()["user"] is None

    def test_a_failed_token_exchange_is_a_failed_sign_in(self, client):
        state, _ = _begin(client)
        with fake_google() as rspx_router:
            rspx_router.post(TOKEN_URL).respond(
                400, json={"error": "invalid_grant"}
            )
            response = _callback(client, state, code="used-code")
        assert _location(response) == f"{APP}/?error=signin_failed"
        assert client.get("/api/auth/me").json()["user"] is None
        assert client.get("/api/lectures").status_code == 401

    def test_two_sign_ins_create_one_account(self, client, signing_key):
        _sign_in(client, signing_key)
        _sign_in(client, signing_key)
        with db_mod.get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE google_sub = ?",
                ("google-sub-1",),
            ).fetchone()
        assert row["n"] == 1


# --------------------------------------------------------------------------
# Forged and malformed id_tokens
# --------------------------------------------------------------------------


class TestForgedIdTokens:
    """Every rejection here must be a redirect, never a 500 and never a
    session."""

    def _rejects(self, client, id_token: str, key: RSAKey) -> str:
        """Run a callback carrying ``id_token``; return the landing URL."""
        state, _ = _begin(client)
        with fake_google() as rspx_router:
            rspx_router.get(JWKS_URL).respond(200, json=_jwks(key))
            rspx_router.post(TOKEN_URL).respond(
                200, json={"id_token": id_token}
            )
            response = _callback(client, state, code="auth-code")
        assert response.status_code in (302, 307), response.text
        return response.headers["location"]

    def test_a_token_signed_by_the_wrong_key_is_rejected(
        self, client, signing_key, other_key
    ):
        landing = self._rejects(
            client, _mint(other_key, _claims(), kid=KID), signing_key
        )
        assert landing == f"{APP}/?error=signin_failed"
        assert client.get("/api/auth/me").json()["user"] is None

    def test_alg_none_is_rejected(self, client, signing_key):
        landing = self._rejects(client, _alg_none(_claims()), signing_key)
        assert landing == f"{APP}/?error=signin_failed"
        assert client.get("/api/auth/me").json()["user"] is None

    def test_the_wrong_audience_is_rejected(self, client, signing_key):
        landing = self._rejects(
            client,
            _mint(signing_key, _claims(aud="someone-elses-app.apps.googleusercontent.com")),
            signing_key,
        )
        assert landing == f"{APP}/?error=signin_failed"
        assert client.get("/api/auth/me").json()["user"] is None

    def test_the_wrong_issuer_is_rejected(self, client, signing_key):
        landing = self._rejects(
            client, _mint(signing_key, _claims(iss="https://evil.example")), signing_key
        )
        assert landing == f"{APP}/?error=signin_failed"

    def test_an_expired_token_is_rejected(self, client, signing_key):
        now = int(time.time())
        landing = self._rejects(
            client,
            _mint(signing_key, _claims(exp=now - 30, iat=now - 600)),
            signing_key,
        )
        assert landing == f"{APP}/?error=signin_failed"
        assert client.get("/api/auth/me").json()["user"] is None

    def test_a_token_without_an_expiry_is_rejected(self, client, signing_key):
        landing = self._rejects(client, _mint(signing_key, _claims(exp=None)), signing_key)
        assert landing == f"{APP}/?error=signin_failed"

    def test_a_token_without_a_subject_is_rejected(self, client, signing_key):
        landing = self._rejects(client, _mint(signing_key, _claims(sub=None)), signing_key)
        assert landing == f"{APP}/?error=signin_failed"

    def test_an_unverified_email_is_rejected(self, client, signing_key):
        """Google has not confirmed the address, so it is not an identity."""
        landing = self._rejects(
            client, _mint(signing_key, _claims(email_verified=False)), signing_key
        )
        assert landing == f"{APP}/?error=signin_failed"
        assert client.get("/api/auth/me").json()["user"] is None

    def test_garbage_is_rejected(self, client, signing_key):
        landing = self._rejects(client, "not-a-jwt-at-all", signing_key)
        assert landing == f"{APP}/?error=signin_failed"

    def test_an_unknown_kid_fetches_the_keys_exactly_once(
        self, client, signing_key
    ):
        """A stream of forged tokens with random kids must not become a
        stream of outbound requests: the cooldown holds after one try."""
        for index, kid in enumerate(("rotated-1", "rotated-2")):
            state, _ = _begin(client)
            with fake_google() as rspx_router:
                keys_route = rspx_router.get(JWKS_URL).respond(
                    200, json=_jwks(signing_key)
                )
                rspx_router.post(TOKEN_URL).respond(
                    200, json={"id_token": _mint(signing_key, _claims(), kid=kid)}
                )
                response = _callback(client, state, code="auth-code")
            assert _location(response) == f"{APP}/?error=signin_failed"
            # The first unknown kid spends the one allowed fetch; the
            # second lands inside the cooldown window and must fetch
            # nothing at all.
            assert keys_route.call_count == (1 if index == 0 else 0)
        assert client.get("/api/auth/me").json()["user"] is None


# --------------------------------------------------------------------------
# Logout
# --------------------------------------------------------------------------


class TestLogout:
    def test_it_revokes_the_session_on_the_server(self, client, signing_key):
        assert _sign_in(client, signing_key) == f"{APP}/lectures"

        with db_mod.get_conn() as conn:
            open_sessions = conn.execute(
                "SELECT COUNT(*) AS n FROM user_sessions"
            ).fetchone()["n"]
        assert open_sessions == 1

        response = client.post("/api/auth/logout")
        assert response.status_code == 200
        assert response.json() == {"ok": True}

        # The row is gone, not merely the cookie: copying the old cookie
        # back in must not resurrect the session.
        with db_mod.get_conn() as conn:
            assert (
                conn.execute("SELECT COUNT(*) AS n FROM user_sessions").fetchone()["n"]
                == 0
            )
        assert client.get("/api/auth/me").json()["user"] is None
        assert client.get("/api/lectures").status_code == 401


# --------------------------------------------------------------------------
# Redirect hygiene
# --------------------------------------------------------------------------


def test_safe_next_only_allows_local_paths():
    assert auth_mod._safe_next(None) == "/"
    assert auth_mod._safe_next("") == "/"
    assert auth_mod._safe_next("/lectures/lec_1") == "/lectures/lec_1"
    assert auth_mod._safe_next("/lectures?tab=cards") == "/lectures?tab=cards"
    assert auth_mod._safe_next("https://evil.com") == "/"
    assert auth_mod._safe_next("//evil.com") == "/"
    assert auth_mod._safe_next("/\\evil.com") == "/"
    assert auth_mod._safe_next("/ok\r\nSet-Cookie: x=1") == "/"
    assert auth_mod._safe_next("javascript:alert(1)") == "/"


# --------------------------------------------------------------------------
# The JWKS client itself
# --------------------------------------------------------------------------


class TestJwks:
    async def test_it_keeps_only_usable_rsa_keys(self, signing_key):
        good = dict(signing_key.as_dict(), kid="good")
        async with httpx.AsyncClient() as http:
            with fake_google() as rspx_router:
                rspx_router.get(JWKS_URL).respond(
                    200,
                    json={
                        "keys": [
                            good,
                            # Not RSA, so not a Google signing key for us.
                            {
                                "kty": "EC",
                                "kid": "ec",
                                "crv": "P-256",
                                "x": "AQAB",
                                "y": "AQAB",
                            },
                            # RSA but unaddressable without a kid.
                            {"kty": "RSA", "n": "AQAB", "e": "AQAB"},
                        ]
                    },
                )
                await auth_mod._fetch_jwks(http)

        assert set(auth_mod._jwks["keys"]) == {"good"}
        assert auth_mod._jwks["last_attempt"] > 0

    async def test_refresh_never_raises_when_the_endpoint_is_down(self):
        async with httpx.AsyncClient() as http:
            with fake_google() as rspx_router:
                keys_route = rspx_router.get(JWKS_URL).respond(503, json={})
                assert await auth_mod._refresh_keys(http) is False
                # The cooldown applies after a failure too, so a dead
                # Google endpoint is not hammered by retrying callers.
                assert await auth_mod._refresh_keys(http) is False
                assert keys_route.call_count == 1

    async def test_refresh_keeps_stale_keys_when_the_refresh_fails(
        self, signing_key
    ):
        auth_mod._jwks["keys"] = {KID: signing_key}
        auth_mod._jwks["last_attempt"] = 0.0
        async with httpx.AsyncClient() as http:
            with fake_google() as rspx_router:
                rspx_router.get(JWKS_URL).respond(503, json={})
                # Old keys still verify: Google keeps them valid while
                # rotating, so a cache we could not update is not a loss.
                assert await auth_mod._refresh_keys(http) is True
        assert KID in auth_mod._jwks["keys"]
