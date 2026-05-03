"""Scheduler heartbeat — `_safe_call` stamps on success, skips on failure.

`/system/status` reads these stamps so an external watchdog can alert when
a loop silently stops ticking. A failing tick must NOT stamp — otherwise
the watchdog can't tell "alive but erroring" from "healthy".
"""

from __future__ import annotations

import pytest

from app.scheduler import heartbeat
from app.scheduler.runner import _safe_call


@pytest.fixture(autouse=True)
def _clear_heartbeats():
    heartbeat.reset()
    yield
    heartbeat.reset()


async def test_successful_tick_stamps_heartbeat():
    async def noop():
        return None

    await _safe_call(noop, "loop[stocks]")()
    stamps = heartbeat.snapshot()
    assert "loop[stocks]" in stamps


async def test_failing_tick_does_not_stamp_heartbeat():
    async def boom():
        raise RuntimeError("simulated tick failure")

    await _safe_call(boom, "loop[stocks]")()
    assert heartbeat.snapshot() == {}


async def test_labels_are_independent():
    async def noop():
        return None

    async def boom():
        raise RuntimeError("x")

    await _safe_call(noop, "loop[stocks]")()
    await _safe_call(boom, "monitor[stocks]")()

    stamps = heartbeat.snapshot()
    assert "loop[stocks]" in stamps
    assert "monitor[stocks]" not in stamps
