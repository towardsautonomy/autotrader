"""In-memory scout candidate queue.

The scout loop (fast cadence) pushes interesting tickers here; the decision
loop (slower cadence) drains them. Entries expire after a TTL so stale
picks don't keep getting re-evaluated.

Thread-safety: we live inside a single asyncio loop so an `asyncio.Lock`
is enough. Writes are dict-level updates; lookups are O(1).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from time import time


@dataclass
class ScoutCandidate:
    symbol: str
    source: str
    note: str = ""
    score: float | None = None
    added_at: float = field(default_factory=time)

    def age_sec(self) -> float:
        return time() - self.added_at


class CandidateQueue:
    """Bounded, TTL'd set of scout-supplied candidates keyed by symbol.

    Newer pushes for the same symbol overwrite older ones so the note/score
    reflect the latest scout run. No persistence — queue state is runtime-
    only and resets when the process restarts.
    """

    def __init__(self, *, ttl_sec: float = 600.0, max_size: int = 50) -> None:
        self._ttl_sec = ttl_sec
        self._max_size = max_size
        self._items: dict[str, ScoutCandidate] = {}
        self._lock = asyncio.Lock()

    async def push(self, candidate: ScoutCandidate) -> None:
        async with self._lock:
            self._evict_expired_locked()
            self._items[candidate.symbol.upper()] = candidate
            if len(self._items) > self._max_size:
                oldest = min(self._items.values(), key=lambda c: c.added_at)
                self._items.pop(oldest.symbol, None)

    async def push_many(self, candidates: list[ScoutCandidate]) -> None:
        async with self._lock:
            self._evict_expired_locked()
            for c in candidates:
                self._items[c.symbol.upper()] = c
            if len(self._items) > self._max_size:
                overflow = sorted(
                    self._items.values(), key=lambda c: c.added_at
                )[: len(self._items) - self._max_size]
                for c in overflow:
                    self._items.pop(c.symbol, None)

    async def peek(self) -> list[ScoutCandidate]:
        """Return all live candidates without removing them. Ordered
        most-recent first so the decision loop sees the freshest signal."""
        async with self._lock:
            self._evict_expired_locked()
            return sorted(
                self._items.values(), key=lambda c: c.added_at, reverse=True
            )

    async def drain(self) -> list[ScoutCandidate]:
        """Return and remove all live candidates atomically."""
        async with self._lock:
            self._evict_expired_locked()
            live = list(self._items.values())
            self._items.clear()
            return sorted(live, key=lambda c: c.added_at, reverse=True)

    async def size(self) -> int:
        async with self._lock:
            self._evict_expired_locked()
            return len(self._items)

    def _evict_expired_locked(self) -> None:
        now = time()
        for sym, c in list(self._items.items()):
            if now - c.added_at > self._ttl_sec:
                self._items.pop(sym, None)
