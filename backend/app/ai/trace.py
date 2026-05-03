"""Per-tick tracing context.

A single TradingLoop.tick() is one "cycle" — the scout has already run
upstream, the orchestrator fans out research agents, and the decision
agent makes the final call. They all share a single ``cycle_id`` that
the UI uses to group their LLM calls under one swarm hierarchy.

Set the ID once in the loop (``set_cycle_id(...)``), and every
``log_usage`` call underneath picks it up via ``contextvars`` without
needing the ID threaded through method signatures. asyncio copies the
current context to spawned tasks, so fan-out via ``gather`` inherits
the cycle automatically.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_cycle_id_var: ContextVar[str | None] = ContextVar("cycle_id", default=None)


def new_cycle_id() -> str:
    """Return a compact, monotonic-ish cycle id: ``cyc-<epoch_ms>-<rand>``."""
    return f"cyc-{int(time.time() * 1000):x}-{uuid.uuid4().hex[:6]}"


def get_cycle_id() -> str | None:
    return _cycle_id_var.get()


def set_cycle_id(cycle_id: str | None) -> None:
    _cycle_id_var.set(cycle_id)


@contextmanager
def cycle_scope(cycle_id: str) -> Iterator[str]:
    token = _cycle_id_var.set(cycle_id)
    try:
        yield cycle_id
    finally:
        _cycle_id_var.reset(token)


__all__ = ["new_cycle_id", "get_cycle_id", "set_cycle_id", "cycle_scope"]
