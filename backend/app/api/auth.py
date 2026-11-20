"""Sign-in with Google, and the session that follows it.

The flow is plain OpenID Connect: the browser is sent to Google with a
random ``state``, Google sends it back to our callback with a ``code``,
and the code is exchanged server-side for an ``id_token``. The token is
verified against Google's published keys before anything from it is
trusted: signature, issuer, audience and expiry.

What the browser keeps is not a token but an opaque session id in a
signed cookie. The record it names lives in ``user_sessions``, so
signing out -- or revoking a cookie that was stolen -- is a row delete
that takes effect on the next request instead of whenever a token would
have expired.

If ``GOOGLE_CLIENT_ID`` / ``GOOGLE_CLIENT_SECRET`` are absent the app
**fails closed**: data routes answer 401. The local demo can opt into an
open, anonymous mode with ``OPEN_MODE=true``, which is reported by
``/api/auth/config`` so the frontend knows whether to show a sign-in
screen. Configuring Google credentials always wins over that flag.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from joserfc import jwt as jose_jwt
from joserfc.errors import BadSignatureError, JoseError
from joserfc.jwk import RSAKey
from joserfc.jwt import JWTClaimsRegistry
from joserfc.util import json_b64decode

from .. import db as db_mod
from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
#: Google rotates its signing keys; this is the discovery-free JWKS endpoint.
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
#: Google signs with either form of the issuer depending on the endpoint.
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

#: The user handed to every request while the app runs open.
ANONYMOUS_USER: dict[str, Any] = {
    "id": "anonymous",
    "google_sub": "",
    "email": "",
    "name": "Local",
}

#: Never hit Google's key endpoint more often than this, or a stream of
#: forged tokens -- each carrying a random ``kid`` -- turns into a stream
#: of outbound requests.
_JWKS_MIN_RETRY_SECONDS = 30.0

# kid -> JWK, plus the fetch bookkeeping. Process-local by design: the keys
# are public, so there is nothing to share between processes.
_jwks: dict[str, Any] = {
    "keys": {},
    "last_attempt": 0.0,
}


def reset_jwks_cache() -> None:
    """Forget cached Google signing keys. Tests call this between cases."""
    _jwks["keys"] = {}
    _jwks["last_attempt"] = 0.0


def _frontend_base(request: Request) -> str:
    """Origin the browser should return to, without a trailing slash."""
    return get_settings().frontend_base(str(request.base_url))


def _redirect_uri(request: Request) -> str:
    """The exact URI Google must send the browser back to.

    Registered verbatim in the Google Cloud console, so it is derived
    from the same base as the post-login redirect and can never drift
    from it.
    """
    return _frontend_base(request) + "/api/auth/google/callback"


def _safe_next(value: str | None) -> str:
    """Turn an untrusted ``next`` into a same-origin path.

    Anything that could leave the site -- a scheme, a protocol-relative
    ``//host``, a backslash (which browsers treat as a slash), or a
    header-splitting newline -- degrades to the home page.
    """
    if not value:
        return "/"
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    if "\\" in value or "\r" in value or "\n" in value:
        return "/"
    return value


def current_user(request: Request) -> dict[str, Any] | None:
    """The signed-in user, or ``None``.

    ``None`` means "not authenticated". An explicitly open demo is the only
    case where that is acceptable, and ``require_user`` decides it.
    """
    settings = get_settings()
    if not settings.has_google_oauth():
        return None

    sid = request.session.get("sid")
    if not isinstance(sid, str) or not sid:
        return None

    now = time.time()
    with db_mod.get_conn() as conn:
        row = conn.execute(
            """SELECT u.id, u.google_sub, u.email, u.name
               FROM user_sessions s
               JOIN users u ON u.id = s.user_id
               WHERE s.sid = ? AND s.expires_at > ?""",
            (sid, now),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "google_sub": row["google_sub"],
        "email": row["email"],
        "name": row["name"],
    }


async def require_user(request: Request) -> dict[str, Any]:
    """Router-level guard: 401 unless the request carries a live session.

    The only exception is an explicitly enabled local demo (``OPEN_MODE``
    with no Google credentials). An unconfigured deployment fails closed
    instead of silently exposing the data routes.
    """
    settings = get_settings()
    if not settings.auth_required():
        return dict(ANONYMOUS_USER)
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


# --------------------------------------------------------------------------
# OIDC helpers
# --------------------------------------------------------------------------


async def _fetch_jwks(client: httpx.AsyncClient) -> None:
    """Fetch Google's signing keys. Raises if the response is unusable."""
    _jwks["last_attempt"] = time.time()
    response = await client.get(GOOGLE_JWKS_URL)
    response.raise_for_status()
    payload = response.json()
    keys: dict[str, RSAKey] = {}
    for jwk in payload.get("keys", []):
        kid = jwk.get("kid")
        if not kid or jwk.get("kty") != "RSA":
            continue
        try:
            keys[kid] = RSAKey.import_key(jwk)
        except (ValueError, TypeError) as exc:  # pragma: no cover - defensive
            log.warning("skipping unusable JWK kid=%s: %s", kid, exc)
    if not keys:
        raise ValueError("Google JWKS contained no usable RSA keys")
    _jwks["keys"] = keys


