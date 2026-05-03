"""Runtime monitor — enforces stop-loss / take-profit on open positions
independently of the strategy. Runs every N seconds while trading is
enabled."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.activity import EventSeverity, get_bus
from app.brokers import BrokerAdapter
from app.models import Trade, TradeStatus, utc_now
from app.risk import TradeAction, load_active_paper_cost_bps, realized_pnl_usd

from .locks import get_lock
from .snapshot import agents_paused

logger = logging.getLogger(__name__)


class RuntimeMonitor:
    def __init__(self, broker: BrokerAdapter, session_factory):
        self.broker = broker
        self.session_factory = session_factory

    async def tick(self) -> int:
        """Check open positions for stop-loss / take-profit triggers.

        Returns the number of positions force-closed this tick.
        """
        if await agents_paused(self.session_factory):
            return 0
        # Serialize with TradingLoop + position-review + API close paths
        # on the same broker so snapshots and trade writes can't interleave.
        lock = get_lock(self.broker.market.value)
        async with lock:
            return await self._tick_locked()

    async def _tick_locked(self) -> int:
        closed = 0
        async with self.session_factory() as session:
            paper_cost_bps = await load_active_paper_cost_bps(session)
            open_trades = (
                await session.execute(
                    select(Trade).where(
                        Trade.status == TradeStatus.OPEN,
                        Trade.market == self.broker.market.value,
                    )
                )
            ).scalars().all()

            for trade in open_trades:
                if trade.entry_price is None:
                    continue
                if trade.option_json:
                    await self._check_option_trade(trade, paper_cost_bps)
                    if trade.status == TradeStatus.CLOSED:
                        session.add(trade)
                        closed += 1
                    continue
                try:
                    current = await self.broker.get_price(trade.symbol)
                except Exception:
                    logger.exception("get_price failed for %s", trade.symbol)
                    continue

                # Sign-aware pnl: longs profit when price rises, shorts
                # profit when price falls. Without this flip a losing
                # short looked like a winner and tripped take-profit at
                # the worst moment.
                is_short = trade.action == TradeAction.OPEN_SHORT
                raw_pct = (current / trade.entry_price) - 1.0
                pnl_pct = -raw_pct if is_short else raw_pct
                hit_stop = (
                    trade.stop_loss_pct is not None
                    and pnl_pct <= -trade.stop_loss_pct
                )
                hit_tp = (
                    trade.take_profit_pct is not None
                    and pnl_pct >= trade.take_profit_pct
                )
                if not (hit_stop or hit_tp):
                    continue

                result = await self.broker.close_position(trade.symbol)
                if not result.success:
                    logger.error(
                        "monitor failed to close %s: %s", trade.symbol, result.error
                    )
                    get_bus().publish(
                        "monitor.close_failed",
                        f"{trade.symbol}: {result.error}",
                        severity=EventSeverity.ERROR,
                        data={"symbol": trade.symbol, "error": result.error},
                    )
                    continue

                exit_price = result.fill_price or current
                trade.status = TradeStatus.CLOSED
                trade.closed_at = utc_now()
                trade.exit_price = exit_price
                trade.broker_close_order_id = result.broker_order_id
                trade.realized_pnl_usd = realized_pnl_usd(
                    action=trade.action,
                    size_usd=trade.size_usd,
                    entry_price=trade.entry_price,
                    exit_price=exit_price,
                    paper_mode=self.broker.paper_mode,
                    paper_cost_bps=paper_cost_bps,
                )
                session.add(trade)
                closed += 1
                trigger = "STOP" if hit_stop else "TAKE_PROFIT"
                logger.info(
                    "force-closed %s at %.4f (%s)", trade.symbol, exit_price, trigger
                )
                get_bus().publish(
                    "monitor.triggered",
                    f"{trigger} on {trade.symbol} @ ${exit_price:.2f} "
                    f"(pnl ${trade.realized_pnl_usd:+.2f})",
                    severity=(
                        EventSeverity.SUCCESS if hit_tp else EventSeverity.WARN
                    ),
                    data={
                        "symbol": trade.symbol,
                        "trigger": trigger,
                        "exit_price": exit_price,
                        "realized_pnl_usd": trade.realized_pnl_usd,
                    },
                )

            if closed:
                await session.commit()
        return closed

    async def _check_option_trade(self, trade: Trade, paper_cost_bps: float) -> None:
        """Mark an open option trade to market and trip stop/take-profit.

        ``entry_price`` is the net per-contract debit paid at open (positive
        for a long/debit structure). The current combo mark comes from
        ``broker.get_option_mark``. We compute pnl_pct against entry and
        fire ``close_option_position`` when the stop or take-profit
        threshold is breached. Non-debit opens (entry_price <= 0) aren't
        supported — skip them.
        """
        if not trade.option_json:
            return
        if trade.entry_price is None or trade.entry_price <= 0:
            return

        try:
            current = await self.broker.get_option_mark(trade.option_json)
        except Exception:
            logger.exception("get_option_mark failed for %s", trade.symbol)
            return
        if current is None:
            return

        pnl_pct = (current / trade.entry_price) - 1.0
        hit_stop = (
            trade.stop_loss_pct is not None and pnl_pct <= -trade.stop_loss_pct
        )
        hit_tp = (
            trade.take_profit_pct is not None and pnl_pct >= trade.take_profit_pct
        )
        if not (hit_stop or hit_tp):
            return

        result = await self.broker.close_option_position(trade.option_json)
        if not result.success:
            logger.error(
                "monitor failed to close option %s: %s", trade.symbol, result.error
            )
            get_bus().publish(
                "monitor.close_failed",
                f"{trade.symbol} option: {result.error}",
                severity=EventSeverity.ERROR,
                data={"symbol": trade.symbol, "error": result.error},
            )
            return

        exit_price = result.fill_price if result.fill_price is not None else current
        trade.status = TradeStatus.CLOSED
        trade.closed_at = utc_now()
        trade.exit_price = exit_price
        trade.broker_close_order_id = result.broker_order_id
        trade.realized_pnl_usd = realized_pnl_usd(
            action=trade.action,
            size_usd=trade.size_usd,
            entry_price=trade.entry_price,
            exit_price=exit_price,
            paper_mode=self.broker.paper_mode,
            paper_cost_bps=paper_cost_bps,
        )
        trigger = "STOP" if hit_stop else "TAKE_PROFIT"
        logger.info(
            "force-closed option %s at %.4f (%s)",
            trade.symbol,
            exit_price,
            trigger,
        )
        get_bus().publish(
            "monitor.triggered",
            f"{trigger} on {trade.symbol} option @ ${exit_price:.2f} "
            f"(pnl ${trade.realized_pnl_usd:+.2f})",
            severity=(EventSeverity.SUCCESS if hit_tp else EventSeverity.WARN),
            data={
                "symbol": trade.symbol,
                "trigger": trigger,
                "exit_price": exit_price,
                "realized_pnl_usd": trade.realized_pnl_usd,
            },
        )
