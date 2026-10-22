"""LiveKit room tokens.

The browser needs a token to join the room. Minting it server-side keeps
the LiveKit API secret out of the client, and lets the server decide
what a participant is allowed to do.

The token is minted for a room named after the lecture, so a student
and the agent end up in the same place without a shared secret in the
URL.
"""

from __future__ import annotations

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

    try:
        at = api.AccessToken(
            settings.livekit_api_key, settings.livekit_api_secret
        )
        at = at.with_identity(request.participant_name or "Student")
        at = at.with_name(request.participant_name or "Student")
        at = at.with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name_for(request.lecture_id),
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
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
        "room": room_name_for(request.lecture_id),
        "expires_at": time.time() + TOKEN_TTL_SECONDS,
    }


def room_name_for(lecture_id: str) -> str:
    """The room name for a lecture.

    The lecture id is already filesystem- and URL-safe, so it can be
    used directly.
    """
    return f"lecture-{lecture_id}"
