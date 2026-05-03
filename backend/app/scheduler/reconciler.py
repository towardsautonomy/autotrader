"""Bracket-order reconciler.

``TradingLoop`` prefers Alpaca broker-side BRACKET orders so a sudden
gap can fire the stop at the exchange even if our 30s ``RuntimeMonitor``
tick hasn't run yet. The downside: when Alpaca executes the stop or
take-profit child leg, our DB ``Trade`` row stays OPEN — we never see
the fill because nothing in our code submitted a close.

This loop polls each OPEN trade with a ``broker_order_id`` and asks the
broker whether a child leg has filled. If yes, it marks the row CLOSED
with the real fill price and the sign-aware realized pnl, exactly like
``RuntimeMonitor`` would on an internally-triggered close.

Runs under the per-market lock so it can't race with TradingLoop,
RuntimeMonitor, PositionReviewLoop, or the API close paths.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import EventSeverity, get_bus
from app.brokers import BrokerAdapter
from app.models import Trade, TradeStatus, utc_now
from app.risk import load_active_paper_cost_bps, realized_pnl_usd

from .locks import get_lock
from .snapshot import agents_paused

logger = logging.getLogger(__name__)


class BracketReconciler:
    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        session_factory: async_sessionmaker,
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory

    @property
    def market_label(self) -> str:
        return self._broker.market.value

    async def tick(self) -> int:
        """Return the number of trades reconciled this tick."""
        if await agents_paused(self._session_factory):
            return 0

        lock = get_lock(self._broker.market.value)
        async with lock:
            return await self._tick_locked()

    async def _tick_locked(self) -> int:
        reconciled = 0
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Trade).where(
                        Trade.status == TradeStatus.OPEN,
                        Trade.market == self._broker.market.value,
                        Trade.broker_order_id.is_not(None),
                    )
                )
            ).scalars().all()
            # Options use MLEG, not BRACKET — filter in Python so JSON
            # NULL vs empty-dict ambiguity across backends isn't a trap.
            open_trades = [t for t in rows if not t.option_json]

            if not open_trades:
                return 0

            paper_cost_bps = await load_active_paper_cost_bps(session)
            bus = get_bus()

            for trade in open_trades:
                try:
                    fill = await self._broker.get_bracket_fill(
                        trade.broker_order_id
                    )
                except Exception:
                    logger.exception(
                        "reconciler: get_bracket_fill raised for trade %d",
                        trade.id,
                    )
                    continue

                if fill is None or trade.entry_price is None:
                    continue

                trade.status = TradeStatus.CLOSED
                trade.closed_at = utc_now()
                trade.exit_price = fill.fill_price
                trade.broker_close_order_id = fill.child_order_id
                trade.realized_pnl_usd = realized_pnl_usd(
                    action=trade.action,
                    size_usd=trade.size_usd,
                    entry_price=trade.entry_price,
                    exit_price=fill.fill_price,
                    paper_mode=self._broker.paper_mode,
                    paper_cost_bps=paper_cost_bps,
                )
                session.add(trade)
                reconciled += 1

                bus.publish(
                    "reconciler.bracket_filled",
                    (
                        f"{trade.symbol} bracket {fill.trigger} filled "
                        f"@ ${fill.fill_price:.2f} "
                        f"(pnl ${trade.realized_pnl_usd:+.2f})"
                    ),
                    severity=(
                        EventSeverity.SUCCESS
                        if fill.trigger == "TAKE_PROFIT"
                        else EventSeverity.WARN
                    ),
                    data={
                        "symbol": trade.symbol,
                        "trigger": fill.trigger,
                        "exit_price": fill.fill_price,
                        "realized_pnl_usd": trade.realized_pnl_usd,
                        "trade_id": trade.id,
                    },
                )

            if reconciled:
                await session.commit()
        return reconciled


__all__ = ["BracketReconciler"]
