"""Read-only operator overview.

Admin status is derived from the email in a verified Google id_token via
``ADMIN_EMAILS``; there is no role column to grant or escalate, and open-mode
users have no email, so they can never be admins.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from .. import db as db_mod
from ..config import get_settings
from .auth import require_user

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/overview")
async def overview(
    user: dict[str, Any] = Depends(require_user),  # noqa: B008
) -> dict[str, Any]:
    """How many accounts and lectures exist, without exposing lecture text."""
    settings = get_settings()
    if not settings.is_admin(user.get("email", "")):
        raise HTTPException(status_code=403, detail="Admin access required")

    with db_mod.get_conn() as conn:
        users = [
            dict(row)
            for row in conn.execute(
                "SELECT u.id, u.email, u.name, u.created_at, u.last_login_at, "
                "COUNT(l.id) AS lecture_count "
                "FROM users u LEFT JOIN lectures l ON l.owner_id=u.id "
                "GROUP BY u.id ORDER BY u.created_at DESC LIMIT 100"
            )
        ]
        anonymous_lectures = conn.execute(
            "SELECT COUNT(*) FROM lectures WHERE owner_id='anonymous'"
        ).fetchone()[0]
        total_lectures = conn.execute("SELECT COUNT(*) FROM lectures").fetchone()[0]

    return {
        "users": users,
        "anonymous_lectures": anonymous_lectures,
        "totals": {"users": len(users), "lectures": total_lectures},
        "limits": {"max_lectures_per_user": settings.max_lectures_per_user},
    }
