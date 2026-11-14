"""HTTP security middleware.

Three jobs, all cheap enough to run on every request:

1. **Headers.** A strict Content-Security-Policy plus the usual hardening
   headers (``nosniff``, ``X-Frame-Options``, ``Referrer-Policy``,
   ``Permissions-Policy``) on every response, and HSTS when the request
   arrived over HTTPS.
2. **Cross-origin writes.** A browser on another site must not be able to
   POST to this API using the visitor's session cookie. A request with no
   ``Origin`` header at all (curl, a script, the test client) is allowed:
   this check exists to stop *browsers* being tricked, and a client that
   forges its own headers is not subject to CSRF in the first place.
3. **Rate limits.** Per-client-address sliding windows over the three
   expensive families of routes: sign-in, upload, and every other write.
   A limit of 0 (or less) disables that bucket.

Middleware wraps in the order it is added, so this one is registered
after ``SessionMiddleware`` and before ``CORSMiddleware``: it must never
read a cookie itself, but its 403/429 answers still need the CORS
headers an outer layer adds, and the browser has to see those.
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from urllib.parse import urlsplit

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .config import Settings, get_settings
from .logging_setup import get_logger

log = get_logger(__name__)

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Sliding window length. Counters are per minute, so a 60s window with
# the configured "per minute" limits is the natural fit.
_WINDOW_SECONDS = 60.0

# Cap on distinct (address, bucket) counters before a full sweep. Keeps
# memory flat even if a scan shows up from thousands of addresses.
_MAX_BUCKETS = 4096

# The frontend is same-origin: scripts and styles it loads come from this
# server (Vite in development, the built bundle in production), it talks
# to the API with ``fetch``, and it reaches LiveKit over its own
# WebSocket. Lecture audio is played from a blob URL. ``unsafe-inline``
# for styles covers Tailwind's injected rules and React style attributes.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "media-src 'self' blob:; "
    "connect-src 'self' wss://*.livekit.cloud https://*.livekit.cloud; "
    "worker-src 'self' blob:; "
    "frame-ancestors 'none'; "
    "form-action 'self'; "
    "base-uri 'self'; "
    "object-src 'none'"
)


def _security_headers(scope: Scope) -> list[tuple[bytes, bytes]]:
    """Headers to stamp on every response, bytes-ready."""
    headers: list[tuple[bytes, bytes]] = [
        (b"content-security-policy", CONTENT_SECURITY_POLICY.encode("latin-1")),
        (b"x-content-type-options", b"nosniff"),
        (b"x-frame-options", b"DENY"),
        (b"referrer-policy", b"no-referrer"),
        # The voice tutor needs the microphone, but only from this origin.
        (b"permissions-policy", b"microphone=(self)"),
    ]
    if scope.get("scheme") == "https":
        headers.append(
            (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
        )
    return headers


def _stamp(message: Message, scope: Scope) -> None:
    """Attach the security headers to a ``response.start`` message.

    Anything the application set itself wins, so a route that needs a
    different policy for one response is not silently overridden.
    """
    if message["type"] != "http.response.start":
        return
    existing = {name for name, _ in message.get("headers", [])}
    outgoing = message.setdefault("headers", [])
    for name, value in _security_headers(scope):
        if name not in existing:
            outgoing.append((name, value))


def _origin_allowed(headers: Headers, allowed: list[str]) -> bool:
    """True when the browser may perform a state-changing request.

    No ``Origin`` means a non-browser client: allowed. An origin on the
    allowlist (the Vite dev server, any configured extra origin) is
    allowed. Otherwise the origin must at least share this request's
    host — a TLS-terminating proxy changes the scheme ASGI sees from the
    one the browser used, so schemes are compared loosely, but the host
    never is.
    """
    origin = headers.get("origin")
    if origin is None:
        return True
    if origin in allowed:
        return True
    host = (headers.get("host") or "").lower()
    netloc = urlsplit(origin).netloc.lower()
    return bool(netloc) and netloc == host


def _bucket(path: str, method: str, settings: Settings) -> tuple[str, int] | None:
    """Which rate-limit bucket this request belongs to, if any."""
    if path.startswith("/api/auth/"):
        return "auth", settings.rate_limit_auth_per_minute
    if path == "/api/lectures/upload":
        return "upload", settings.rate_limit_upload_per_minute
    if method in _WRITE_METHODS:
        return "write", settings.rate_limit_write_per_minute
    return None


class _RateLimiter:
    """Sliding-window counters keyed by client address and bucket."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = {}

    def reset(self) -> None:
        """Drop every counter (tests use this)."""
        self._hits.clear()

    def retry_after(self, key: str, limit: int) -> float | None:
        """Seconds the caller must wait, or ``None`` to let it through.

        Only requests that are allowed count towards the window, so a
        rejected request never extends its own penalty.
        """
        if limit <= 0:
            return None
        now = time.monotonic()
        hits = self._hits.setdefault(key, deque())
        cutoff = now - _WINDOW_SECONDS
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return max(1.0, hits[0] + _WINDOW_SECONDS - now)
        hits.append(now)
        if len(self._hits) > _MAX_BUCKETS:
            self._sweep(cutoff)
        return None

    def _sweep(self, cutoff: float) -> None:
        stale = [key for key, hits in self._hits.items() if not hits or hits[-1] <= cutoff]
        for key in stale:
            del self._hits[key]


_LIMITER = _RateLimiter()


def reset_rate_limiter() -> None:
    """Clear every rate-limit counter. Tests use this for isolation."""
    _LIMITER.reset()


async def _send_json(
    scope: Scope,
    send: Send,
    status: int,
    payload: dict[str, str],
    extra: list[tuple[bytes, bytes]] | None = None,
) -> None:
    """Answer directly, with the security headers the browser expects."""
    body = json.dumps(payload).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    headers.extend(extra or [])
    headers.extend(_security_headers(scope))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class SecurityMiddleware:
    """Headers, cross-origin write rejection, and rate limiting."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        settings = get_settings()
        headers = Headers(scope=scope)
        method = scope["method"]
        path = scope["path"]

        # 1. A browser on another origin must not be able to write.
        if method in _WRITE_METHODS and not _origin_allowed(
            headers, settings.cors_origins()
        ):
            log.warning(
                "cross-origin write rejected",
                extra={"path": path, "origin": headers.get("origin", "")},
            )
            await _send_json(
                scope, send, 403, {"detail": "Cross-origin request blocked"}
            )
            return

        # 2. Rate limit. CORS preflights never get here (the outer CORS
        # middleware answers them itself), and plain OPTIONS is harmless.
        bucket = _bucket(path, method, settings)
        if bucket is not None and method != "OPTIONS":
            name, limit = bucket
            client = scope.get("client") or ("unknown", 0)
            retry = _LIMITER.retry_after(f"{client[0]}::{name}", limit)
            if retry is not None:
                seconds = int(math.ceil(retry))
                log.warning(
                    "rate limit exceeded",
                    extra={"bucket": name, "path": path},
                )
                await _send_json(
                    scope,
                    send,
                    429,
                    {"detail": "Too many requests"},
                    extra=[(b"retry-after", str(seconds).encode("ascii"))],
                )
                return

        # 3. Everything else, with the security headers on the way out.
        async def send_with_headers(message: Message) -> None:
            _stamp(message, scope)
            await send(message)

        await self.app(scope, receive, send_with_headers)
