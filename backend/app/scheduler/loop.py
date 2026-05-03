"""TradingLoop.tick() — one end-to-end decision cycle.

strategy.decide(snapshot) → RiskEngine.validate → broker.place_order,
persisting a Decision row with full audit trail regardless of outcome.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.activity import EventSeverity, get_bus
from app.ai.trace import cycle_scope, new_cycle_id
from app.brokers import BrokerAdapter
from app.models import Decision, Trade, TradeStatus, utc_now
from app.risk import RiskEngine, TradeAction, realized_pnl_usd
from app.strategies import Strategy

from .budget import budget_exceeded
from .locks import get_lock
from .snapshot import agents_paused, build_snapshot, pause_when_market_closed

logger = logging.getLogger(__name__)


class TradingLoop:
    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        strategy: Strategy,
        risk_engine: RiskEngine,
        session_factory,
        daily_llm_budget_usd: float = 0.0,
        respect_market_hours: bool = True,
        decide_timeout_sec: float = 180.0,
    ) -> None:
        self.broker = broker
        self.strategy = strategy
        self.risk_engine = risk_engine
        self.session_factory = session_factory
        self.daily_llm_budget_usd = daily_llm_budget_usd
        self.respect_market_hours = respect_market_hours
        self.decide_timeout_sec = decide_timeout_sec

    async def _market_open(self) -> bool:
        try:
            return await self.broker.is_market_open()
        except Exception:
            logger.warning("broker is_market_open failed", exc_info=True)
            # Fail-closed: an unknown clock state must not trigger trades
            # during off-hours. A transient outage pausing the loop is
            # strictly safer than placing orders at an uncertain time.
            return False

    async def tick(self) -> Decision | None:
        if await agents_paused(self.session_factory):
            get_bus().publish(
                "loop.skipped_paused",
                f"[{self.broker.market.value}] agents paused — skipping tick",
                severity=EventSeverity.INFO,
            )
            return None
        # Opt-in: skip the whole tick (no LLM spend) when the market is
        # closed. Cheaper than the 24/7 mode which still researches. Auto-
        # re-enables on next session open.
        if await pause_when_market_closed(self.session_factory) and not await self._market_open():
            get_bus().publish(
                "loop.skipped_market_closed",
                f"[{self.broker.market.value}] market closed — agents idle",
                severity=EventSeverity.INFO,
            )
            return None
        # Market-hours gate used to skip the entire tick. We now let the
        # strategy run (research + proposal logging) 24/7 and only block
        # the broker call at order-submission time. See _market_open().
        if self.daily_llm_budget_usd > 0:
            over, spent = await budget_exceeded(
                self.session_factory, ceiling_usd=self.daily_llm_budget_usd
            )
            if over:
                get_bus().publish(
                    "loop.skipped_budget",
                    (
                        f"daily LLM spend ${spent:.4f} ≥ "
                        f"cap ${self.daily_llm_budget_usd:.2f}"
                    ),
                    severity=EventSeverity.WARN,
                    data={
                        "spent_usd": spent,
                        "ceiling_usd": self.daily_llm_budget_usd,
                    },
                )
                return None
        cycle_id = new_cycle_id()
        # Serialize with monitor + position-review + API close paths on
        # the same broker so snapshots and trade writes can't interleave.
        lock = get_lock(self.broker.market.value)
        async with lock:
            with cycle_scope(cycle_id):
                async with self.session_factory() as session:
                    return await self._tick_in_session(session, cycle_id)

    async def _tick_in_session(
        self, session: AsyncSession, cycle_id: str | None = None
    ) -> Decision:
        bus = get_bus()
        market = self.broker.market.value

        bus.publish(
            "loop.tick.start",
            f"[{market}] decision cycle started",
            data={"cycle_id": cycle_id},
        )

        snapshot = await build_snapshot(self.broker, session)
        bus.publish(
            "snapshot.built",
            (
                f"cash=${snapshot.cash_balance:.2f} "
                f"exposure=${snapshot.total_exposure_usd:.2f} "
                f"positions={len(snapshot.positions)} "
                f"trading_enabled={snapshot.trading_enabled}"
            ),
            data={
                "cash": snapshot.cash_balance,
                "exposure": snapshot.total_exposure_usd,
                "day_pnl": snapshot.day_realized_pnl,
                "cumulative_pnl": snapshot.cumulative_pnl,
                "trading_enabled": snapshot.trading_enabled,
                "positions": [
                    {
                        "symbol": p.symbol,
                        "size_usd": p.size_usd,
                        "entry": p.entry_price,
                        "now": p.current_price,
                    }
                    for p in snapshot.positions
                ],
            },
        )

        try:
            proposal = await asyncio.wait_for(
                self.strategy.decide(snapshot),
                timeout=self.decide_timeout_sec,
            )
        except asyncio.TimeoutError:
            # Fail-closed: a hung LLM must not keep holding the per-broker
            # lock. Record a no-op decision and bail out so the next tick
            # gets a fresh chance.
            decision = Decision(
                market=market,
                model="",
                prompt_json={},
                response_json={"error": "decide_timeout"},
                rationale=(
                    f"strategy.decide timed out after "
                    f"{self.decide_timeout_sec:.0f}s"
                ),
                proposal_json=None,
                approved=False,
                executed=False,
                rejection_code="decide_timeout",
                rejection_reason=(
                    f"decide exceeded {self.decide_timeout_sec:.0f}s budget"
                ),
                cycle_id=cycle_id,
            )
            session.add(decision)
            await session.commit()
            logger.error(
                "[%s] strategy.decide timed out after %.0fs",
                market,
                self.decide_timeout_sec,
            )
            bus.publish(
                "loop.tick.timeout",
                f"[{market}] strategy.decide timed out — tick aborted",
                severity=EventSeverity.ERROR,
                data={
                    "decision_id": decision.id,
                    "timeout_sec": self.decide_timeout_sec,
                },
            )
            return decision

        decision = Decision(
            market=proposal.market.value,
            model=proposal.model,
            prompt_json=proposal.raw_prompt or {},
            response_json=proposal.raw_response or {"rationale": proposal.rationale},
            rationale=proposal.rationale,
            proposal_json=(
                _proposal_to_dict(proposal.trade) if proposal.trade else None
            ),
            approved=False,
            executed=False,
            research_json=list(proposal.research) if proposal.research else None,
            cycle_id=cycle_id,
        )

        if proposal.trade is None:
            decision.rejection_code = "strategy_no_op"
            decision.rejection_reason = proposal.rationale
            session.add(decision)
            await session.commit()
            bus.publish(
                "loop.tick.end",
                f"[{market}] no-op: {proposal.rationale[:120]}",
                data={"decision_id": decision.id, "outcome": "hold"},
            )
            return decision

        result = self.risk_engine.validate(proposal.trade, snapshot)

        if not result.approved:
            decision.rejection_code = (
                result.code.value if result.code else "unknown"
            )
            decision.rejection_reason = result.reason
            session.add(decision)
            await session.commit()
            logger.info(
                "RiskEngine rejected: %s — %s", decision.rejection_code, result.reason
            )
            bus.publish(
                "risk.rejected",
                f"{decision.rejection_code}: {result.reason}",
                severity=EventSeverity.WARN,
                data={
                    "code": decision.rejection_code,
                    "reason": result.reason,
                    "symbol": proposal.trade.symbol,
                    "size_usd": proposal.trade.size_usd,
                },
            )
            bus.publish(
                "loop.tick.end",
                f"[{market}] rejected by risk engine",
                data={"decision_id": decision.id, "outcome": "rejected"},
            )
            return decision

        adjusted = result.adjusted
        assert adjusted is not None
        decision.approved = True
        decision.proposal_json = _proposal_to_dict(adjusted)

        bus.publish(
            "risk.approved",
            f"{adjusted.action.value} {adjusted.symbol} ${adjusted.size_usd:.2f}",
            severity=EventSeverity.SUCCESS,
            data={
                "action": adjusted.action.value,
                "symbol": adjusted.symbol,
                "size_usd": adjusted.size_usd,
                "stop_loss_pct": adjusted.stop_loss_pct,
                "take_profit_pct": adjusted.take_profit_pct,
            },
        )

        if self.respect_market_hours and not await self._market_open():
            decision.approved = True
            decision.rejection_code = "market_closed"
            decision.rejection_reason = (
                "proposal approved but market is closed — order not submitted"
            )
            session.add(decision)
            await session.commit()
            bus.publish(
                "order.skipped_market_closed",
                (
                    f"{adjusted.action.value} {adjusted.symbol} "
                    f"${adjusted.size_usd:.2f} — market closed, not submitted"
                ),
                severity=EventSeverity.WARN,
                data={
                    "decision_id": decision.id,
                    "action": adjusted.action.value,
                    "symbol": adjusted.symbol,
                },
            )
            bus.publish(
                "loop.tick.end",
                f"[{market}] proposal logged, market closed",
                data={"decision_id": decision.id, "outcome": "market_closed"},
            )
            return decision

        bus.publish(
            "order.submit",
            f"submitting {adjusted.action.value} {adjusted.symbol} ${adjusted.size_usd:.2f}",
            data={
                "action": adjusted.action.value,
                "symbol": adjusted.symbol,
                "size_usd": adjusted.size_usd,
            },
        )

        if adjusted.action == TradeAction.CLOSE:
            # If the underlying trade was an option structure, replay the
            # stored leg spec with inverse intents. Otherwise close the
            # plain stock position by symbol.
            open_trade = await _find_open_option_trade(
                session, adjusted.market.value, adjusted.symbol
            )
            if open_trade is not None and open_trade.option_json:
                order = await self.broker.close_option_position(open_trade.option_json)
            else:
                order = await self.broker.close_position(adjusted.symbol)
        elif adjusted.option is not None:
            order = await self.broker.place_multileg_order(adjusted)
        else:
            order = await self.broker.place_order(adjusted)

        if not order.success:
            decision.execution_error = order.error
            session.add(decision)
            await session.commit()
            logger.error("broker order failed: %s", order.error)
            bus.publish(
                "order.failed",
                f"{adjusted.symbol}: {order.error}",
                severity=EventSeverity.ERROR,
                data={"symbol": adjusted.symbol, "error": order.error},
            )
            bus.publish(
                "loop.tick.end",
                f"[{market}] broker error",
                severity=EventSeverity.ERROR,
                data={"decision_id": decision.id, "outcome": "broker_failed"},
            )
            return decision

        # For CLOSE actions, find and mark the open trade closed rather
        # than writing a new Trade row.
        if adjusted.action == TradeAction.CLOSE:
            open_trade = await _find_open_trade(
                session, adjusted.market.value, adjusted.symbol
            )
            if open_trade is not None:
                exit_price = order.fill_price
                open_trade.status = TradeStatus.CLOSED
                open_trade.closed_at = utc_now()
                open_trade.exit_price = exit_price
                open_trade.broker_close_order_id = order.broker_order_id
                # Compute pnl for stock AND option closes. For long/debit
                # option structures, size_usd is the notional paid and
                # entry/exit are per-contract net premiums — the same
                # (exit/entry - 1) ratio applies. Skip only when entry is
                # missing/non-positive (credit structures not supported).
                if (
                    exit_price is not None
                    and open_trade.entry_price
                    and open_trade.entry_price > 0
                ):
                    open_trade.realized_pnl_usd = realized_pnl_usd(
                        action=open_trade.action,
                        size_usd=open_trade.size_usd,
                        entry_price=open_trade.entry_price,
                        exit_price=exit_price,
                        paper_mode=self.broker.paper_mode,
                        paper_cost_bps=self.risk_engine.config.paper_cost_bps,
                    )
                session.add(open_trade)
                await session.flush()
        else:
            # Alpaca accepts orders off-hours with fill_price=None and fills
            # them at the next session open. Mark PENDING until we see a
            # fill so unfilled/cancelled orders never get counted as live
            # positions.
            status = (
                TradeStatus.OPEN if order.fill_price is not None
                else TradeStatus.PENDING
            )
            trade = Trade(
                market=adjusted.market.value,
                symbol=adjusted.symbol,
                action=adjusted.action.value,
                size_usd=adjusted.size_usd,
                entry_price=order.fill_price,
                stop_loss_pct=adjusted.stop_loss_pct,
                take_profit_pct=adjusted.take_profit_pct,
                broker_order_id=order.broker_order_id,
                status=status,
                paper_mode=self.broker.paper_mode,
                opened_at=utc_now() if order.fill_price is not None else None,
                option_json=(
                    _option_to_dict(adjusted.option) if adjusted.option else None
                ),
            )
            session.add(trade)
            await session.flush()

        decision.executed = True
        session.add(decision)
        await session.commit()
        logger.info(
            "executed %s %s $%.2f (order %s)",
            adjusted.action.value,
            adjusted.symbol,
            adjusted.size_usd,
            order.broker_order_id,
        )
        bus.publish(
            "order.filled",
            f"{adjusted.action.value} {adjusted.symbol} @ ${order.fill_price or 0:.2f}",
            severity=EventSeverity.SUCCESS,
            data={
                "symbol": adjusted.symbol,
                "fill_price": order.fill_price,
                "broker_order_id": order.broker_order_id,
            },
        )
        bus.publish(
            "loop.tick.end",
            f"[{market}] executed {adjusted.symbol}",
            severity=EventSeverity.SUCCESS,
            data={"decision_id": decision.id, "outcome": "executed"},
        )
        return decision


def _proposal_to_dict(p) -> dict:
    return {
        "market": p.market.value,
        "action": p.action.value,
        "symbol": p.symbol,
        "size_usd": p.size_usd,
        "stop_loss_pct": p.stop_loss_pct,
        "take_profit_pct": p.take_profit_pct,
        "rationale": p.rationale,
        "confidence": p.confidence,
        "option": _option_to_dict(p.option) if p.option else None,
    }


def _option_to_dict(opt) -> dict:
    return {
        "structure": opt.structure.value,
        "underlying": opt.underlying,
        "expiry": opt.expiry,
        "net_debit_usd": opt.net_debit_usd,
        "max_loss_usd": opt.max_loss_usd,
        "max_gain_usd": opt.max_gain_usd,
        "legs": [
            {
                "option_symbol": leg.option_symbol,
                "side": leg.side.value,
                "strike": leg.strike,
                "expiry": leg.expiry,
                "ratio": leg.ratio,
                "mid_price": leg.mid_price,
            }
            for leg in opt.legs
        ],
    }


async def _find_open_trade(
    session: AsyncSession, market: str, symbol: str
) -> Trade | None:
    """Return the most recently opened OPEN trade for this underlying.

    We match on symbol (the underlying ticker) since option structures are
    stored with the underlying as `symbol` even though the broker holds
    OCC-symbol legs."""
    stmt = (
        select(Trade)
        .where(
            Trade.market == market,
            Trade.symbol == symbol,
            Trade.status == TradeStatus.OPEN,
        )
        .order_by(Trade.opened_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _find_open_option_trade(
    session: AsyncSession, market: str, symbol: str
) -> Trade | None:
    trade = await _find_open_trade(session, market, symbol)
    if trade is not None and trade.option_json:
        return trade
    return None
