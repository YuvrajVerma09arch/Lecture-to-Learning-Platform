"""The shared outbound-concurrency cap."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import config as config_mod
from app import outbound


@pytest.fixture(autouse=True)
def clean_limiter(monkeypatch: Any) -> Any:
    outbound.reset_outbound_limiter()
    yield
    outbound.reset_outbound_limiter()


def _limit(monkeypatch: Any, value: str) -> None:
    monkeypatch.setenv("MAX_OUTBOUND_CONCURRENT", value)
    config_mod.reset_settings_cache()
    outbound.reset_outbound_limiter()


async def test_no_more_than_the_configured_number_of_calls_run_at_once(monkeypatch: Any):
    _limit(monkeypatch, "2")
    active = 0
    peak = 0

    async def call() -> None:
        nonlocal active, peak
        async with outbound.outbound_permit("test"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*(call() for _ in range(8)))
    assert peak == 2
    assert active == 0


async def test_a_failure_releases_the_slot(monkeypatch: Any):
    _limit(monkeypatch, "1")

    async def failing() -> None:
        async with outbound.outbound_permit("test"):
            raise RuntimeError("provider failed")

    with pytest.raises(RuntimeError, match="provider failed"):
        await failing()

    # A second call would hang forever if the first had leaked its permit.
    await asyncio.wait_for(
        _one_call(), timeout=1.0
    )


async def _one_call() -> None:
    async with outbound.outbound_permit("test"):
        return


async def test_changing_the_limit_takes_effect(monkeypatch: Any):
    _limit(monkeypatch, "1")
    assert outbound._semaphore()._value == 1
    _limit(monkeypatch, "3")
    assert outbound._semaphore()._value == 3
