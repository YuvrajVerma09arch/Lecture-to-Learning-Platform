"""Service-to-service endpoints for the hosted LiveKit agent.

The agent runs in LiveKit's infrastructure and cannot read this process's
SQLite file. It asks for the orientation data over HTTPS with a bearer token;
every request is still scoped to the lecture's owner, which the API put in
the room metadata when it minted the token.
"""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db as db_mod
from ..config import get_settings

router = APIRouter(prefix="/api/internal", tags=["internal"])


def _require_service_token(request: Request) -> None:
    settings = get_settings()
    if not settings.agent_api_token:
        raise HTTPException(status_code=503, detail="Agent API is not configured")
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied, f"Bearer {settings.agent_api_token}"):
        raise HTTPException(status_code=401, detail="Invalid service token")


@router.get("/lectures/{lecture_id}/context")
async def lecture_context(
    lecture_id: str,
    request: Request,
    owner_id: str = "",
    _token: None = Depends(_require_service_token),  # noqa: B008
) -> dict[str, Any]:
    """Title, summary and notes document for one owned lecture."""
    with db_mod.get_conn() as conn:
        if db_mod.owned_lecture(conn, lecture_id, owner_id) is None:
            raise HTTPException(status_code=404, detail="No such lecture")
        row = conn.execute(
            "SELECT l.title, n.summary, n.document "
            "FROM lectures l LEFT JOIN notes n ON n.lecture_id = l.id "
            "WHERE l.id = ? AND l.owner_id = ?",
            (lecture_id, owner_id),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such lecture")
    return {
        "title": row["title"] or "",
        "summary": row["summary"] or "",
        "document": row["document"] or "",
    }
