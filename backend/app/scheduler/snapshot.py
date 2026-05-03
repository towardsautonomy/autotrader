"""Build an AccountSnapshot by combining broker state with DB-tracked
counters (daily trade count, realized P&L, cumulative P&L, kill switch)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.brokers import BrokerAdapter
from app.clock import (
    five_business_days_ago_ny_start_utc,
    ny_session_date,
    pacific_day_bounds_utc,
)
from app.models import SystemState, Trade, TradeStatus
from app.risk import AccountSnapshot, Market, Position, PositionSide, TradeAction


async def agents_paused(session_factory: async_sessionmaker) -> bool:
    """Read the global agents_paused flag. Returns False if unset/missing."""
    async with session_factory() as session:
        state = await session.get(SystemState, 1)
        return bool(state.agents_paused) if state else False


async def pause_when_market_closed(session_factory: async_sessionmaker) -> bool:
    """Read the pause-when-market-closed flag. Returns False if unset."""
    async with session_factory() as session:
        state = await session.get(SystemState, 1)
        return bool(state.pause_when_market_closed) if state else False


async def build_snapshot(
    broker: BrokerAdapter, session: AsyncSession
) -> AccountSnapshot:
    cash = await broker.get_cash_balance()
    broker_positions = list(await broker.get_positions())

    day_start, _ = pacific_day_bounds_utc()

    # Supplement broker positions with open *option* trades we've tracked
    # locally. Alpaca reports each leg as its own OCC-symbol position, which
    # doesn't let the risk engine match a CLOSE by underlying ticker. We
    # fold each open option Trade into one synthetic Position keyed on the
    # underlying so find_position(market, underlying) works.
    # Pull every open trade once — we need the open ones for option
    # synthesis AND for short-side tagging on the broker positions, so
    # pulling them together saves a round-trip.
    open_trades = (
        await session.execute(
            select(Trade).where(
                Trade.market == broker.market.value,
                Trade.status == TradeStatus.OPEN,
            )
        )
    ).scalars().all()

    open_option_trades = [t for t in open_trades if t.option_json is not None]

    occ_symbols_covered: set[str] = set()
    for t in open_option_trades:
        legs = (t.option_json or {}).get("legs") or []
        for leg in legs:
            if leg.get("option_symbol"):
                occ_symbols_covered.add(leg["option_symbol"])

    # Broker returns size/price but not long-vs-short. Resolve side from
    # the Trade row so unrealized P&L comes out signed correctly for
    # short positions.
    side_by_symbol: dict[str, PositionSide] = {}
    for t in open_trades:
        if t.option_json is not None:
            continue
        side_by_symbol[t.symbol] = (
            PositionSide.SHORT
            if t.action == TradeAction.OPEN_SHORT
            else PositionSide.LONG
        )

    # Drop broker-reported legs that are already represented by our
    # synthetic underlying-level position, to avoid double-counting.
    positions: list[Position] = []
    for p in broker_positions:
        if p.symbol in occ_symbols_covered:
            continue
        side = side_by_symbol.get(p.symbol, p.side)
        positions.append(p if p.side == side else _with_side(p, side))

    for t in open_option_trades:
        entry = t.entry_price if t.entry_price is not None else 0.0
        positions.append(
            Position(
                market=Market(t.market),
                symbol=t.symbol,
                size_usd=float(t.size_usd),
                entry_price=float(entry),
                current_price=float(entry),
            )
        )

    positions_tuple = tuple(positions)

    # Count trades that actually executed (opened_at is set only on fill),
    # not rows that were created and never filled. Using created_at here
    # would let rejected/pending rows chew through the daily trade cap.
    day_count_stmt = select(func.count(Trade.id)).where(
        Trade.opened_at.is_not(None),
        Trade.opened_at >= day_start,
        Trade.market == broker.market.value,
    )
    daily_trade_count = (await session.execute(day_count_stmt)).scalar_one()

    day_pnl_stmt = select(func.coalesce(func.sum(Trade.realized_pnl_usd), 0.0)).where(
        Trade.closed_at >= day_start,
        Trade.status == TradeStatus.CLOSED,
        Trade.market == broker.market.value,
    )
    day_realized_pnl = float((await session.execute(day_pnl_stmt)).scalar_one() or 0.0)

    cumulative_pnl_stmt = select(
        func.coalesce(func.sum(Trade.realized_pnl_usd), 0.0)
    ).where(Trade.status == TradeStatus.CLOSED, Trade.market == broker.market.value)
    cumulative_pnl = float(
        (await session.execute(cumulative_pnl_stmt)).scalar_one() or 0.0
    )

    # Today's unrealized: sum mark-to-market on positions whose trades
    # opened today. Feeds the daily-loss halt so an open position that's
    # 50% underwater intraday blocks new trades before it closes.
    # SQLite can return naive datetimes even with timezone=True; treat
    # those as UTC so the comparison against day_start (tz-aware) works.
    opened_today_symbols: set[str] = set()
    for t in open_trades:
        ts = t.opened_at
        if ts is None:
            continue
        if ts.tzinfo is None:
            from datetime import UTC

            ts = ts.replace(tzinfo=UTC)
        if ts >= day_start:
            opened_today_symbols.add(t.symbol)
    day_unrealized_pnl = sum(
        p.unrealized_pnl for p in positions_tuple if p.symbol in opened_today_symbols
    )

    # PDT: count trades whose open + close landed in the same NY session
    # date, closed within the trailing 5 business days. Only stocks count
    # toward FINRA's PDT definition — options exits are separate, and
    # Polymarket isn't an equity broker.
    pdt_window_used = 0
    if broker.market == Market.STOCKS:
        pdt_window_start = five_business_days_ago_ny_start_utc()
        # Filter option_json in Python — SQLite's JSON NULL handling makes
        # `.is_(None)` unreliable at the SQL level.
        pdt_rows = (
            await session.execute(
                select(
                    Trade.opened_at, Trade.closed_at, Trade.option_json
                ).where(
                    Trade.market == broker.market.value,
                    Trade.status == TradeStatus.CLOSED,
                    Trade.closed_at >= pdt_window_start,
                    Trade.opened_at.is_not(None),
                )
            )
        ).all()
        for opened_at, closed_at, option_json in pdt_rows:
            if opened_at is None or closed_at is None:
                continue
            if option_json:
                continue
            if ny_session_date(opened_at) == ny_session_date(closed_at):
                pdt_window_used += 1

    state = await session.get(SystemState, 1)
    trading_enabled = bool(state.trading_enabled) if state else True

    return AccountSnapshot(
        cash_balance=cash,
        positions=positions_tuple,
        day_realized_pnl=day_realized_pnl,
        cumulative_pnl=cumulative_pnl,
        daily_trade_count=int(daily_trade_count),
        trading_enabled=trading_enabled,
        day_unrealized_pnl=float(day_unrealized_pnl),
        pdt_day_trades_window_used=int(pdt_window_used),
    )


def _with_side(p: Position, side: PositionSide) -> Position:
    return Position(
        market=p.market,
        symbol=p.symbol,
        size_usd=p.size_usd,
        entry_price=p.entry_price,
        current_price=p.current_price,
        side=side,
    )


__all__ = ["build_snapshot", "agents_paused", "pause_when_market_closed"]
