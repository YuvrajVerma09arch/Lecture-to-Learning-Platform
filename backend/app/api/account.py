"""Data-subject export and erasure for signed-in students.

The lecture rows are the unit of ownership: deleting them cascades to
segments, notes, cards, their review state and the FTS index. The shared,
content-addressed transcript cache is *not* deleted when another student
still has a lecture with the same audio hash.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import db as db_mod
from ..config import get_settings
from .auth import require_user

router = APIRouter(prefix="/api/account", tags=["account"])

_LEC_ID = re.compile(r"^lec_[0-9a-f]{16}$")
_HASH_DIR = re.compile(r"^[0-9a-f]{16}$")

# Hard-coded table names: these are never taken from request data.
_LECTURE_CHILDREN = (
    ("segments", "seq"),
    ("note_topics", "seq"),
    ("notes", "created_at"),
    ("stage_state", "stage"),
    ("flashcards", "created_at"),
)


def _require_configured(user: dict[str, Any]) -> None:
    """Open mode has no account to export or erase."""
    if not get_settings().has_google_oauth() or user["id"] == "anonymous":
        raise HTTPException(
            status_code=400,
            detail="Account data is available only when sign-in is configured",
        )


def _safe_child(
    root: Path, name: str, pattern: re.Pattern[str], suffix: str = ""
) -> Path | None:
    """A validated child path, or ``None`` if the name or target is unsafe."""
    if not pattern.fullmatch(name):
        return None
    base = root.resolve()
    child = (base / f"{name}{suffix}").resolve()
    if child == base or not child.is_relative_to(base):
        return None
    return child


@router.get("/export")
async def export_account(
    user: dict[str, Any] = Depends(require_user),  # noqa: B008
) -> JSONResponse:
    """Everything tied to this account, as a downloadable JSON document."""
    _require_configured(user)
    settings = get_settings()

    with db_mod.get_conn() as conn:
        profile = conn.execute(
            "SELECT id, email, name, picture, created_at, last_login_at "
            "FROM users WHERE id=?",
            (user["id"],),
        ).fetchone()
        lectures = conn.execute(
            "SELECT * FROM lectures WHERE owner_id=? ORDER BY created_at", (user["id"],)
        ).fetchall()

        has_vectors = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='segment_embeddings'"
        ).fetchone() is not None

        exported: list[dict[str, Any]] = []
        for lecture in lectures:
            lecture_id = lecture["id"]
            item: dict[str, Any] = dict(lecture)
            for table, order in _LECTURE_CHILDREN:
                item[table] = [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} WHERE lecture_id=? ORDER BY {order}",
                        (lecture_id,),
                    )
                ]
            item["flashcard_reviews"] = [
                dict(row)
                for row in conn.execute(
                    "SELECT r.* FROM flashcard_reviews r "
                    "JOIN flashcards f ON f.id=r.flashcard_id "
                    "WHERE f.lecture_id=?",
                    (lecture_id,),
                )
            ]
            item["segment_embeddings"] = (
                [
                    {"segment_id": row["segment_id"], "model": row["model"], "dims": row["dims"]}
                    for row in conn.execute(
                        "SELECT segment_id, model, dims FROM segment_embeddings "
                        "WHERE segment_id IN (SELECT id FROM segments WHERE lecture_id=?)",
                        (lecture_id,),
                    )
                ]
                if has_vectors
                else []
            )
            note_file = _safe_child(settings.data_dir / "notes", lecture_id, _LEC_ID, ".md")
            if note_file and note_file.is_file():
                item["notes_markdown_file"] = note_file.read_text(encoding="utf-8")
            exported.append(item)

    payload = {
        "format": "lecture-to-learning/account-export-v1",
        "profile": dict(profile) if profile else None,
        "lectures": exported,
    }
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": 'attachment; filename="lecture-to-learning-export.json"'},
    )


@router.delete("")
async def delete_account(
    request: Request,
    user: dict[str, Any] = Depends(require_user),  # noqa: B008
) -> dict[str, Any]:
    """Erase this account and everything it owns; revoke its sessions."""
    _require_configured(user)
    settings = get_settings()

    with db_mod.get_conn() as conn:
        owned = conn.execute(
            "SELECT id, content_hash FROM lectures WHERE owner_id=?", (user["id"],)
        ).fetchall()
        lecture_ids = [row["id"] for row in owned]
        hashes = {row["content_hash"] for row in owned}

        conn.execute("DELETE FROM lectures WHERE owner_id=?", (user["id"],))
        conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user["id"],))
        conn.execute("DELETE FROM users WHERE id=?", (user["id"],))

        # A content-addressed cache directory can only go once no lecture
        # anywhere still points at that hash.
        orphaned = [
            digest[:16]
            for digest in hashes
            if re.fullmatch(r"[0-9a-f]{64}", digest or "")
            and conn.execute(
                "SELECT 1 FROM lectures WHERE content_hash=? LIMIT 1", (digest,)
            ).fetchone() is None
        ]

    files_removed = 0
    notes_root = settings.data_dir / "notes"
    for lecture_id in lecture_ids:
        path = _safe_child(notes_root, lecture_id, _LEC_ID, ".md")
        if path and path.is_file():
            path.unlink(missing_ok=True)
            files_removed += 1
    cache_root = settings.cache_dir / "ingest"
    for digest_dir in orphaned:
        path = _safe_child(cache_root, digest_dir, _HASH_DIR)
        if path and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            files_removed += 1

    request.session.clear()
    return {
        "status": "deleted",
        "lectures_removed": len(lecture_ids),
        "files_removed": files_removed,
    }
