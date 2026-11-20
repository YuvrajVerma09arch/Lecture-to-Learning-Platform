"""A bounded number of simultaneous outbound HTTP calls per process.

The semaphore is created lazily and keyed by the running event loop: settings
are cached but tests build a fresh app per loop, and a semaphore bound to a
closed loop must never be reused. Only the actual request holds a slot —
backoff sleeps, polling waits and long-lived provider sessions do not.
"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .config import get_settings
from .logging_setup import get_logger

log = get_logger(__name__)

_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]] = (
    weakref.WeakKeyDictionary()
)


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limit = get_settings().max_outbound_concurrent
    entry = _SEMAPHORES.get(loop)
    if entry is None or entry[0] != limit:
        entry = (limit, asyncio.Semaphore(limit))
        _SEMAPHORES[loop] = entry
        log.info("outbound concurrency cap", extra={"limit": limit})
    return entry[1]


def reset_outbound_limiter() -> None:
    """Forget all per-loop semaphores (tests only)."""
    _SEMAPHORES.clear()


@asynccontextmanager
async def outbound_permit(provider: str) -> AsyncIterator[None]:
    """Hold one outbound slot for the duration of the wrapped call."""
    semaphore = _semaphore()
    started = time.monotonic()
    if semaphore.locked():
        log.warning(
            "outbound concurrency limit reached",
            extra={"provider": provider, "wait_started": True},
        )
    async with semaphore:
        waited_ms = round((time.monotonic() - started) * 1000)
        if waited_ms > 10:
            log.warning(
                "outbound call waited for a slot",
                extra={"provider": provider, "wait_ms": waited_ms},
            )
        # ``async with semaphore`` releases the slot on normal exit, on
        # exceptions and on cancellation.
        yield
