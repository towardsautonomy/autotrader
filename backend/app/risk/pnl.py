"""Realized P&L computation — one source of truth for every close path.

Four places used to compute ``realized_pnl_usd`` with the same long-only
formula and the same ``exit/entry - 1`` bug for shorts: the runtime
monitor, the trading loop's close branch, the manual-close API route,
and the position-review agent. This helper centralizes:

- Sign-aware direction (long profits when price rises; short profits
  when price falls).
- Optional simulated round-trip cost on paper trades (spread + slippage),
  so paper analytics reflect what real fills would cost.

Callers pass ``action`` as either a TradeAction enum or the raw string
persisted on the Trade row (``"open_long"`` / ``"open_short"``).
"""

from __future__ import annotations

from .types import TradeAction


def realized_pnl_usd(
    *,
    action: str | TradeAction,
    size_usd: float,
    entry_price: float,
    exit_price: float,
    paper_mode: bool = False,
    paper_cost_bps: float = 0.0,
) -> float:
    """Return the $ P&L of closing ``size_usd`` at ``exit_price``.

    For shorts, the raw ``(exit/entry - 1)`` ratio is flipped so a
    rising price is a loss. When ``paper_mode`` is set, a one-sided
    simulated cost of ``paper_cost_bps`` is subtracted — applied once
    per close (the entry-side cost is implicit in that subtraction, so
    this is the *round-trip* cost on the notional).
    """
    if entry_price <= 0:
        return 0.0
    is_short = str(action) == TradeAction.OPEN_SHORT.value
    raw = (exit_price / entry_price) - 1.0
    pct = -raw if is_short else raw
    gross = size_usd * pct
    if paper_mode and paper_cost_bps > 0:
        gross -= size_usd * (paper_cost_bps / 10000.0)
    return gross


async def load_active_paper_cost_bps(session) -> float:
    """Look up the active RiskConfig row's paper_cost_bps.

    Returns 0.0 when no active row exists (e.g. in tests that don't
    seed one), so calling this is always safe — it just degrades to
    "no simulated cost".
    """
    from sqlalchemy import desc, select

    from app.models import RiskConfigRow

    row = (
        await session.execute(
            select(RiskConfigRow)
            .where(RiskConfigRow.is_active.is_(True))
            .order_by(desc(RiskConfigRow.id))
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return 0.0
    return float(row.paper_cost_bps or 0.0)


__all__ = ["realized_pnl_usd", "load_active_paper_cost_bps"]
