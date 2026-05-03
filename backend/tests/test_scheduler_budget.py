"""Budget gate: decision loop skips ticks once today's LLM spend >= ceiling."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, LlmUsageRow
from app.scheduler.budget import budget_exceeded, today_cost_usd
from app.scheduler.loop import TradingLoop


async def _engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine


@pytest.mark.asyncio
async def test_today_cost_sums_today_rows():
    engine = await _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            LlmUsageRow(
                provider="p",
                model="m",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                cost_usd=0.42,
            )
        )
        await s.commit()

    assert await today_cost_usd(factory) == pytest.approx(0.42)
    await engine.dispose()


@pytest.mark.asyncio
async def test_budget_exceeded_true_and_false():
    engine = await _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            LlmUsageRow(
                provider="p",
                model="m",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost_usd=1.25,
            )
        )
        await s.commit()

    over, spent = await budget_exceeded(factory, ceiling_usd=1.0)
    assert over is True
    assert spent == pytest.approx(1.25)

    under, spent2 = await budget_exceeded(factory, ceiling_usd=5.0)
    assert under is False
    assert spent2 == pytest.approx(1.25)
    await engine.dispose()


@pytest.mark.asyncio
async def test_trading_loop_skips_tick_when_over_budget():
    engine = await _engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(
            LlmUsageRow(
                provider="p",
                model="m",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost_usd=2.0,
            )
        )
        await s.commit()

    # Strategy would raise if called; we're asserting it's NOT called.
    strategy = AsyncMock()
    strategy.decide.side_effect = AssertionError("strategy must not be called")

    loop = TradingLoop(
        broker=AsyncMock(),  # unused — decide is short-circuited
        strategy=strategy,
        risk_engine=AsyncMock(),
        session_factory=factory,
        daily_llm_budget_usd=1.0,
    )
    result = await loop.tick()
    assert result is None
    strategy.decide.assert_not_called()
    await engine.dispose()
