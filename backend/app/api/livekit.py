"""LiveKit room tokens.

The browser needs a token to join the room. Minting it server-side keeps
the LiveKit API secret out of the client, and lets the server decide
what a participant is allowed to do.

Each mint names a **fresh** room -- ``lecture-<lecture_id>-<session>``
-- and the token carries the room configuration that dispatches the
tutor agent into it. LiveKit only honours a token's ``roomConfig`` when
the room is *created*, so a unique room per session is what makes the
dispatch fire on every join: a lingering room from an earlier session
would swallow the dispatch and leave the student alone in the room.

The name still derives from the lecture id, so the agent knows which
lecture to ground its answers in, without a shared secret in the URL.
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import db as db_mod
from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api/livekit", tags=["livekit"])

#: How long a minted token stays valid.
TOKEN_TTL_SECONDS = 60 * 60

#: The agent the token dispatches into the room.
#:
#: Must match the name the worker registers under (``agent_name`` in
#: ``agent/worker.py``, sourced from ``agent/tutor.py``); a mismatch
#: means the room opens without a tutor. A test pins the two together.
AGENT_NAME = "lecture-tutor"

#: The single-language codes the Gnani streaming STT accepts as ``lang_code``.
#:
#: Probed live (`scripts/probe_lang_detect.py`): every code is honoured as an
#: instruction, not a detector -- `en-IN` transliterates everything to Latin,
#: `hi-IN` everything to Devanagari. A comma list or `auto` is rejected by the
#: server with WebSocket close 1008, so there is no auto-detect to lean on and
#: the session language must be chosen. Keep in sync with
#: `GnaniSTTLanguages` in `.venv/.../livekit/plugins/gnani/models.py`.
SUPPORTED_LANGUAGES = frozenset(
    {
        "bn-IN",
        "en-IN",
        "gu-IN",
        "hi-IN",
        "kn-IN",
        "ml-IN",
        "mr-IN",
        "pa-IN",
        "ta-IN",
        "te-IN",
    }
)


class TokenRequest(BaseModel):
    #: The lecture the student is asking about. The room is named after
    #: it, so the agent knows which lecture to ground the answer in.
    lecture_id: str = Field(min_length=1, max_length=64)
    participant_name: str = Field(default="Student", max_length=64)
    #: The language the student will speak this session, e.g. ``hi-IN``.
    #: Optional: when omitted the lecture's own language is used. Must be
    #: one of the single codes Gnani accepts -- anything else would be a
    #: server-side 1008 on the streaming socket, so it is refused here
    #: with a 422 instead.
    language: str | None = Field(default=None, max_length=16)


@router.post("/token")
async def mint_token(request: TokenRequest) -> dict[str, Any]:
    """Mint a room token for the student."""
    settings = get_settings()
    if not settings.has_livekit():
        raise HTTPException(
            status_code=503, detail="LiveKit is not configured on this server."
        )

    from datetime import timedelta

    from livekit import api

    language = resolve_language(request)
    room = room_name_for(request.lecture_id)
    try:
        at = api.AccessToken(
            settings.livekit_api_key, settings.livekit_api_secret
        )
        at = at.with_identity(request.participant_name or "Student")
        at = at.with_name(request.participant_name or "Student")
        at = at.with_grants(
            api.VideoGrants(
                room_join=True,
                room=room,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        # Dispatch the tutor into the room. This is read when the room is
        # created -- by this very join, because the room name is fresh --
        # so the agent arrives with the student rather than early.
        at = at.with_room_config(
            api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)],
                metadata=json.dumps({"language": language}),
            )
        )
        at = at.with_ttl(timedelta(seconds=TOKEN_TTL_SECONDS))
        token = at.to_jwt()
    except Exception as exc:  # noqa: BLE001
        log.error("could not mint a LiveKit token", extra={"error": str(exc)[:200]})
        raise HTTPException(
            status_code=500, detail="Could not mint a LiveKit token."
        ) from exc

    return {
        "token": token,
        "url": settings.livekit_url,
        "room": room,
        "expires_at": time.time() + TOKEN_TTL_SECONDS,
    }


def resolve_language(request: TokenRequest) -> str:
    """The session language: the request's choice, else the lecture's.

    A language outside :data:`SUPPORTED_LANGUAGES` is a 422 rather than a
    silent default -- Gnani closes the streaming socket with 1008 for an
    unknown ``lang_code``, which would fail mid-session with a far worse
    error than a rejected request.
    """
    if request.language is not None:
        if request.language not in SUPPORTED_LANGUAGES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unsupported language {request.language!r}; "
                    f"use one of {sorted(SUPPORTED_LANGUAGES)}"
                ),
            )
        return request.language
    with db_mod.get_conn() as conn:
        row = conn.execute(
            "SELECT language_code FROM lectures WHERE id=?",
            (request.lecture_id,),
        ).fetchone()
    if row is not None and row["language_code"] in SUPPORTED_LANGUAGES:
        return str(row["language_code"])
    return "en-IN"


def room_name_for(lecture_id: str) -> str:
    """A fresh room name for one voice session.

    The lecture id is already filesystem- and URL-safe, so it is used
    directly; the random session suffix keeps the name unique. Uniqueness
    is the point: LiveKit acts on a token's ``roomConfig`` only when the
    room is created, so the room must not exist yet when the student
    joins -- otherwise a room left over from an earlier session would
    swallow the agent dispatch.
    """
    return f"lecture-{lecture_id}-{secrets.token_hex(4)}"
