"""End-to-end smoke test: boot the real server against the built frontend.

The unit suite proves each piece in isolation; this proves the assembled
product: one uvicorn process serving ``web/dist`` and the API, exercised
over HTTP exactly the way a browser would be.

Two servers are started, because the app has two identities:

* **Run A — open mode** (``GOOGLE_CLIENT_ID`` empty): the demo deployment.
  The API is unauthenticated, the SPA is served, the security headers and
  cross-origin guard are asserted, and upload rejections are checked.
* **Run B — configured mode**: Google sign-in is on, so the data routes
  must answer 401 until a session exists, and the OAuth entry points must
  behave (redirect to Google, reject a forged ``state``).

Checks are collected rather than failing fast, so one run reports every
problem. Exit codes: 0 all passed, 1 some failed, 2 environment problem.

Requires ``web/dist`` (``cd web && npm run build``). Run with the venv::

    .venv/bin/python scripts/e2e_smoke.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "backend"
DIST = PROJECT_ROOT / "web" / "dist"

OPEN_PORT = 8123
CONFIGURED_PORT = 8124

#: Each entry is (assertion name, passed, evidence shown on failure).
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record one assertion. Never raises: the run continues either way."""
    RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    suffix = f"  [{detail}]" if detail and not ok else ""
    print(f"  {mark}  {name}{suffix}", flush=True)


def _json(response: httpx.Response) -> dict:
    """The response body as an object, or ``{}`` if it is not a mapping."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def build_env(tmp: Path, run: str, google_configured: bool) -> dict[str, str]:
    """The child process environment: isolated storage, dummy secrets."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    env["DATA_DIR"] = str(tmp / run / "data")
    env["DB_PATH"] = str(tmp / run / "data" / "app.sqlite3")
    env["CACHE_DIR"] = str(tmp / run / "data" / "cache")
    # Dummy Gnani key so the upload gate is reached; nothing transcribes.
    env["GNANI_API_KEY"] = "dummy-gnani-key-for-smoke-test"
    env["SESSION_SECRET"] = "smoke-test-session-secret-0123456789abcdef"
    # No provider keys: no request in this script reaches a provider.
    env["GOOGLE_API_KEY"] = ""
    env["GROQ_API_KEY"] = ""
    env["LIVEKIT_API_KEY"] = ""
    env["LIVEKIT_API_SECRET"] = ""
    env["GOOGLE_CLIENT_ID"] = "dummy.apps.googleusercontent.com" if google_configured else ""
    env["GOOGLE_CLIENT_SECRET"] = "dummy-oauth-secret" if google_configured else ""
    return env


def warm_import(env: dict[str, str]) -> None:
    """Import the app once so the servers below start from a warm cache.

    The first cold import pulls in ``livekit.plugins`` and can take
    minutes; doing it here, with a visible message, keeps server startup
    timeouts honest.
    """
    print("warming the import cache (first cold import can take minutes)...", flush=True)
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import app.main"],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        print("FATAL: importing app.main timed out after 600s", file=sys.stderr)
        raise SystemExit(2) from None
    if proc.returncode != 0:
        print("FATAL: importing app.main failed:\n" + proc.stderr[-2000:], file=sys.stderr)
        raise SystemExit(2)


def start_server(port: int, env: dict[str, str], log_path: Path) -> subprocess.Popen:
    """Boot uvicorn on ``port``, logging to ``log_path``."""
    with log_path.open("wb") as log_file:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
                "--no-access-log",
            ],
            cwd=PROJECT_ROOT,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )


def wait_ready(base: str, proc: subprocess.Popen, log_path: Path) -> str:
    """Poll ``/api/health`` until the server answers. Returns an error, or ``""``."""
    deadline = time.monotonic() + 60
    with httpx.Client(base_url=base, timeout=2.0, trust_env=False) as client:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = log_path.read_text(errors="replace")[-2000:]
                return f"server exited with code {proc.returncode}:\n{tail}"
            try:
                if client.get("/api/health").status_code == 200:
                    return ""
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
    return f"timed out waiting for {base}/api/health; log tail:\n" + log_path.read_text(
        errors="replace"
    )[-2000:]


