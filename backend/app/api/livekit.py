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

import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

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


class TokenRequest(BaseModel):
    #: The lecture the student is asking about. The room is named after
    #: it, so the agent knows which lecture to ground the answer in.
    lecture_id: str = Field(min_length=1, max_length=64)
    participant_name: str = Field(default="Student", max_length=64)


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
                agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME)]
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
