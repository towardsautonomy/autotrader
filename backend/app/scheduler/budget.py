"""Daily LLM spend gate.

Sums `LlmUsageRow.cost_usd` for the current Pacific-local day and
compares against a configured ceiling. Both the scout loop and the
decision loop call this before issuing any LLM call so we can't blow
past the budget.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clock import pacific_day_bounds_utc
from app.models import LlmUsageRow


async def today_cost_usd(session_factory: async_sessionmaker) -> float:
    """Sum of cost_usd for LLM calls in the current Pacific day."""
    start, end = pacific_day_bounds_utc()
    async with session_factory() as s:
        total = (
            await s.execute(
                select(func.coalesce(func.sum(LlmUsageRow.cost_usd), 0.0)).where(
                    LlmUsageRow.created_at >= start,
                    LlmUsageRow.created_at < end,
                )
            )
        ).scalar_one()
    return float(total or 0.0)


async def budget_exceeded(
    session_factory: async_sessionmaker, *, ceiling_usd: float
) -> tuple[bool, float]:
    """Return (exceeded, spent_today)."""
    if ceiling_usd <= 0:
        return False, 0.0
    spent = await today_cost_usd(session_factory)
    return spent >= ceiling_usd, spent
