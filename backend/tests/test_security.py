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
        for hits in mw._LIMITER._hits.values():
            hits.clear()
            hits.append(time.monotonic() - mw._WINDOW_SECONDS - 1)

        assert client.get("/api/auth/me").status_code == 200
