"""PDT window counting in the snapshot builder.

Pins that same-NY-session open+close round trips within the trailing 5
business days are counted, while overnight holds, options, and old
trades are excluded. The engine relies on this count to enforce the
3-in-5 rule — a miscount here silently lets the system trigger FINRA's
PDT restriction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.base import BrokerAdapter, OrderResult
from app.clock import (
    NYSE,
    five_business_days_ago_ny_start_utc,
    ny_session_date,
)
from app.models import Base, SystemState, Trade, TradeStatus
from app.risk import Market, Position, TradeAction
from app.scheduler.snapshot import build_snapshot


class _NullBroker(BrokerAdapter):
    @property
    def market(self):
        return Market.STOCKS

    @property
    def paper_mode(self):
        return True

    async def get_cash_balance(self):
        return 1000.0

    async def get_positions(self) -> list[Position]:
        return []

    async def is_market_open(self):
        return True

    async def get_price(self, symbol: str) -> float:
        return 0.0

    async def place_order(self, proposal):
        raise NotImplementedError

    async def close_position(self, symbol: str) -> OrderResult:
        raise NotImplementedError


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as s:
        s.add(SystemState(id=1, trading_enabled=True, agents_paused=False))
        await s.commit()
    yield sf
    await engine.dispose()


async def _insert_closed_round_trip(
    sf,
    *,
    opened_at: datetime,
    closed_at: datetime,
    symbol: str = "AAPL",
    option_json: dict | None = None,
) -> None:
    async with sf() as s:
        s.add(
            Trade(
                market=Market.STOCKS.value,
                symbol=symbol,
                action=TradeAction.OPEN_LONG.value,
                size_usd=100.0,
                entry_price=100.0,
                exit_price=101.0,
                status=TradeStatus.CLOSED,
                paper_mode=True,
                opened_at=opened_at,
                closed_at=closed_at,
                option_json=option_json,
            )
        )
        await s.commit()


def _ny_session_start(d_offset: int) -> datetime:
    """Return a UTC datetime at ~10:00 ET `d_offset` business days before today."""
    today = datetime.now(UTC).astimezone(NYSE).date()
    # walk back d_offset business days
    d = today
    remaining = d_offset
    while remaining > 0:
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            remaining -= 1
    dt = datetime.combine(d, datetime.min.time(), tzinfo=NYSE).replace(
        hour=14  # 10am ET-ish
    )
    return dt.astimezone(UTC)


@pytest.mark.asyncio
async def test_counts_same_session_round_trip(session_factory):
    opened = _ny_session_start(1)
    closed = opened + timedelta(hours=2)  # same NY date
    await _insert_closed_round_trip(
        session_factory, opened_at=opened, closed_at=closed
    )
    async with session_factory() as session:
        snap = await build_snapshot(_NullBroker(), session)
    assert snap.pdt_day_trades_window_used == 1


@pytest.mark.asyncio
async def test_ignores_overnight_hold(session_factory):
    opened = _ny_session_start(2)
    # Close next NY session → NOT a day trade
    closed = opened + timedelta(days=1, hours=2)
    await _insert_closed_round_trip(
        session_factory, opened_at=opened, closed_at=closed
    )
    async with session_factory() as session:
        snap = await build_snapshot(_NullBroker(), session)
    assert snap.pdt_day_trades_window_used == 0


@pytest.mark.asyncio
async def test_ignores_options_round_trip(session_factory):
    opened = _ny_session_start(1)
    closed = opened + timedelta(hours=1)
    await _insert_closed_round_trip(
        session_factory,
        opened_at=opened,
        closed_at=closed,
        option_json={"legs": []},
    )
    async with session_factory() as session:
        snap = await build_snapshot(_NullBroker(), session)
    assert snap.pdt_day_trades_window_used == 0


@pytest.mark.asyncio
async def test_ignores_trades_outside_window(session_factory):
    # 10 business days ago — outside the 5-BD rolling window
    opened = _ny_session_start(10)
    closed = opened + timedelta(hours=1)
    await _insert_closed_round_trip(
        session_factory, opened_at=opened, closed_at=closed
    )
    async with session_factory() as session:
        snap = await build_snapshot(_NullBroker(), session)
    assert snap.pdt_day_trades_window_used == 0


def test_ny_session_date_tz_conversion():
    # 2am UTC on Mon = 10pm ET on Sun → NY date is Sunday
    ts = datetime(2026, 4, 20, 2, 0, tzinfo=UTC)
    from datetime import date

    assert ny_session_date(ts) == date(2026, 4, 19)


def test_five_business_days_helper_skips_weekends():
    # Reference a known Monday (2026-04-20)
    ref = datetime(2026, 4, 20, 18, 0, tzinfo=UTC)  # 2pm ET Monday
    # 5 NY business days back from Monday = previous Monday (2026-04-13)
    result = five_business_days_ago_ny_start_utc(ref)
    ny = result.astimezone(NYSE)
    assert ny.year == 2026 and ny.month == 4 and ny.day == 13
    assert ny.hour == 0 and ny.minute == 0


@pytest.mark.asyncio
async def test_daily_trade_count_ignores_unfilled_rows(session_factory):
    """Pending rows that never filled must not consume the daily cap.

    The counter sources from ``opened_at`` (set only on fill); a row
    with ``opened_at=None`` represents an order that was submitted
    but never executed, so counting it would let rejected/pending
    attempts drain the cap.
    """
    from datetime import UTC, datetime
    from app.models import Trade, TradeStatus
    from app.risk import Market, TradeAction
    from app.scheduler.snapshot import build_snapshot

    now = datetime.now(UTC)
    async with session_factory() as s:
        # Filled trade opened today — should count.
        s.add(
            Trade(
                market=Market.STOCKS.value,
                symbol="AAPL",
                action=TradeAction.OPEN_LONG.value,
                size_usd=100.0,
                entry_price=100.0,
                status=TradeStatus.OPEN,
                paper_mode=True,
                opened_at=now,
            )
        )
        # Unfilled pending row from today — should NOT count.
        s.add(
            Trade(
                market=Market.STOCKS.value,
                symbol="MSFT",
                action=TradeAction.OPEN_LONG.value,
                size_usd=100.0,
                entry_price=None,
                status=TradeStatus.PENDING,
                paper_mode=True,
                opened_at=None,
            )
        )
        await s.commit()

    async with session_factory() as session:
        snap = await build_snapshot(_NullBroker(), session)
    assert snap.daily_trade_count == 1
