"""Process-global runtime handles.

These are set by `main._build_scheduler` after the scheduler wires up its
components, and read by API routes that need to peek at live in-memory
state (scout queue, etc.). Keeps routes decoupled from the scheduler
module so importing one doesn't pull the other.
"""

from __future__ import annotations

from app.scheduler.candidate_queue import CandidateQueue

_candidate_queue: CandidateQueue | None = None


def set_candidate_queue(queue: CandidateQueue | None) -> None:
    global _candidate_queue
    _candidate_queue = queue


def get_candidate_queue() -> CandidateQueue | None:
    return _candidate_queue
