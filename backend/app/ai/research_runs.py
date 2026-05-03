"""In-process registry for detached research agent runs.

The researcher's ``agent.stream()`` is the expensive part — a single run can
take minutes and cost real dollars. We don't want it to cancel when the
browser tab backgrounds, the laptop sleeps, or the SSE socket drops from a
flaky network. This module lets the run live in a background task while
one or more SSE consumers subscribe to its event stream.

Design:

- A ``ResearchRun`` owns one ``asyncio.Task`` that iterates the agent's
  event stream. Events are (1) appended to a bounded history list and
  (2) fanned out to every subscribed listener queue.
- Subscribers call ``subscribe()`` to get an async iterator that first
  replays the history (from an optional ``after_seq`` cursor) and then
  tails live events until the run completes.
- The registry is keyed by ``conversation_id`` so only one run per thread
  can be active at a time. Starting a second run for the same thread
  cancels the first — protects against accidental double-submits while
  still letting follow-up messages work once the prior run finishes.

This is intentionally in-process (not Redis / a queue). Works fine for
one-box deployments. If the app is ever clustered, swap the registry for
a broker — the API surface here (start / get / subscribe) is small.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from app.ai.researcher import ResearcherAgent, ResearchEvent

logger = logging.getLogger(__name__)


# Upper bound on the per-run history list. A long research loop emits
# on the order of 60–200 events; 2000 is plenty for a single run and
# still caps memory at ~1 MB even if payloads are fat.
_MAX_HISTORY = 2000


@dataclass
class _SeqEvent:
    seq: int
    event: ResearchEvent


@dataclass
class ResearchRun:
    conversation_id: int
    title: str
    started_at: float
    history: list[_SeqEvent] = field(default_factory=list)
    listeners: set[asyncio.Queue[_SeqEvent | None]] = field(default_factory=set)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    task: asyncio.Task | None = None

    def next_seq(self) -> int:
        return (self.history[-1].seq + 1) if self.history else 1

    def record(self, event: ResearchEvent) -> _SeqEvent:
        se = _SeqEvent(seq=self.next_seq(), event=event)
        self.history.append(se)
        if len(self.history) > _MAX_HISTORY:
            # Drop the oldest ~10% at a time to avoid list-copy churn.
            drop = len(self.history) // 10
            del self.history[:drop]
        # Fan out to every active listener. put_nowait is safe because
        # the queues are unbounded — back-pressure is not our problem at
        # these event rates (tens per minute).
        dead: list[asyncio.Queue] = []
        for q in self.listeners:
            try:
                q.put_nowait(se)
            except Exception:
                dead.append(q)
        for q in dead:
            self.listeners.discard(q)
        return se

    def close(self, error: str | None = None) -> None:
        self.error = error
        self.done.set()
        # Signal end-of-stream to every listener with a None sentinel.
        for q in list(self.listeners):
            with contextlib.suppress(Exception):
                q.put_nowait(None)


_RUNS: dict[int, ResearchRun] = {}
_RUNS_LOCK = asyncio.Lock()


def get_run(conversation_id: int) -> ResearchRun | None:
    """Return the current run for a conversation, or None if none active.

    "Active" includes runs that have finished but whose ResearchRun object
    still lives in the registry — callers can decide based on ``done``
    whether to tail live events or just replay history.
    """
    return _RUNS.get(conversation_id)


async def start_run(
    *,
    agent: ResearcherAgent,
    conversation_id: int,
    title: str,
    prior_messages: list[dict[str, Any]],
    user_message: str,
) -> ResearchRun:
    """Start a background research run for a conversation.

    If a run is already active for the same conversation_id, it is
    cancelled first — treats the new submission as an override, which
    matches user intent (they sent a new message, they want the new
    answer, not the stale in-flight one).
    """
    loop = asyncio.get_running_loop()
    async with _RUNS_LOCK:
        existing = _RUNS.get(conversation_id)
        if existing is not None and not existing.done.is_set():
            logger.info(
                "cancelling existing research run for conv_id=%s",
                conversation_id,
            )
            if existing.task is not None:
                existing.task.cancel()
            # Wait briefly for the prior task to unwind so its final
            # events (if any) don't bleed into the new run's history.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(existing.done.wait(), timeout=2.0)

        run = ResearchRun(
            conversation_id=conversation_id,
            title=title,
            started_at=loop.time(),
        )
        _RUNS[conversation_id] = run

    async def _driver() -> None:
        try:
            # Emit a synthetic conversation event first so reconnecting
            # clients see the thread identity without hitting the DB.
            run.record(
                ResearchEvent(
                    type="conversation",
                    data={"id": conversation_id, "title": title},
                )
            )
            async for event in agent.stream(
                conversation_id=conversation_id,
                prior_messages=prior_messages,
                user_message=user_message,
            ):
                run.record(event)
        except asyncio.CancelledError:
            run.record(
                ResearchEvent(
                    type="error",
                    data={"error": "cancelled"},
                )
            )
            run.close(error="cancelled")
            raise
        except Exception as exc:
            logger.exception("research run crashed conv_id=%s", conversation_id)
            run.record(
                ResearchEvent(
                    type="error",
                    data={"error": f"{type(exc).__name__}: {exc}"},
                )
            )
            run.close(error=str(exc))
        else:
            run.close()

    run.task = asyncio.create_task(
        _driver(), name=f"research-run-{conversation_id}"
    )
    return run


async def subscribe(
    run: ResearchRun,
    *,
    after_seq: int = 0,
) -> AsyncIterator[_SeqEvent]:
    """Yield history (after ``after_seq``) then tail live events.

    The caller owns the async generator's lifecycle: breaking out of the
    loop (client disconnect, cancelled) deregisters the listener but
    leaves the underlying run running.
    """
    # Replay history first. Copy the slice to avoid racing with record()
    # mutating the list while we iterate.
    history_snapshot = [se for se in run.history if se.seq > after_seq]
    for se in history_snapshot:
        yield se

    if run.done.is_set():
        return

    # Subscribe for live events. Use an unbounded queue so the driver
    # never blocks on a slow consumer — if the socket backs up, the queue
    # grows rather than stalling the agent.
    q: asyncio.Queue[_SeqEvent | None] = asyncio.Queue()
    run.listeners.add(q)

    # Between the history snapshot and adding ourselves as a listener,
    # new events may have been recorded. Catch up from the live history
    # list before yielding from the queue.
    last_seq = history_snapshot[-1].seq if history_snapshot else after_seq
    catchup = [se for se in run.history if se.seq > last_seq]
    try:
        for se in catchup:
            yield se
            last_seq = se.seq
        while True:
            item = await q.get()
            if item is None:
                return
            if item.seq <= last_seq:
                # Already delivered via catch-up.
                continue
            last_seq = item.seq
            yield item
    finally:
        run.listeners.discard(q)


__all__ = [
    "ResearchRun",
    "get_run",
    "start_run",
    "subscribe",
]
