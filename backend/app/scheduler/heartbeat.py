"""In-memory scheduler heartbeat registry.

``SchedulerRunner`` stamps each loop's label here after every successful
tick. The authenticated ``/system/status`` endpoint reads it to report
which loops are alive and how long since each last ran — an external
watchdog can page on a stale heartbeat when APScheduler has silently
stopped firing (hung provider, swallowed exception upstream of
``_safe_call``, event loop stall, etc.).

Kept in-memory because it's ephemeral boot-life state and the status
endpoint runs in-process. On restart it clears and repopulates.
"""

from __future__ import annotations

from datetime import UTC, datetime

_last_tick: dict[str, datetime] = {}


def mark(label: str) -> None:
    _last_tick[label] = datetime.now(UTC)


def snapshot() -> dict[str, datetime]:
    return dict(_last_tick)


def reset() -> None:
    """Test helper — clear all recorded heartbeats."""
    _last_tick.clear()


__all__ = ["mark", "reset", "snapshot"]
