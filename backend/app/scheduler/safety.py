"""Deterministic safety rails that run on the RuntimeMonitor cadence.

These are NOT LLM-driven. They enforce hard rules that no market reading
can override:

  · Circuit-breaker — N consecutive losing closes today → pause agents.
  · DTE watchdog    — option leg within K days of expiry → auto-close.

Both hook into the same 30s monitor tick so the latency on a forced
action is at most one interval.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clock import ny_today

from app.activity import EventSeverity, get_bus
from app.brokers import BrokerAdapter
from app.clock import pacific_day_bounds_utc
from app.models import SystemState, Trade, TradeStatus, utc_now

logger = logging.getLogger(__name__)


class SafetyMonitor:
    """Deterministic halting rules. Runs alongside RuntimeMonitor."""

    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        session_factory: async_sessionmaker,
        consecutive_loss_limit: int = 3,
        option_dte_watchdog_days: int = 1,
    ) -> None:
        self._broker = broker
        self._session_factory = session_factory
        self._loss_limit = consecutive_loss_limit
        self._dte_days = option_dte_watchdog_days

    async def tick(self) -> int:
        """Return the number of safety actions taken this tick."""
        actions = 0
        actions += await self._check_circuit_breaker()
        actions += await self._check_dte_watchdog()
        return actions

    async def _check_circuit_breaker(self) -> int:
        if self._loss_limit <= 0:
            return 0
        bus = get_bus()
        day_start, _ = pacific_day_bounds_utc()
        async with self._session_factory() as session:
            state = await session.get(SystemState, 1)
            if state is None or state.agents_paused:
                return 0
            closes = (
                await session.execute(
                    select(Trade)
                    .where(
                        Trade.market == self._broker.market.value,
                        Trade.status == TradeStatus.CLOSED,
                        Trade.closed_at >= day_start,
                    )
                    .order_by(desc(Trade.closed_at))
                    .limit(self._loss_limit)
                )
            ).scalars().all()
            if len(closes) < self._loss_limit:
                return 0
            if not all((t.realized_pnl_usd or 0.0) < 0.0 for t in closes):
                return 0
            state.agents_paused = True
            session.add(state)
            await session.commit()

        bus.publish(
            "safety.circuit_breaker",
            (
                f"circuit-breaker tripped: {self._loss_limit} consecutive "
                "losing closes — agents paused"
            ),
            severity=EventSeverity.ERROR,
            data={
                "consecutive_losses": self._loss_limit,
                "action": "agents_paused",
            },
        )
        return 1

    async def _check_dte_watchdog(self) -> int:
        if self._dte_days <= 0:
            return 0
        bus = get_bus()
        today = ny_today()
        threshold = today + timedelta(days=self._dte_days)
        closed_count = 0
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Trade).where(
                        Trade.market == self._broker.market.value,
                        Trade.status == TradeStatus.OPEN,
                        Trade.option_json.is_not(None),
                    )
                )
            ).scalars().all()

            to_close: list[Trade] = []
            for t in rows:
                legs = (t.option_json or {}).get("legs") or []
                min_expiry = _min_leg_expiry(legs)
                if min_expiry is None:
                    continue
                if min_expiry <= threshold:
                    to_close.append(t)

        for t in to_close:
            try:
                result = await self._broker.close_option_position(
                    t.option_json or {}
                )
            except Exception as exc:
                logger.exception("DTE close raised for %s", t.symbol)
                bus.publish(
                    "safety.dte_close_failed",
                    f"{t.symbol}: {exc}",
                    severity=EventSeverity.ERROR,
                    data={"symbol": t.symbol, "error": str(exc)},
                )
                continue
            if not result.success:
                bus.publish(
                    "safety.dte_close_failed",
                    f"{t.symbol}: {result.error}",
                    severity=EventSeverity.ERROR,
                    data={"symbol": t.symbol, "error": result.error},
                )
                continue
            async with self._session_factory() as session:
                live = await session.get(Trade, t.id)
                if live is None or live.status != TradeStatus.OPEN:
                    continue
                live.status = TradeStatus.CLOSED
                live.closed_at = utc_now()
                live.exit_price = result.fill_price
                live.broker_close_order_id = result.broker_order_id
                session.add(live)
                await session.commit()
            closed_count += 1
            bus.publish(
                "safety.dte_closed",
                (
                    f"{t.symbol} option force-closed — expiry within "
                    f"{self._dte_days}d"
                ),
                severity=EventSeverity.WARN,
                data={
                    "symbol": t.symbol,
                    "dte_threshold_days": self._dte_days,
                    "structure": (t.option_json or {}).get("structure"),
                },
            )
        return closed_count


def _min_leg_expiry(legs: list) -> date | None:
    best: date | None = None
    for leg in legs:
        exp = leg.get("expiry") if isinstance(leg, dict) else None
        if not exp:
            continue
        try:
            d = date.fromisoformat(exp)
        except ValueError:
            continue
        if best is None or d < best:
            best = d
    return best


__all__ = ["SafetyMonitor"]