async def _refresh_keys(client: httpx.AsyncClient) -> bool:
    """Try once to (re)load Google's signing keys. Never raises.

    Returns ``True`` when the cache now holds keys. Every call starts the
    30-second cooldown, whether or not it succeeds, so a stream of forged
    tokens -- each carrying a random ``kid`` -- cannot turn into a stream
    of outbound requests while Google's endpoint is down.

    Stale keys are kept when a refresh fails: Google keeps old keys valid
    while it rotates, so a cache we could not update still verifies.
    """
    now = time.time()
    if now - _jwks["last_attempt"] < _JWKS_MIN_RETRY_SECONDS and _jwks["keys"]:
        return True
    if now - _jwks["last_attempt"] < _JWKS_MIN_RETRY_SECONDS:
        # We already tried very recently and have nothing to show for it.
        return False
    try:
        await _fetch_jwks(client)
    except Exception as exc:
        log.warning("JWKS refresh failed: %s", exc)
        return bool(_jwks["keys"])
    return True


def _header_kid(id_token: str) -> str | None:
    """Peek the unprotected ``kid`` header so we know which key to fetch.

    Nothing here is trusted: it only chooses which key to verify against.
    The signature check that follows decides whether the token is real.
    """
    try:
        header = json_b64decode(id_token.split(".", 1)[0].encode())
        kid = json.loads(header).get("kid")
    except (ValueError, TypeError, IndexError):  # pragma: no cover - malformed
        return None
    return kid if isinstance(kid, str) else None


