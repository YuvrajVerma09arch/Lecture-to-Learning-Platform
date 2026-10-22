"""Health and readiness.

The two are deliberately different:

* ``/api/health`` answers "is this process alive". It must not depend
  on anything external, or a provider outage would take the whole app
  down and the orchestrator would restart it.
* ``/api/ready`` answers "can this process do useful work". It reports
  the state of the external dependencies *as data*, not as a failure: a
  Gnani outage leaves the app ready to serve cached notes and to
  accept a text question, so it must not report "not ready".
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter

from .. import db as db_mod
from ..config import get_settings
from ..logging_setup import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness. Deliberately does not touch any external service."""
    return {"status": "ok", "time": time.time()}


@router.get("/ready")
async def ready() -> dict[str, Any]:
    """Readiness, with the state of each dependency reported as data."""
    settings = get_settings()
    checks: dict[str, Any] = {}

    # The database must be writable; without it nothing can be served.
    try:
        with db_mod.get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "error": str(exc)[:200]}

    # The providers are reported, but their absence does not make the
    # app unready: cached notes and text chat still work.
    checks["gnani"] = {"ok": settings.has_gnani(), "required_for": "transcription"}
    checks["google"] = {"ok": settings.has_google(), "required_for": "notes generation"}
    checks["groq"] = {"ok": settings.has_groq(), "required_for": "conversational answers"}
    checks["livekit"] = {"ok": settings.has_livekit(), "required_for": "voice chat"}

    core_ok = bool(checks["database"]["ok"])
    return {
        "status": "ready" if core_ok else "not-ready",
        "checks": checks,
        "time": time.time(),
    }
