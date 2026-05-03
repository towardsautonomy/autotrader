"""PendingReconciler — reconcile broker fills asynchronously.

Two symmetric concerns:

1. **Open-fill reconciliation.** ``TradingLoop`` writes a Trade row as
   ``PENDING`` when the broker accepts an order without returning a fill
   price (Alpaca does this for orders submitted outside regular hours —
   they queue and fill at the next open). Without a promoter those fills
   never become tracked OPEN positions: stops aren't enforced, and the
   daily trade count undercounts them.

2. **Close-fill reconciliation.** ``broker.close_position`` returns the
   just-submitted close order whose ``filled_avg_price`` is ``None`` at
   that instant. Callers (position-review, safety monitor, API close)
   stamp the row as ``CLOSED`` but leave ``exit_price`` null and
   ``realized_pnl_usd`` at 0. Without a reconciler the fill price is
   lost forever and the UI shows phantom closed-with-zero-P/L rows.

The loop polls ``broker.get_order_fill`` per outstanding row and:

* Open pending + ``filled``  → ``status=OPEN``, ``entry_price``, ``opened_at=now``
* Open pending + ``canceled`` / ``rejected`` → ``status=CANCELED``, ``closed_at=now``
* Closed-unfilled + ``filled``  → set ``exit_price`` + recompute
  ``realized_pnl_usd``

Runs under the per-market lock so it can't race with TradingLoop,
RuntimeMonitor, BracketReconciler, PositionReviewLoop, or API close
paths.
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


class PendingReconciler:
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
        """Return the number of PENDING trades transitioned this tick."""
        if await agents_paused(self._session_factory):
            return 0

        lock = get_lock(self._broker.market.value)
        async with lock:
            return await self._tick_locked()

    async def _tick_locked(self) -> int:
        transitioned = 0
        async with self._session_factory() as session:
            transitioned += await self._reconcile_opens(session)
            transitioned += await self._reconcile_closes(session)
            if transitioned:
                await session.commit()
        return transitioned

    async def _reconcile_opens(self, session) -> int:
        transitioned = 0
        rows = (
            await session.execute(
                select(Trade).where(
                    Trade.status == TradeStatus.PENDING,
                    Trade.market == self._broker.market.value,
                    Trade.broker_order_id.is_not(None),
                )
            )
        ).scalars().all()

        if not rows:
            return 0

        bus = get_bus()

        for trade in rows:
            try:
                fill = await self._broker.get_order_fill(trade.broker_order_id)
            except Exception:
                logger.exception(
                    "pending reconciler: get_order_fill raised for trade %d",
                    trade.id,
                )
                continue

            if fill is None or fill.status == "pending":
                continue

            if fill.status == "filled" and fill.fill_price is not None:
                trade.status = TradeStatus.OPEN
                trade.entry_price = float(fill.fill_price)
                trade.opened_at = utc_now()
                session.add(trade)
                transitioned += 1
                bus.publish(
                    "reconciler.pending_filled",
                    (
                        f"{trade.symbol} pending → open "
                        f"@ ${fill.fill_price:.2f}"
                    ),
                    severity=EventSeverity.SUCCESS,
                    data={
                        "symbol": trade.symbol,
                        "entry_price": fill.fill_price,
                        "trade_id": trade.id,
                    },
                )
                continue

            if fill.status in {"canceled", "rejected"}:
                trade.status = TradeStatus.CANCELED
                trade.closed_at = utc_now()
                session.add(trade)
                transitioned += 1
                bus.publish(
                    "reconciler.pending_canceled",
                    f"{trade.symbol} pending → canceled ({fill.status})",
                    severity=EventSeverity.WARN,
                    data={
                        "symbol": trade.symbol,
                        "trade_id": trade.id,
                        "reason": fill.status,
                    },
                )
        return transitioned

    async def _reconcile_closes(self, session) -> int:
        """Back-fill ``exit_price`` / ``realized_pnl_usd`` for closes.

        ``broker.close_position`` returns the freshly-submitted close
        order whose ``filled_avg_price`` is ``None`` at submission; the
        caller stamps the row as CLOSED but the fill price is not yet
        known. We poll the broker and patch the row once the fill lands.
        Option trades are skipped — those marks come from combo snapshots
        which the option monitor path already handles in-flow.
        """
        # NB: ``Trade.option_json.is_(None)`` looks right but doesn't
        # match — the column is SQLAlchemy JSON, so Python ``None`` is
        # serialized as the JSON string ``"null"`` (text, not SQL NULL).
        # Filter at the Python level instead.
        rows = (
            await session.execute(
                select(Trade).where(
                    Trade.status == TradeStatus.CLOSED,
                    Trade.market == self._broker.market.value,
                    Trade.broker_close_order_id.is_not(None),
                    Trade.exit_price.is_(None),
                )
            )
        ).scalars().all()
        rows = [r for r in rows if not r.option_json]

        if not rows:
            return 0

        bus = get_bus()
        paper_cost_bps = await load_active_paper_cost_bps(session)
        transitioned = 0

        for trade in rows:
            try:
                fill = await self._broker.get_order_fill(
                    trade.broker_close_order_id
                )
            except Exception:
                logger.exception(
                    "close reconciler: get_order_fill raised for trade %d",
                    trade.id,
                )
                continue

            if fill is None or fill.status == "pending":
                continue

            if fill.status != "filled" or fill.fill_price is None:
                # A canceled/rejected close would leave the row mis-stated
                # as CLOSED; surface the anomaly but don't auto-revert.
                logger.warning(
                    "close reconciler: trade %d close order %s ended as %s; "
                    "row stays CLOSED with null exit",
                    trade.id,
                    trade.broker_close_order_id,
                    fill.status,
                )
                continue

            exit_price = float(fill.fill_price)
            trade.exit_price = exit_price
            if trade.entry_price:
                trade.realized_pnl_usd = realized_pnl_usd(
                    action=trade.action,
                    size_usd=trade.size_usd,
                    entry_price=trade.entry_price,
                    exit_price=exit_price,
                    paper_mode=self._broker.paper_mode,
                    paper_cost_bps=paper_cost_bps,
                )
            session.add(trade)
            transitioned += 1
            bus.publish(
                "reconciler.close_filled",
                (
                    f"{trade.symbol} close filled @ ${exit_price:.2f} "
                    f"(pnl ${(trade.realized_pnl_usd or 0.0):+.2f})"
                ),
                severity=EventSeverity.SUCCESS,
                data={
                    "symbol": trade.symbol,
                    "exit_price": exit_price,
                    "realized_pnl_usd": trade.realized_pnl_usd,
                    "trade_id": trade.id,
                },
            )

        return transitioned


__all__ = ["PendingReconciler"]