async def _verify_id_token(id_token: str, client: httpx.AsyncClient) -> dict[str, Any]:
    """Decode and fully validate Google's ``id_token``.

    Signature first (against a key looked up by ``kid``, refreshing the
    JWKS when the id is unknown or the signature does not match a rotated
    key), then the claims that make the token about *this* app: issuer,
    audience, expiry, and an email Google has actually verified.

    Every rejection is a 401. joserfc's errors derive from ``JoseError``,
    **not** ``ValueError``, so each site catches both: catching only
    ``ValueError`` would let a tampered signature or a wrong audience
    escape as a 500.
    """
    settings = get_settings()

    def pick(obj: Any) -> RSAKey:
        kid = obj.headers().get("kid")
        key = _jwks["keys"].get(kid)
        if key is None:
            raise ValueError(f"unknown signing key {kid!r}")
        return key

    # A kid we have never seen normally means Google rotated its keys.
    if _header_kid(id_token) not in _jwks["keys"]:
        await _refresh_keys(client)

    token = None
    last_error: Exception | None = None
    for attempt in (0, 1):
        try:
            token = jose_jwt.decode(id_token, pick, algorithms=["RS256"])
            break
        except BadSignatureError as exc:
            # We hold a key for this kid but it did not sign the token:
            # either the token is forged, or our cached key is stale and
            # Google has already rotated. One refresh-and-retry settles it.
            last_error = exc
            if attempt == 0:
                await _refresh_keys(client)
        except (JoseError, ValueError) as exc:
            last_error = exc
            break

    if token is None:
        log.warning("id_token rejected: %s", last_error)
        raise HTTPException(status_code=401, detail="Could not verify sign-in")

    claims = dict(token.claims)
    try:
        JWTClaimsRegistry(
            iss={"values": list(GOOGLE_ISSUERS)},
            aud={"value": settings.google_client_id},
            # Essential, not optional: a token without an expiry or a
            # subject is not a token this app will accept, even if the
            # signature is perfect.
            exp={"essential": True},
            sub={"essential": True},
        ).validate(claims)
    except (JoseError, ValueError) as exc:
        log.warning("rejected id_token: %s", exc)
        raise HTTPException(status_code=401, detail="Could not verify sign-in") from exc

    # An unverified address is not an identity: anyone can claim one at
    # a provider that has not confirmed it.
    if claims.get("email_verified") is False:
        raise HTTPException(status_code=401, detail="Email address is not verified")

    sub = claims.get("sub")
    if not sub or not isinstance(sub, str):
        raise HTTPException(status_code=401, detail="Sign-in token has no subject")
    return claims


async def _exchange_code(client: httpx.AsyncClient, code: str, redirect_uri: str) -> str:
    """Trade the authorization code for the ``id_token``.

    The client secret travels only in this server-to-server call; the
    browser never sees it.
    """
    settings = get_settings()
    response = await client.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )
    if response.status_code >= 400:
        log.warning("token exchange failed: HTTP %s", response.status_code)
        raise HTTPException(status_code=401, detail="Could not complete sign-in")
    payload = response.json()
    id_token = payload.get("id_token")
    if not id_token:
        raise HTTPException(status_code=401, detail="Token exchange returned no id_token")
    return str(id_token)


