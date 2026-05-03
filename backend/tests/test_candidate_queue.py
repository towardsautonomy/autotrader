"""CandidateQueue: TTL, overflow, and dedupe semantics."""

from __future__ import annotations

import asyncio

import pytest

from app.scheduler.candidate_queue import CandidateQueue, ScoutCandidate


@pytest.mark.asyncio
async def test_push_peek_and_dedupe_by_symbol():
    q = CandidateQueue()
    await q.push(ScoutCandidate(symbol="nvda", source="movers"))
    await q.push(ScoutCandidate(symbol="NVDA", source="screener", note="hot"))
    items = await q.peek()
    assert len(items) == 1
    assert items[0].symbol == "NVDA"
    assert items[0].note == "hot"


@pytest.mark.asyncio
async def test_push_many_and_drain_order_is_newest_first():
    q = CandidateQueue()
    await q.push(ScoutCandidate(symbol="AAA", source="a"))
    await asyncio.sleep(0.01)
    await q.push(ScoutCandidate(symbol="BBB", source="b"))
    items = await q.peek()
    assert [c.symbol for c in items] == ["BBB", "AAA"]

    drained = await q.drain()
    assert [c.symbol for c in drained] == ["BBB", "AAA"]
    assert await q.size() == 0


@pytest.mark.asyncio
async def test_max_size_evicts_oldest():
    from time import time

    q = CandidateQueue(max_size=2, ttl_sec=3600)
    now = time()
    await q.push(ScoutCandidate(symbol="A", source="s", added_at=now - 2))
    await q.push(ScoutCandidate(symbol="B", source="s", added_at=now - 1))
    await q.push(ScoutCandidate(symbol="C", source="s", added_at=now))
    syms = {c.symbol for c in await q.peek()}
    assert syms == {"B", "C"}


@pytest.mark.asyncio
async def test_ttl_eviction():
    q = CandidateQueue(ttl_sec=0.05)
    await q.push(ScoutCandidate(symbol="OLD", source="s"))
    await asyncio.sleep(0.08)
    items = await q.peek()
    assert items == []
