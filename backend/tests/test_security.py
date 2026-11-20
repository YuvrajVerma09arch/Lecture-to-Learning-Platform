"""Security middleware tests.

Three jobs, three groups: response headers, cross-origin write
rejection, and per-address rate limits. Nothing here calls an external
service or touches the real database.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import config as config_mod
from app import db as db_mod
from app import middleware as mw
from app.middleware import CONTENT_SECURITY_POLICY, reset_rate_limiter


@pytest.fixture(autouse=True)
def clean_limits():
    """Each test starts and ends with an empty limiter."""
    reset_rate_limiter()
    yield
    reset_rate_limiter()


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def limits(monkeypatch: pytest.MonkeyPatch):
    """Set rate limits for one test, then put the defaults back."""

    def apply(**values: int) -> None:
        for key, value in values.items():
            monkeypatch.setenv(key, str(value))
        config_mod.reset_settings_cache()

    yield apply
    config_mod.reset_settings_cache()


class TestSecurityHeaders:
    def test_a_normal_response_carries_the_hardening_headers(self, client):
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "microphone=(self)" in response.headers["permissions-policy"]

    def test_the_content_security_policy_locks_scripts_to_same_origin(self, client):
        csp = client.get("/api/health").headers["content-security-policy"]
        assert csp == CONTENT_SECURITY_POLICY
        assert "default-src 'self'" in csp
        assert "script-src 'self'" in csp
        assert "unsafe-eval" not in csp
        assert "frame-ancestors 'none'" in csp

    def test_the_policy_reaches_livekit_and_blob_audio(self, client):
        csp = client.get("/api/health").headers["content-security-policy"]
        assert "wss://*.livekit.cloud" in csp
        assert "media-src 'self' blob:" in csp

    def test_error_responses_are_stamped_too(self, client):
        response = client.get("/api/no-such-route")
        assert response.status_code == 404
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" in response.headers

    def test_hsts_is_sent_only_over_https(self, client):
        assert "strict-transport-security" not in client.get("/api/health").headers

        with TestClient(client.app, base_url="https://testserver") as secure:
            response = secure.get("/api/health")
            assert response.headers["strict-transport-security"].startswith("max-age=")


class TestCrossOriginWrites:
    def test_a_foreign_origin_cannot_post(self, client):
        response = client.post("/api/auth/logout", headers={"origin": "https://evil.example"})
        assert response.status_code == 403
        assert response.json() == {"detail": "Cross-origin request blocked"}

    def test_the_rejection_still_carries_the_security_headers(self, client):
        response = client.post("/api/auth/logout", headers={"origin": "https://evil.example"})
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" in response.headers

    def test_the_same_origin_may_post(self, client):
        response = client.post("/api/auth/logout", headers={"origin": "http://testserver"})
        assert response.status_code != 403

    def test_the_dev_server_origin_is_allowed(self, client):
        response = client.post(
            "/api/auth/logout", headers={"origin": "http://localhost:5173"}
        )
        assert response.status_code != 403

    def test_a_request_without_an_origin_is_allowed(self, client):
        # curl, scripts, and the test client send no Origin at all.
        assert client.post("/api/auth/logout").status_code != 403

    def test_gets_are_not_origin_checked(self, client):
        response = client.get("/api/health", headers={"origin": "https://evil.example"})
        assert response.status_code == 200

    def test_an_opaque_origin_is_rejected(self, client):
        # Sandboxed iframes and file:// pages send Origin: null.
        response = client.post("/api/auth/logout", headers={"origin": "null"})
        assert response.status_code == 403


class TestRateLimits:
    def test_the_auth_bucket_throttles_sign_in_routes(self, client, limits):
        limits(RATE_LIMIT_AUTH_PER_MINUTE=2)

        assert client.get("/api/auth/me").status_code == 200
        assert client.get("/api/auth/me").status_code == 200
        blocked = client.get("/api/auth/me")
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) >= 1

    def test_the_upload_bucket_throttles_uploads_on_its_own(self, client, limits):
        limits(RATE_LIMIT_UPLOAD_PER_MINUTE=1, RATE_LIMIT_WRITE_PER_MINUTE=1000000)

        first = client.post("/api/lectures/upload")
        second = client.post("/api/lectures/upload")
        assert first.status_code != 429
        assert second.status_code == 429

    def test_the_write_bucket_throttles_other_posts(self, client, limits):
        limits(RATE_LIMIT_WRITE_PER_MINUTE=2)

        statuses = [client.post("/api/chat", json={}).status_code for _ in range(3)]
        assert all(status != 429 for status in statuses[:2])
        assert statuses[2] == 429

    def test_reads_are_not_throttled_by_the_write_bucket(self, client, limits):
        limits(RATE_LIMIT_WRITE_PER_MINUTE=1)

        for _ in range(5):
            assert client.get("/api/health").status_code == 200

    def test_a_limit_of_zero_disables_the_bucket(self, client, limits):
        limits(RATE_LIMIT_AUTH_PER_MINUTE=0)

        for _ in range(5):
            assert client.get("/api/auth/me").status_code == 200

    def test_the_window_slides(self, client, limits, monkeypatch):
        """An old hit must stop counting once it leaves the window."""
        limits(RATE_LIMIT_AUTH_PER_MINUTE=1)

        assert client.get("/api/auth/me").status_code == 200
        assert client.get("/api/auth/me").status_code == 429

        # Age the recorded hit past the window without waiting a minute.
        mw._LIMITER.age_all_hits(mw._WINDOW_SECONDS + 1)

        assert client.get("/api/auth/me").status_code == 200

    def test_hits_from_another_connection_share_the_quota(self, client, limits):
        """A second API process writes to the same file, not its own memory."""
        limits(RATE_LIMIT_AUTH_PER_MINUTE=2)

        assert client.get("/api/auth/me").status_code == 200
        # Simulate a hit recorded by another worker against the same key.
        conn = db_mod.connect_raw()
        try:
            bucket = conn.execute("SELECT bucket FROM rate_limit_hits LIMIT 1").fetchone()[0]
            conn.execute(
                "INSERT INTO rate_limit_hits(bucket, hit_at) VALUES (?, ?)",
                (bucket, time.monotonic()),
            )
            conn.commit()
        finally:
            conn.close()

        assert client.get("/api/auth/me").status_code == 429


class TestSessionCookie:
    def _set_cookie(self, monkeypatch, *, frontend: str, override: str | None) -> str:
        from app.main import create_app

        monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client")
        monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
        monkeypatch.setenv("FRONTEND_URL", frontend)
        if override is None:
            monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
        else:
            monkeypatch.setenv("SESSION_COOKIE_SECURE", override)
        config_mod.reset_settings_cache()
        try:
            # create_app reads the settings once, the way a deployment does.
            with TestClient(create_app(), base_url="https://testserver") as client:
                response = client.get("/api/auth/google", follow_redirects=False)
            assert response.status_code == 302
            return response.headers.get("set-cookie", "")
        finally:
            config_mod.reset_settings_cache()

    def test_an_https_frontend_gets_a_secure_cookie_by_default(self, monkeypatch):
        cookie = self._set_cookie(
            monkeypatch, frontend="https://app.example.com", override=None
        )
        assert "l2lp_session=" in cookie
        assert "secure" in cookie.lower()
        assert "httponly" in cookie.lower()
        assert "samesite=lax" in cookie.lower()

    def test_a_local_http_frontend_stays_insecure(self, monkeypatch):
        cookie = self._set_cookie(
            monkeypatch, frontend="http://localhost:5173", override=None
        )
        assert "l2lp_session=" in cookie
        assert "secure" not in cookie.lower()

    def test_an_explicit_false_overrides_an_https_frontend(self, monkeypatch):
        cookie = self._set_cookie(
            monkeypatch, frontend="https://app.example.com", override="false"
        )
        assert "secure" not in cookie.lower()

    def test_an_explicit_true_protects_an_http_frontend(self, monkeypatch):
        cookie = self._set_cookie(
            monkeypatch, frontend="http://localhost:5173", override="true"
        )
        assert "secure" in cookie.lower()
