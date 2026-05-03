"""BracketReconciler behavior.

When Alpaca fires a bracket child leg (stop or take-profit) server-side,
our Trade row stays OPEN until the reconciler catches the fill. These
tests pin that the reconciler closes the row with the fill price and
sign-aware pnl, skips trades with no bracket fill, and ignores options.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.base import BracketFill, BrokerAdapter, OrderResult
from app.models import Base, Trade, TradeStatus, utc_now
from app.risk import Market, Position, TradeAction
from app.scheduler.locks import reset_locks
from app.scheduler.reconciler import BracketReconciler


class _FakeBroker(BrokerAdapter):
    def __init__(self, fills: dict[str, BracketFill] | None = None):
        self._fills = fills or {}

    @property
    def market(self):
        return Market.STOCKS

    @property
    def paper_mode(self):
        return True

    async def get_cash_balance(self):
        return 0.0

    async def get_positions(self) -> list[Position]:
        return []

    async def is_market_open(self):
        return True

    async def get_price(self, symbol: str) -> float:
        return 0.0

    async def place_order(self, proposal):
        raise NotImplementedError

    async def close_position(self, symbol: str) -> OrderResult:
        return OrderResult(success=False, error="unexpected close call")

    async def get_bracket_fill(self, order_id: str) -> BracketFill | None:
        return self._fills.get(order_id)


@pytest.fixture
async def session_factory():
    reset_locks()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
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
    broker_order_id: str | None,
    action: str = TradeAction.OPEN_LONG.value,
    entry: float = 100.0,
    option_json: dict | None = None,
) -> int:
    async with sf() as s:
        t = Trade(
            market=Market.STOCKS.value,
            symbol=symbol,
            action=action,
            size_usd=100.0,
            entry_price=entry,
            broker_order_id=broker_order_id,
            status=TradeStatus.OPEN,
            paper_mode=True,
            opened_at=utc_now(),
            option_json=option_json,
        )
        s.add(t)
        await s.commit()
        return t.id


@pytest.mark.asyncio
async def test_reconciler_closes_long_on_stop_fill(session_factory):
    trade_id = await _insert_trade(
        session_factory, symbol="AAPL", broker_order_id="ord-1"
    )
    broker = _FakeBroker(
        {"ord-1": BracketFill(fill_price=95.0, trigger="STOP", child_order_id="leg-1")}
    )
    n = await BracketReconciler(
        broker=broker, session_factory=session_factory
    ).tick()
    assert n == 1
    async with session_factory() as s:
        t = await s.get(Trade, trade_id)
        assert t.status == TradeStatus.CLOSED
        assert t.exit_price == pytest.approx(95.0)
        assert t.broker_close_order_id == "leg-1"
        assert t.realized_pnl_usd == pytest.approx(-5.0)


@pytest.mark.asyncio
async def test_reconciler_closes_short_on_take_profit(session_factory):
    # Short @ 100, TP fill @ 94 → short gained 6% → +$6
    trade_id = await _insert_trade(
        session_factory,
        symbol="TSLA",
        broker_order_id="ord-2",
        action=TradeAction.OPEN_SHORT.value,
    )
    broker = _FakeBroker(
        {"ord-2": BracketFill(fill_price=94.0, trigger="TAKE_PROFIT", child_order_id="leg-2")}
    )
    n = await BracketReconciler(
        broker=broker, session_factory=session_factory
    ).tick()
    assert n == 1
    async with session_factory() as s:
        t = await s.get(Trade, trade_id)
        assert t.realized_pnl_usd == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_reconciler_no_fill_leaves_open(session_factory):
    trade_id = await _insert_trade(
        session_factory, symbol="AAPL", broker_order_id="ord-3"
    )
    broker = _FakeBroker()  # no fills
    n = await BracketReconciler(
        broker=broker, session_factory=session_factory
    ).tick()
    assert n == 0
    async with session_factory() as s:
        t = await s.get(Trade, trade_id)
        assert t.status == TradeStatus.OPEN


@pytest.mark.asyncio
async def test_reconciler_skips_options(session_factory):
    await _insert_trade(
        session_factory,
        symbol="AAPL",
        broker_order_id="ord-4",
        option_json={"legs": []},
    )
    # Broker would claim a fill if asked — reconciler must not ask.
    broker = _FakeBroker(
        {"ord-4": BracketFill(fill_price=90.0, trigger="STOP", child_order_id="x")}
    )
    n = await BracketReconciler(
        broker=broker, session_factory=session_factory
    ).tick()
    assert n == 0


@pytest.mark.asyncio
async def test_reconciler_skips_trades_without_order_id(session_factory):
    await _insert_trade(session_factory, symbol="AAPL", broker_order_id=None)
    broker = _FakeBroker()
    n = await BracketReconciler(
        broker=broker, session_factory=session_factory
    ).tick()
    assert n == 0
