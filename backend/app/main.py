"""The FastAPI application.

One process serves the API and, in production, the built frontend. That
keeps the deployment to a single artifact: no second web server, no
CORS configuration between two origins, and no separate static host.

The agent worker is a *separate* process. It has to be, because a
LiveKit agent connects to a room and holds a long-lived session; it
does not belong in the same process as the request handler.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import db as db_mod
from .api import account, admin, chat, flashcards, health, lectures, livekit
from .api import auth as auth_api
from .api.auth import require_user
from .config import PROJECT_ROOT, get_settings
from .logging_setup import configure_logging, get_logger
from .middleware import SecurityMiddleware
from .pipeline import cache_cleanup

log = get_logger(__name__)

FRONTEND_DIST = PROJECT_ROOT / "web" / "dist"


async def _prune_cache_forever() -> None:
    """Periodically prune old, unreferenced ingest work directories."""
    settings = get_settings()
    while True:
        await asyncio.sleep(settings.cache_prune_interval_seconds)
        try:
            with db_mod.get_conn() as conn:
                removed = await asyncio.to_thread(cache_cleanup.prune_ingest_cache, conn)
            if removed:
                log.info("ingest cache cleanup", extra={"removed": len(removed)})
        except Exception as exc:  # noqa: BLE001 - never let the timer die
            log.warning("ingest cache cleanup failed", extra={"error": str(exc)[:200]})


def create_app() -> FastAPI:
    configure_logging("INFO")

    app = FastAPI(
        title="Gnani Lecture-to-Learning Platform",
        version="0.1.0",
        description=(
            "Turn a classroom recording into structured study notes and a "
            "grounded voice tutor, using Gnani Prisma and Timbre."
        ),
    )

    # Middleware wraps in the order it is added: the last one added is the
    # outermost. Sessions therefore go on first, so that a CORS preflight
    # or a security header never has to read a cookie, and CORS goes on
    # last, so the browser sees the same response whether or not the
    # origin was allowed.
    settings = get_settings()
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret_value(),
        session_cookie=settings.session_cookie_name,
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        # Secure cookies need HTTPS. Off for the local http:// demo.
        https_only=settings.secure_session_cookies(),
    )

    # Added second, so it wraps the session layer: it stamps security
    # headers, rejects cross-origin writes, and rate-limits — without
    # ever having to read a cookie itself.
    app.add_middleware(SecurityMiddleware)

    # The frontend is served from the same origin in production, so CORS
    # is only needed for the Vite dev server. It goes on last — outermost
    # — so the browser sees the same response whether or not the origin
    # was allowed, including the 403 the security layer answers with.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth_api.router)

    # Everything that reads or writes lecture data needs a signed-in
    # request. Health and sign-in stay open: a probe that has to log in
    # is not a probe, and you cannot sign in if the sign-in route is
    # guarded.
    signed_in = [Depends(require_user)]
    app.include_router(account.router, dependencies=signed_in)
    app.include_router(admin.router, dependencies=signed_in)
    app.include_router(lectures.router, dependencies=signed_in)
    app.include_router(chat.router, dependencies=signed_in)
    app.include_router(flashcards.router, dependencies=signed_in)
    app.include_router(livekit.router, dependencies=signed_in)

    @app.on_event("startup")
    async def _startup() -> None:
        db_mod.init_db()
        settings = get_settings()
        if not settings.auth_required():
            log.warning(
                "open mode explicitly enabled; data routes are unauthenticated",
                extra={"hint": "remove OPEN_MODE before exposing this service"},
            )
        elif not settings.has_google_oauth():
            log.error(
                "authentication is not configured; data routes answer 401",
                extra={
                    "hint": "set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, "
                    "or OPEN_MODE=true for the local demo only"
                },
            )
        log.info(
            "api ready",
            extra={
                "database": str(settings.db_path),
                "frontend": FRONTEND_DIST.exists(),
                "auth": settings.has_google_oauth(),
                "auth_required": settings.auth_required(),
            },
        )

        if settings.cache_pruner_enabled:
            app.state.cache_pruner_task = asyncio.create_task(
                _prune_cache_forever(), name="ingest-cache-pruner"
            )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        task = getattr(app.state, "cache_pruner_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Never leak an internal error to the client."""
        log.error(
            "unhandled error",
            extra={"path": request.url.path, "error": str(exc)[:300]},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "Something went wrong. Please try again."},
        )

    # The built frontend, if it exists. Mounted last so the API routes
    # win.
    if FRONTEND_DIST.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=FRONTEND_DIST / "assets"),
            name="assets",
        )

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str) -> Any:
            """Serve the single-page app, falling back to index.html."""
            if full_path.startswith("api/"):
                return JSONResponse(status_code=404, content={"detail": "Not found"})
            candidate = FRONTEND_DIST / full_path
            if full_path and candidate.is_file():
                # Unhashed helpers (theme-init.js, sw.js, …) revalidate on
                # every use so a stale copy can never outlive a change.
                return FileResponse(candidate, headers={"Cache-Control": "no-cache"})
            # The entry document is the one file that must never be cached:
            # an old copy references bundle names that no longer exist.
            return FileResponse(
                FRONTEND_DIST / "index.html", headers={"Cache-Control": "no-store"}
            )

    return app


app = create_app()


def main() -> None:
    """Run the API server."""
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",  # noqa: S104 - the container/VM needs to reach it
        port=8000,
        log_level="info",
    )


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        main()
