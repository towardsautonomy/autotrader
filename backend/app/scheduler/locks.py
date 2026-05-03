"""Per-broker tick locks.

Three loops can touch the same broker's trades inside one process:

- ``TradingLoop`` (every N minutes) — decides new trades, closes
- ``RuntimeMonitor`` (every ~30s) — force-closes on stop/TP
- ``PositionReviewLoop`` (every ~90s) — AI-driven close/tighten

Without coordination, a monitor tick can close Trade X while a loop tick
has already counted X as open in its snapshot — the loop then commits a
new open that blows the concurrent-positions cap. The kill-switch API
and close-all endpoint add two more race paths.

APScheduler's ``max_instances=1`` prevents same-job overlap but doesn't
coordinate across jobs. We add a process-wide ``asyncio.Lock`` per
market value (``"stocks"``, ``"polymarket"``) that every path
acquires before reading snapshots or modifying trades. Locks are cheap
and the workloads are light, so this is preferred over finer-grained
row locking (SQLite's row locking is awkward anyway).
"""

from __future__ import annotations

import asyncio

_locks: dict[str, asyncio.Lock] = {}


def get_lock(market: str) -> asyncio.Lock:
    """Return the (lazily created) process-wide lock for this market."""
    lock = _locks.get(market)
    if lock is None:
        lock = asyncio.Lock()
        _locks[market] = lock
    return lock


def reset_locks() -> None:
    """Test-only: clear the registry between tests."""
    _locks.clear()


__all__ = ["get_lock", "reset_locks"]
