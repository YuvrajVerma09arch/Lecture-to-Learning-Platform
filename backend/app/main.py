"""The FastAPI application.

One process serves the API and, in production, the built frontend. That
keeps the deployment to a single artifact: no second web server, no
CORS configuration between two origins, and no separate static host.

The agent worker is a *separate* process. It has to be, because a
LiveKit agent connects to a room and holds a long-lived session; it
does not belong in the same process as the request handler.
"""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db as db_mod
from .api import chat, flashcards, health, lectures, livekit
from .config import PROJECT_ROOT, get_settings
from .logging_setup import configure_logging, get_logger

log = get_logger(__name__)

FRONTEND_DIST = PROJECT_ROOT / "web" / "dist"


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

    # The frontend is served from the same origin in production, so CORS
    # is only needed for the Vite dev server.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(lectures.router)
    app.include_router(chat.router)
    app.include_router(flashcards.router)
    app.include_router(livekit.router)

    @app.on_event("startup")
    async def _startup() -> None:
        db_mod.init_db()
        log.info(
            "api ready",
            extra={
                "database": str(get_settings().db_path),
                "frontend": FRONTEND_DIST.exists(),
            },
        )

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
                return FileResponse(candidate)
            return FileResponse(FRONTEND_DIST / "index.html")

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
