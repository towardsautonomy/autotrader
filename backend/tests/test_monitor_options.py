"""Options stop-loss / take-profit enforcement in the runtime monitor.

Long/debit option structures expose us to premium decay and underlying
moves. Without a runtime mark-to-market check, an open combo could stay
open through a full paid-premium wipeout. The monitor pulls the current
per-contract net premium from the broker, compares against the stored
entry price, and fires ``close_option_position`` when the stop or
take-profit trips.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.base import BrokerAdapter, OrderResult
from app.models import Base, SystemState, Trade, TradeStatus
from app.risk import Market, Position, TradeAction
from app.scheduler.locks import reset_locks
from app.scheduler.monitor import RuntimeMonitor


class _FakeBroker(BrokerAdapter):
    def __init__(self, *, option_mark: float | None = None, close_ok: bool = True):
        self._option_mark = option_mark
        self._close_ok = close_ok
        self.close_calls: list[dict] = []

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
        return OrderResult(success=False, error="unexpected stock close")

    async def close_option_position(self, option_json: dict) -> OrderResult:
        self.close_calls.append(option_json)
        if not self._close_ok:
            return OrderResult(success=False, error="close failed")
        return OrderResult(
            success=True, broker_order_id="close-123", fill_price=self._option_mark
        )

    async def get_option_mark(self, option_json: dict) -> float | None:
        return self._option_mark


@pytest.fixture
async def session_factory():
    reset_locks()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as s:
        s.add(SystemState(id=1, trading_enabled=True, agents_paused=False))
        await s.commit()
    yield sf
    await engine.dispose()


async def _insert_open_option_trade(
    sf,
    *,
    entry_price: float,
    stop_loss_pct: float | None = 0.5,
    take_profit_pct: float | None = 1.0,
    option_json: dict | None = None,
) -> int:
    async with sf() as s:
        t = Trade(
            market=Market.STOCKS.value,
            symbol="AAPL",
            action=TradeAction.OPEN_LONG.value,
            size_usd=500.0,
            entry_price=entry_price,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            status=TradeStatus.OPEN,
            paper_mode=True,
            opened_at=datetime.now(UTC),
            broker_order_id="opt-abc",
            option_json=option_json or {"legs": [{"option_symbol": "X", "ratio": 1}]},
        )
        s.add(t)
        await s.commit()
        return t.id


async def _get_trade(sf, tid: int) -> Trade:
    async with sf() as s:
        return (
            await s.execute(select(Trade).where(Trade.id == tid))
        ).scalars().one()


@pytest.mark.asyncio
async def test_option_stop_fires_when_premium_halves(session_factory):
    # Paid $2.00 debit; mark collapses to $0.80 → pnl = -60% ≤ -50% stop.
    tid = await _insert_open_option_trade(session_factory, entry_price=2.00)
    broker = _FakeBroker(option_mark=0.80)
    closed = await RuntimeMonitor(broker, session_factory).tick()
    assert closed == 1
    assert len(broker.close_calls) == 1
    t = await _get_trade(session_factory, tid)
    assert t.status == TradeStatus.CLOSED
    assert t.exit_price == pytest.approx(0.80)
    # Realized pnl signed-aware: long debit, mark below entry → loss.
    assert t.realized_pnl_usd is not None and t.realized_pnl_usd < 0


@pytest.mark.asyncio
async def test_option_take_profit_fires(session_factory):
    # Paid $1.00, mark doubles to $2.10 → pnl +110% ≥ +100% take-profit.
    tid = await _insert_open_option_trade(session_factory, entry_price=1.00)
    broker = _FakeBroker(option_mark=2.10)
    closed = await RuntimeMonitor(broker, session_factory).tick()
    assert closed == 1
    t = await _get_trade(session_factory, tid)
    assert t.status == TradeStatus.CLOSED
    assert t.realized_pnl_usd is not None and t.realized_pnl_usd > 0


@pytest.mark.asyncio
async def test_option_within_bounds_stays_open(session_factory):
    # Paid $2.00, mark down to $1.20 → pnl -40%, still above -50% stop.
    tid = await _insert_open_option_trade(session_factory, entry_price=2.00)
    broker = _FakeBroker(option_mark=1.20)
    closed = await RuntimeMonitor(broker, session_factory).tick()
    assert closed == 0
    assert broker.close_calls == []
    t = await _get_trade(session_factory, tid)
    assert t.status == TradeStatus.OPEN


@pytest.mark.asyncio
async def test_option_no_mark_leaves_open(session_factory):
    # Broker can't price the combo → monitor must not force-close.
    tid = await _insert_open_option_trade(session_factory, entry_price=2.00)
    broker = _FakeBroker(option_mark=None)
    closed = await RuntimeMonitor(broker, session_factory).tick()
    assert closed == 0
    assert broker.close_calls == []
    t = await _get_trade(session_factory, tid)
    assert t.status == TradeStatus.OPEN


@pytest.mark.asyncio
async def test_option_close_failure_leaves_open(session_factory):
    # Mark trips stop, broker close errors → row must remain OPEN so a
    # later tick can retry rather than desync DB from broker state.
    tid = await _insert_open_option_trade(session_factory, entry_price=2.00)
    broker = _FakeBroker(option_mark=0.50, close_ok=False)
    closed = await RuntimeMonitor(broker, session_factory).tick()
    assert closed == 0
    t = await _get_trade(session_factory, tid)
    assert t.status == TradeStatus.OPEN


@pytest.mark.asyncio
async def test_non_positive_entry_is_skipped(session_factory):
    # Credit structure (entry_price <= 0) not supported — skip instead of
    # computing a nonsense pnl_pct against a zero/negative denominator.
    tid = await _insert_open_option_trade(session_factory, entry_price=0.0)
    broker = _FakeBroker(option_mark=5.0)
    closed = await RuntimeMonitor(broker, session_factory).tick()
    assert closed == 0
    t = await _get_trade(session_factory, tid)
    assert t.status == TradeStatus.OPEN
