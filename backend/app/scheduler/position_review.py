"""Position-review loop — fast-cadence exit scanner.

Separate from the main TradingLoop (5-min) and the RuntimeMonitor (rule-
based, 30s). This loop runs every N seconds, gathers fresh news +
quotes for every open position, fires one LLM round with parallel
review tool calls, and acts on the decisions:

  · close         → submits a broker close + marks Trade CLOSED
  · tighten_stop  → updates Trade.stop_loss_pct (RuntimeMonitor picks up)
  · hold          → no-op

The whole loop skips cleanly when agents are paused, when the market is
closed (opt-in flag), or when the daily LLM budget is exhausted.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import EventSeverity, get_bus
from app.ai.position_review_agent import (
    PositionContext,
    PositionReviewAgent,
    ReviewDecision,
)
from app.ai.trace import cycle_scope, new_cycle_id
from app.brokers import BrokerAdapter
from app.clock import is_us_equities_regular_session
from app.market_data import FinnhubClient
from app.models import Trade, TradeStatus, utc_now
from app.risk import load_active_paper_cost_bps, realized_pnl_usd

from .budget import budget_exceeded
from .locks import get_lock
from .snapshot import agents_paused, pause_when_market_closed

logger = logging.getLogger(__name__)


class PositionReviewLoop:
    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        agent: PositionReviewAgent,
        session_factory: async_sessionmaker,
        news_client: FinnhubClient | None = None,
        daily_llm_budget_usd: float = 0.0,
        respect_market_hours: bool = True,
        market_label: str = "stocks",
    ) -> None:
        self._broker = broker
        self._agent = agent
        self._session_factory = session_factory
        self._news = news_client
        self._budget_ceiling = daily_llm_budget_usd
        self._respect_market_hours = respect_market_hours
        self._market = market_label

    @property
    def market_label(self) -> str:
        return self._market

    async def tick(self) -> int:
        """Return the number of acted-on decisions (close + tighten)."""
        # Serialize with TradingLoop + monitor + API close paths on the
        # same broker so snapshots and trade writes can't interleave.
        lock = get_lock(self._broker.market.value)
        async with lock:
            return await self._tick_locked()

    async def _tick_locked(self) -> int:
        bus = get_bus()

        if await agents_paused(self._session_factory):
            bus.publish(
                "position_review.skipped_paused",
                f"[{self._market}] agents paused — position review skipped",
            )
            return 0

        if (
            await pause_when_market_closed(self._session_factory)
            and not is_us_equities_regular_session()
        ):
            bus.publish(
                "position_review.skipped_market_closed",
                f"[{self._market}] market closed — position review idle",
            )
            return 0

        if self._budget_ceiling > 0:
            over, spent = await budget_exceeded(
                self._session_factory, ceiling_usd=self._budget_ceiling
            )
            if over:
                bus.publish(
                    "position_review.skipped_budget",
                    (
                        f"daily LLM spend ${spent:.4f} ≥ "
                        f"cap ${self._budget_ceiling:.2f}"
                    ),
                    severity=EventSeverity.WARN,
                    data={
                        "spent_usd": spent,
                        "ceiling_usd": self._budget_ceiling,
                    },
                )
                return 0

        open_trades = await self._load_open_trades()
        if not open_trades:
            return 0

        cycle_id = new_cycle_id().replace("cyc-", "pr-", 1)
        with cycle_scope(cycle_id):
            contexts = await self._build_contexts(open_trades)
            if not contexts:
                return 0

            bus.publish(
                "position_review.started",
                (
                    f"[{self._market}] reviewing "
                    f"{len(contexts)} open position(s)"
                ),
                data={
                    "count": len(contexts),
                    "symbols": [c.symbol for c in contexts],
                },
            )

            result = await self._agent.review(contexts)
            if result.error:
                return 0

            acted = 0
            trades_by_symbol = {t.symbol: t for t in open_trades}
            for dec in result.decisions:
                trade = trades_by_symbol.get(dec.symbol)
                if trade is None:
                    continue
                if dec.decide == "close":
                    if await self._close_trade(trade, dec):
                        acted += 1
                elif dec.decide == "tighten_stop":
                    if await self._tighten_stop(trade, dec):
                        acted += 1
                # hold is a no-op; logged via agent.done in the agent.

            bus.publish(
                "position_review.done",
                (
                    f"[{self._market}] acted on {acted}/"
                    f"{len(result.decisions)} review decision(s)"
                ),
                severity=(
                    EventSeverity.SUCCESS if acted else EventSeverity.INFO
                ),
                data={"acted": acted, "total": len(result.decisions)},
            )
            return acted

    async def _load_open_trades(self) -> list[Trade]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Trade).where(
                        Trade.market == self._broker.market.value,
                        Trade.status == TradeStatus.OPEN,
                    )
                )
            ).scalars().all()
            return list(rows)

    async def _build_contexts(
        self, trades: list[Trade]
    ) -> list[PositionContext]:
        contexts: list[PositionContext] = []
        for t in trades:
            if t.entry_price is None:
                continue
            try:
                current = await self._broker.get_price(t.symbol)
            except Exception:
                logger.warning(
                    "position-review: get_price failed for %s",
                    t.symbol,
                    exc_info=True,
                )
                current = t.entry_price

            unrealized = t.size_usd * (
                (current / t.entry_price) - 1.0 if t.entry_price else 0.0
            )
            news_lines: list[str] = []
            if self._news and self._news.enabled:
                try:
                    items = await self._news.company_news(
                        t.symbol, lookback_days=2, limit=5
                    )
                    news_lines = [
                        f"[{n.source}] {n.headline}" for n in items if n.headline
                    ]
                except Exception:
                    logger.warning(
                        "position-review: news fetch failed for %s",
                        t.symbol,
                        exc_info=True,
                    )

            structure = None
            if t.option_json:
                structure = str(
                    t.option_json.get("structure") if t.option_json else None
                )

            contexts.append(
                PositionContext(
                    symbol=t.symbol,
                    size_usd=float(t.size_usd),
                    entry_price=float(t.entry_price),
                    current_price=float(current),
                    unrealized_pnl_usd=float(unrealized),
                    stop_loss_pct=t.stop_loss_pct,
                    take_profit_pct=t.take_profit_pct,
                    option_structure=structure,
                    opened_at_iso=(
                        t.opened_at.isoformat() if t.opened_at else None
                    ),
                    news_lines=news_lines,
                )
            )
        return contexts

    async def _close_trade(self, trade: Trade, dec: ReviewDecision) -> bool:
        bus = get_bus()
        # Idempotency guard: re-read the row under the lock before hitting
        # the broker. Agent dedup handles the common case, but if a
        # duplicate slips through (or monitor already closed it), we must
        # not submit a second broker order — every extra close_position
        # call to Alpaca while a bracket holds the shares fires a new
        # market order that gets canceled for "insufficient qty".
        async with self._session_factory() as session:
            live = await session.get(Trade, trade.id)
            if live is None or live.status != TradeStatus.OPEN:
                return False
        try:
            if trade.option_json:
                result = await self._broker.close_option_position(
                    trade.option_json
                )
            else:
                result = await self._broker.close_position(trade.symbol)
        except Exception as exc:
            logger.exception("position-review: broker close raised")
            bus.publish(
                "position_review.close_failed",
                f"{trade.symbol}: {exc}",
                severity=EventSeverity.ERROR,
                data={"symbol": trade.symbol, "error": str(exc)},
            )
            return False

        if not result.success:
            bus.publish(
                "position_review.close_failed",
                f"{trade.symbol}: {result.error}",
                severity=EventSeverity.ERROR,
                data={"symbol": trade.symbol, "error": result.error},
            )
            return False

        exit_price = result.fill_price
        async with self._session_factory() as session:
            live = await session.get(Trade, trade.id)
            if live is None or live.status != TradeStatus.OPEN:
                return False
            live.status = TradeStatus.CLOSED
            live.closed_at = utc_now()
            live.exit_price = exit_price
            live.broker_close_order_id = result.broker_order_id
            if (
                exit_price is not None
                and live.entry_price
                and not live.option_json
            ):
                bps = await load_active_paper_cost_bps(session)
                live.realized_pnl_usd = realized_pnl_usd(
                    action=live.action,
                    size_usd=live.size_usd,
                    entry_price=live.entry_price,
                    exit_price=exit_price,
                    paper_mode=self._broker.paper_mode,
                    paper_cost_bps=bps,
                )
            session.add(live)
            await session.commit()

        bus.publish(
            "position_review.closed",
            (
                f"{trade.symbol} closed by position-review "
                f"(urgency={dec.urgency}): {dec.rationale[:120]}"
            ),
            severity=EventSeverity.WARN,
            data={
                "symbol": trade.symbol,
                "urgency": dec.urgency,
                "rationale": dec.rationale,
                "exit_price": exit_price,
            },
        )
        return True

    async def _tighten_stop(
        self, trade: Trade, dec: ReviewDecision
    ) -> bool:
        bus = get_bus()
        new_stop = dec.new_stop_loss_pct
        if new_stop is None or new_stop <= 0:
            return False
        current_stop = trade.stop_loss_pct
        if current_stop is not None and new_stop >= current_stop:
            # not actually tighter
            return False
        async with self._session_factory() as session:
            live = await session.get(Trade, trade.id)
            if live is None or live.status != TradeStatus.OPEN:
                return False
            live.stop_loss_pct = float(new_stop)
            session.add(live)
            await session.commit()

        bus.publish(
            "position_review.stop_tightened",
            (
                f"{trade.symbol} stop → {new_stop * 100:.2f}% "
                f"(was {current_stop * 100:.2f}%)"
                if current_stop is not None
                else f"{trade.symbol} stop set → {new_stop * 100:.2f}%"
            ),
            severity=EventSeverity.INFO,
            data={
                "symbol": trade.symbol,
                "new_stop_loss_pct": new_stop,
                "prev_stop_loss_pct": current_stop,
                "rationale": dec.rationale,
            },
        )
        return True


__all__ = ["PositionReviewLoop"]