def _upsert_user(claims: dict[str, Any]) -> str:
    """Create or refresh the local account for a verified token."""
    now = time.time()
    sub = str(claims["sub"])
    email = str(claims.get("email") or "")
    name = str(claims.get("name") or "")
    picture = str(claims.get("picture") or "")

    with db_mod.get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM users WHERE google_sub = ?", (sub,)
        ).fetchone()
        if row is None:
            user_id = db_mod.new_id("usr")
            conn.execute(
                """INSERT INTO users (id, google_sub, email, name, picture,
                                      created_at, last_login_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (user_id, sub, email, name, picture, now, now),
            )
        else:
            user_id = row["id"]
            conn.execute(
                """UPDATE users
                   SET email = ?, name = ?, picture = ?, last_login_at = ?
                   WHERE id = ?""",
                (email, name, picture, now, user_id),
            )
    return user_id


def _open_session(user_id: str, user_agent: str) -> str:
    """Mint a session row and drop this user's expired ones."""
    settings = get_settings()
    now = time.time()
    sid = secrets.token_urlsafe(32)
    with db_mod.get_conn() as conn:
        conn.execute(
            "DELETE FROM user_sessions WHERE user_id = ? AND expires_at <= ?",
            (user_id, now),
        )
        conn.execute(
            """INSERT INTO user_sessions (sid, user_id, created_at, expires_at, user_agent)
               VALUES (?, ?, ?, ?, ?)""",
            (
                sid,
                user_id,
                now,
                now + settings.session_max_age_seconds,
                user_agent[:300],
            ),
        )
    return sid


def _error_redirect(request: Request, code: str) -> RedirectResponse:
    """Send the browser back to the app with a machine-readable reason.

    The app origin is used rather than echoing anything back: a failed
    sign-in must not become an open redirect.
    """
    base = _frontend_base(request)
    return RedirectResponse(
        f"{base}/?error={code}",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


@router.get("/config")
async def auth_config(request: Request) -> dict[str, Any]:
    """What the frontend needs to decide whether to show a sign-in screen."""
    settings = get_settings()
    return {
        "google_oauth": settings.has_google_oauth(),
        "open_mode": settings.open_mode and not settings.has_google_oauth(),
        "auth_required": settings.auth_required(),
        "authenticated": current_user(request) is not None,
    }


@router.get("/me")
async def auth_me(request: Request) -> dict[str, Any]:
    """The current user, or null. Never 401s: it is the boot probe."""
    settings = get_settings()
    return {
        "user": current_user(request),
        "google_oauth": settings.has_google_oauth(),
        "open_mode": settings.open_mode and not settings.has_google_oauth(),
        "auth_required": settings.auth_required(),
    }


@router.post("/logout")
async def auth_logout(request: Request) -> dict[str, Any]:
    """Revoke the session server-side, then drop the cookie."""
    sid = request.session.get("sid")
    if isinstance(sid, str) and sid:
        with db_mod.get_conn() as conn:
            conn.execute("DELETE FROM user_sessions WHERE sid = ?", (sid,))
    # Clears the cookie too: SessionMiddleware writes an expired one when
    # a previously non-empty session is emptied.
    request.session.clear()
    return {"ok": True}


@router.get("/google")
async def google_start(
    request: Request,
    next: str | None = Query(default=None),
) -> RedirectResponse:
    """Begin the OIDC flow, remembering where to come back to."""
    settings = get_settings()
    if not settings.has_google_oauth():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured")

    state = secrets.token_urlsafe(24)
    request.session["oauth_state"] = state
    request.session["oauth_next"] = _safe_next(next)

    params = urlencode(
        {
            "client_id": settings.google_client_id,
            "redirect_uri": _redirect_uri(request),
            "response_type": "code",
            # openid is what makes an id_token come back at all.
            "scope": "openid email profile",
            "state": state,
            # A returning user with several accounts should get the picker,
            # not a silent decision they cannot see.
            "prompt": "select_account",
        }
    )
    return RedirectResponse(
        f"{GOOGLE_AUTH_URL}?{params}",
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/google/callback")
async def google_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Finish the flow: verify, upsert, open a session, return to the app."""
    settings = get_settings()
    base = _frontend_base(request)

    stored_state = request.session.pop("oauth_state", None)
    next_path = _safe_next(request.session.pop("oauth_next", None))

    if error:
        log.info("Google returned an error: %s", error)
        return _error_redirect(request, "access_denied")

    # One-time state: popped above, so a replay of the same callback finds
    # nothing, and timing does not leak whether a state was ever issued.
    if not code or not state or not isinstance(stored_state, str):
        return _error_redirect(request, "state_mismatch")
    if not secrets.compare_digest(state, stored_state):
        return _error_redirect(request, "state_mismatch")

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.http_read_timeout,
                connect=settings.http_connect_timeout,
            ),
            follow_redirects=False,
        ) as client:
            id_token = await _exchange_code(client, code, _redirect_uri(request))
            claims = await _verify_id_token(id_token, client)
    except (httpx.HTTPError, ValueError, KeyError, HTTPException) as exc:
        # Every failure here is "this sign-in did not work": send the
        # student back to the app with a reason rather than a stack trace.
        if isinstance(exc, HTTPException):
            log.info("sign-in rejected: %s", exc.detail)
        else:
            log.warning("sign-in failed: %s", exc)
        return _error_redirect(request, "signin_failed")

    user_id = _upsert_user(claims)
    sid = _open_session(user_id, request.headers.get("user-agent", ""))
    request.session["sid"] = sid

    log.info("user signed in: %s", claims.get("email") or claims["sub"])
    return RedirectResponse(
        base + next_path,
        status_code=302,
        headers={"Cache-Control": "no-store"},
    )