def stop_server(proc: subprocess.Popen) -> None:
    """Terminate politely, then kill. Never raises."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def run_open_mode(base: str) -> None:
    """Run A: Google sign-in unconfigured — the open demo deployment."""
    print(f"\n== run A: open mode ({base}) ==", flush=True)
    with httpx.Client(base_url=base, timeout=20.0, follow_redirects=False, trust_env=False) as c:
        # --- liveness and readiness ---
        r = c.get("/api/health")
        check("health answers ok", r.status_code == 200 and _json(r).get("status") == "ok",
              f"{r.status_code} {r.text[:120]}")
        r = c.get("/api/ready")
        ready = _json(r)
        check(
            "ready reports the database working",
            r.status_code == 200
            and ready.get("status") == "ready"
            and ready.get("checks", {}).get("database", {}).get("ok") is True,
            f"{r.status_code} {r.text[:200]}",
        )

        # --- the built frontend ---
        r = c.get("/")
        check("SPA index is served", r.status_code == 200 and '<div id="root">' in r.text,
              f"{r.status_code}")
        r = c.get("/flashcards")
        check("deep link falls back to index.html",
              r.status_code == 200 and '<div id="root">' in r.text, f"{r.status_code}")
        hashed = sorted((DIST / "assets").glob("*.js"))
        if hashed:
            r = c.get(f"/assets/{hashed[0].name}")
            check("hashed bundle asset is served",
                  r.status_code == 200 and len(r.content) > 0, f"{r.status_code}")
        else:
            check("hashed bundle asset is served", False, "web/dist/assets has no .js files")
        r = c.get("/theme-init.js")
        check("theme-init.js ships and touches localStorage",
              r.status_code == 200 and "localStorage" in r.text, f"{r.status_code}")
        r = c.get("/api/definitely-not-a-route")
        check("unknown API path is a JSON 404, not the SPA",
              r.status_code == 404 and _json(r).get("detail") == "Not found",
              f"{r.status_code} {r.text[:80]}")

        # --- security headers, on a real page response ---
        r = c.get("/")
        csp = r.headers.get("content-security-policy", "")
        for directive in (
            "script-src 'self'",
            "frame-ancestors 'none'",
            "object-src 'none'",
            "base-uri 'self'",
        ):
            check(f"CSP contains {directive}", directive in csp, csp[:160])
        check("X-Content-Type-Options is nosniff",
              r.headers.get("x-content-type-options") == "nosniff")
        check("X-Frame-Options is DENY", r.headers.get("x-frame-options") == "DENY")
        check("Referrer-Policy is no-referrer",
              r.headers.get("referrer-policy") == "no-referrer")
        check("Permissions-Policy allows only this origin's microphone",
              r.headers.get("permissions-policy") == "microphone=(self)",
              r.headers.get("permissions-policy", ""))

        # --- open-mode auth surface ---
        r = c.get("/api/auth/me")
        me = _json(r)
        check("me: anonymous user, OAuth off",
              r.status_code == 200 and me.get("user") is None and me.get("google_oauth") is False,
              f"{r.status_code} {r.text[:120]}")
        r = c.get("/api/lectures")
        check("lectures list is readable without a session",
              r.status_code == 200, f"{r.status_code}")
        r = c.get("/api/auth/google")
        check("google sign-in start answers 503 when unconfigured",
              r.status_code == 503
              and _json(r).get("detail") == "Google sign-in is not configured",
              f"{r.status_code} {r.text[:120]}")

        # --- cross-origin writes are blocked before the route runs ---
        r = c.post("/api/lectures/upload", headers={"Origin": "https://evil.example"})
        check("cross-origin POST is rejected",
              r.status_code == 403 and _json(r).get("detail") == "Cross-origin request blocked",
              f"{r.status_code} {r.text[:120]}")

        # --- upload gates ---
        r = c.post(
            "/api/lectures/upload",
            data={"title": "Smoke"},
            files={"file": ("notes.txt", b"definitely not audio", "text/plain")},
        )
        check("wrong file type is rejected with 400",
              r.status_code == 400 and "Unsupported file type" in _json(r).get("detail", ""),
              f"{r.status_code} {r.text[:160]}")
        r = c.post(
            "/api/lectures/upload",
            data={"title": "Smoke"},
            files={"file": ("empty.mp3", b"", "audio/mpeg")},
        )
        check("empty file is rejected with 400",
              r.status_code == 400 and "empty" in _json(r).get("detail", "").lower(),
              f"{r.status_code} {r.text[:160]}")
        r = c.post(
            "/api/lectures/upload",
            data={"title": "Smoke"},
            files={"wrong_field": ("x.mp3", b"abc", "audio/mpeg")},
        )
        check("missing file field is a 422 validation error",
              r.status_code == 422, f"{r.status_code} {r.text[:160]}")

        # --- rate limiting, last: it poisons the auth bucket for this server ---
        print("  ... hammering /api/auth/* until the window trips", flush=True)
        limiter = None
        attempts = 0
        for _ in range(60):
            attempts += 1
            limiter = c.get("/api/auth/me")
            if limiter.status_code == 429:
                break
        check("auth bucket trips to 429",
              limiter is not None and limiter.status_code == 429,
              f"last status {limiter.status_code if limiter else 'n/a'} after {attempts} requests")
        if limiter is not None and limiter.status_code == 429:
            check("429 says Too many requests",
                  _json(limiter).get("detail") == "Too many requests", limiter.text[:80])
            retry = limiter.headers.get("retry-after", "")
            check("429 carries Retry-After",
                  retry.isdigit() and int(retry) >= 1, retry)


def run_configured_mode(base: str) -> None:
    """Run B: Google sign-in configured — the API must demand a session."""
    print(f"\n== run B: configured mode ({base}) ==", flush=True)
    with httpx.Client(base_url=base, timeout=20.0, follow_redirects=False, trust_env=False) as c:
        r = c.get("/api/auth/me")
        me = _json(r)
        check("me: anonymous user, OAuth on",
              r.status_code == 200 and me.get("user") is None and me.get("google_oauth") is True,
              f"{r.status_code} {r.text[:120]}")

        r = c.get("/api/lectures")
        check("lectures demand sign-in with 401",
              r.status_code == 401 and _json(r).get("detail") == "Sign in required",
              f"{r.status_code} {r.text[:120]}")
        r = c.post(
            "/api/lectures/upload",
            data={"title": "Smoke"},
            files={"file": ("x.mp3", b"abc", "audio/mpeg")},
        )
        check("upload demands sign-in with 401 (before the body is read)",
              r.status_code == 401 and _json(r).get("detail") == "Sign in required",
              f"{r.status_code} {r.text[:120]}")

        r = c.get("/api/auth/google")
        location = r.headers.get("location", "")
        check("sign-in start redirects to Google",
              r.status_code == 302
              and location.startswith("https://accounts.google.com/o/oauth2/v2/auth"),
              f"{r.status_code} {location[:160]}")
        check("authorize URL carries client_id, redirect_uri and state",
              all(key in location for key in ("client_id=", "redirect_uri=", "state=")),
              location[:200])
        cookie = r.headers.get("set-cookie", "")
        check("oauth state is kept in the l2lp_session cookie",
              "l2lp_session=" in cookie, cookie[:120])

        # A callback whose state does not match the one in the cookie must
        # bounce back with an error, never reach Google's token endpoint.
        # (Error redirects use Starlette's default 307; only the success
        # path is pinned to 302.)
        r = c.get("/api/auth/google/callback",
                  params={"code": "forged-code", "state": "forged-state"})
        location = r.headers.get("location", "")
        check("forged oauth state is rejected",
              300 <= r.status_code < 400 and "error=state_mismatch" in location,
              f"{r.status_code} {location[:160]}")


def main() -> int:
    if not (DIST / "index.html").exists():
        print("FATAL: web/dist/index.html is missing — run `cd web && npm run build` first.",
              file=sys.stderr)
        return 2

    print("Lecture to Learning — end-to-end smoke test", flush=True)
    with tempfile.TemporaryDirectory(prefix="l2lp-smoke-") as tmp_name:
        tmp = Path(tmp_name)
        open_env = build_env(tmp, "open", google_configured=False)
        warm_import(open_env)

        procs: list[subprocess.Popen] = []
        try:
            print(f"starting server (open mode) on :{OPEN_PORT}...", flush=True)
            log_a = tmp / "server-open.log"
            proc_a = start_server(OPEN_PORT, open_env, log_a)
            procs.append(proc_a)
            base_a = f"http://127.0.0.1:{OPEN_PORT}"
            error = wait_ready(base_a, proc_a, log_a)
            if error:
                print("FATAL: open-mode server did not start:\n" + error, file=sys.stderr)
                return 2
            run_open_mode(base_a)
            stop_server(proc_a)

            print(f"\nstarting server (configured mode) on :{CONFIGURED_PORT}...", flush=True)
            log_b = tmp / "server-configured.log"
            proc_b = start_server(CONFIGURED_PORT, build_env(tmp, "cfg", True), log_b)
            procs.append(proc_b)
            base_b = f"http://127.0.0.1:{CONFIGURED_PORT}"
            error = wait_ready(base_b, proc_b, log_b)
            if error:
                print("FATAL: configured-mode server did not start:\n" + error, file=sys.stderr)
                return 2
            run_configured_mode(base_b)
            stop_server(proc_b)
        finally:
            for proc in procs:
                stop_server(proc)

    failed = [name for name, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed.", flush=True)
    for name in failed:
        print(f"  FAILED: {name}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
