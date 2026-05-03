"""In-memory pub/sub for live activity events.

The scheduler, risk engine, broker adapters, and market-data layer publish
events here; the FastAPI /events SSE endpoint fans them out to connected
browsers. An optional background task persists each event to the
`activity_events` table so the frontend can backfill on reconnect.

The bus is process-local (single-process APScheduler + uvicorn setup).
Subscribers receive events via asyncio.Queue; slow consumers are dropped
with a warning rather than blocking publishers.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class EventSeverity(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"
    SUCCESS = "success"


@dataclass(slots=True)
class ActivityEvent:
    id: int
    ts: str  # ISO-8601 UTC
    type: str
    severity: EventSeverity
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_sse(self) -> str:
        payload = {
            "id": self.id,
            "ts": self.ts,
            "type": self.type,
            "severity": self.severity.value,
            "message": self.message,
            "data": self.data,
        }
        return f"data: {json.dumps(payload, default=str)}\n\n"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


class ActivityBus:
    """Async fan-out bus. Subscribers get their own bounded queue."""

    def __init__(self, *, queue_max: int = 500) -> None:
        self._subscribers: set[asyncio.Queue[ActivityEvent]] = set()
        self._queue_max = queue_max
        self._counter = 0
        self._lock = asyncio.Lock()
        self._persist_hook = None

    def set_persist_hook(self, hook) -> None:
        """Hook is called with ActivityEvent; exceptions are swallowed.

        Set from main.py lifespan so the bus stays framework-agnostic.
        """
        self._persist_hook = hook

    def subscribe(self) -> asyncio.Queue[ActivityEvent]:
        q: asyncio.Queue[ActivityEvent] = asyncio.Queue(maxsize=self._queue_max)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[ActivityEvent]) -> None:
        self._subscribers.discard(q)

    def publish(
        self,
        type: str,
        message: str,
        *,
        severity: EventSeverity = EventSeverity.INFO,
        data: dict[str, Any] | None = None,
    ) -> ActivityEvent:
        self._counter += 1
        event = ActivityEvent(
            id=self._counter,
            ts=datetime.now(UTC).isoformat(),
            type=type,
            severity=severity,
            message=message,
            data=data or {},
        )

        dropped = 0
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dropped += 1
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    self._subscribers.discard(q)

        if dropped:
            logger.warning("activity bus: %d slow subscribers dropped oldest", dropped)

        if self._persist_hook is not None:
            try:
                self._persist_hook(event)
            except Exception:
                logger.exception("activity persist hook failed")

        return event


_bus: ActivityBus | None = None


def get_bus() -> ActivityBus:
    global _bus
    if _bus is None:
        _bus = ActivityBus()
    return _bus
