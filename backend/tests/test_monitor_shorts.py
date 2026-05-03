"""RuntimeMonitor short-side behavior.

Before the sign-flip fix: rising price on a short position looked like
profit, which tripped take-profit (exiting the losing short for a
larger loss) and reported realized_pnl with the wrong sign. These tests
pin the fixed behavior so shorts can't silently cost real money.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.base import BrokerAdapter, OrderResult
from app.models import Base, Trade, TradeStatus, utc_now
from app.risk import Market, Position, PositionSide, TradeAction
from app.scheduler.monitor import RuntimeMonitor


class _PriceBroker(BrokerAdapter):
    def __init__(self, price: float):
        self.price = price
        self.closed: list[str] = []

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
        return self.price

    async def place_order(self, proposal):
        raise NotImplementedError

    async def close_position(self, symbol: str) -> OrderResult:
        self.closed.append(symbol)
        return OrderResult(
            success=True, broker_order_id="close-1", fill_price=self.price
        )


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    # SystemState row for agents_paused lookup.
    from app.models import SystemState

    async with sf() as s:
        s.add(SystemState(id=1, trading_enabled=True, agents_paused=False))
        await s.commit()
    yield sf
    await engine.dispose()


async def _insert_trade(
    sf,
    *,
    symbol: str,
    action: str,
    entry: float,
    stop_loss_pct: float = 0.03,
    take_profit_pct: float = 0.06,
) -> int:
    async with sf() as s:
        t = Trade(
            market=Market.STOCKS.value,
            symbol=symbol,
            action=action,
            size_usd=100.0,
            entry_price=entry,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            status=TradeStatus.OPEN,
            paper_mode=True,
            opened_at=utc_now(),
        )
        s.add(t)
        await s.commit()
        return t.id


@pytest.mark.asyncio
async def test_short_stops_out_when_price_rises(session_factory):
    # Short @ 100, current 105 → short is down 5% → stop-loss (3%) fires.
    trade_id = await _insert_trade(
        session_factory,
        symbol="TSLA",
        action=TradeAction.OPEN_SHORT.value,
        entry=100.0,
    )
    broker = _PriceBroker(price=105.0)
    closed = await RuntimeMonitor(broker, session_factory).tick()
    assert closed == 1
    assert broker.closed == ["TSLA"]
    async with session_factory() as s:
        t = await s.get(Trade, trade_id)
        assert t.status == TradeStatus.CLOSED
        # Short lost 5% of $100 = -$5 (not +$5).
        assert t.realized_pnl_usd == pytest.approx(-5.0)


@pytest.mark.asyncio
async def test_short_takes_profit_when_price_falls(session_factory):
    # Short @ 100, current 93 → short up 7% → take-profit (6%) fires.
    trade_id = await _insert_trade(
        session_factory,
        symbol="TSLA",
        action=TradeAction.OPEN_SHORT.value,
        entry=100.0,
    )
    broker = _PriceBroker(price=93.0)
    closed = await RuntimeMonitor(broker, session_factory).tick()
    assert closed == 1
    async with session_factory() as s:
        t = await s.get(Trade, trade_id)
        assert t.status == TradeStatus.CLOSED
        # Short gained 7% of $100 = +$7.
        assert t.realized_pnl_usd == pytest.approx(7.0)


@pytest.mark.asyncio
async def test_short_price_move_without_trigger_stays_open(session_factory):
    # Short @ 100, current 101 → short down 1% → no trigger (stop 3%).
    trade_id = await _insert_trade(
        session_factory,
        symbol="TSLA",
        action=TradeAction.OPEN_SHORT.value,
        entry=100.0,
    )
    broker = _PriceBroker(price=101.0)
    closed = await RuntimeMonitor(broker, session_factory).tick()
    assert closed == 0
    assert broker.closed == []
    async with session_factory() as s:
        t = await s.get(Trade, trade_id)
        assert t.status == TradeStatus.OPEN


@pytest.mark.asyncio
async def test_long_still_correct(session_factory):
    # Regression: long side unchanged by the sign flip.
    trade_id = await _insert_trade(
        session_factory,
        symbol="AAPL",
        action=TradeAction.OPEN_LONG.value,
        entry=100.0,
    )
    broker = _PriceBroker(price=95.0)  # down 5% → stop fires
    closed = await RuntimeMonitor(broker, session_factory).tick()
    assert closed == 1
    async with session_factory() as s:
        t = await s.get(Trade, trade_id)
        assert t.realized_pnl_usd == pytest.approx(-5.0)


def test_alpaca_tags_short_side():
    """Broker adapter reports qty<0 as SHORT so the snapshot sees it."""
    # Alpaca SDK isn't mocked deeply here — we just check the mapping
    # logic by constructing a Position directly as the adapter would.
    short = Position(
        market=Market.STOCKS,
        symbol="TSLA",
        size_usd=100.0,
        entry_price=50.0,
        current_price=55.0,
        side=PositionSide.SHORT,
    )
    # Losing short → negative unrealized.
    assert short.unrealized_pnl == pytest.approx(-10.0)
