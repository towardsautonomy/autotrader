"""PendingReconciler — promotes PENDING trades on fill, cancels on reject.

Covers the case where TradingLoop wrote a row as PENDING (order accepted
without an immediate fill) and the broker later fills or rejects it.
Without this loop, such rows linger forever and slip past the runtime
monitor's stop/TP enforcement.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.base import BrokerAdapter, OrderFill
from app.models import Base, SystemState, Trade, TradeStatus
from app.risk import Market, Position
from app.scheduler import PendingReconciler


class _Broker(BrokerAdapter):
    def __init__(self, fills: dict[str, OrderFill]):
        self._fills = fills

    @property
    def market(self):
        return Market.STOCKS

    @property
    def paper_mode(self):
        return True

    async def get_cash_balance(self):
        return 1000.0

    async def get_positions(self):
        return []

    async def is_market_open(self):
        return True

    async def get_price(self, symbol):
        return 100.0

    async def place_order(self, proposal):
        raise NotImplementedError

    async def close_position(self, symbol):
        raise NotImplementedError

    async def get_order_fill(self, order_id):
        return self._fills.get(order_id)


@pytest.fixture
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(SystemState(id=1, trading_enabled=True))
        await s.commit()
    yield factory
    await engine.dispose()


async def _seed_pending(factory, broker_order_id: str = "ord-1") -> int:
    async with factory() as s:
        t = Trade(
            market=Market.STOCKS.value,
            symbol="SPY",
            action="open_long",
            size_usd=500.0,
            entry_price=None,
            status=TradeStatus.PENDING,
            broker_order_id=broker_order_id,
            paper_mode=True,
        )
        s.add(t)
        await s.commit()
        return t.id


async def test_pending_to_open_on_fill(session_factory):
    tid = await _seed_pending(session_factory)
    broker = _Broker({"ord-1": OrderFill(status="filled", fill_price=450.25)})
    rec = PendingReconciler(broker=broker, session_factory=session_factory)

    n = await rec.tick()

    assert n == 1
    async with session_factory() as s:
        t = await s.get(Trade, tid)
        assert t.status == TradeStatus.OPEN
        assert t.entry_price == pytest.approx(450.25)
        assert t.opened_at is not None


async def test_pending_stays_pending_when_broker_still_pending(session_factory):
    tid = await _seed_pending(session_factory)
    broker = _Broker({"ord-1": OrderFill(status="pending")})
    rec = PendingReconciler(broker=broker, session_factory=session_factory)

    n = await rec.tick()

    assert n == 0
    async with session_factory() as s:
        t = await s.get(Trade, tid)
        assert t.status == TradeStatus.PENDING
        assert t.entry_price is None
        assert t.opened_at is None


async def test_pending_to_canceled_on_broker_reject(session_factory):
    tid = await _seed_pending(session_factory)
    broker = _Broker({"ord-1": OrderFill(status="rejected")})
    rec = PendingReconciler(broker=broker, session_factory=session_factory)

    n = await rec.tick()

    assert n == 1
    async with session_factory() as s:
        t = await s.get(Trade, tid)
        assert t.status == TradeStatus.CANCELED
        assert t.closed_at is not None


async def test_pending_to_canceled_on_broker_cancel(session_factory):
    tid = await _seed_pending(session_factory)
    broker = _Broker({"ord-1": OrderFill(status="canceled")})
    rec = PendingReconciler(broker=broker, session_factory=session_factory)

    n = await rec.tick()

    assert n == 1
    async with session_factory() as s:
        t = await s.get(Trade, tid)
        assert t.status == TradeStatus.CANCELED


async def test_open_rows_untouched(session_factory):
    """Reconciler must only act on PENDING rows."""
    async with session_factory() as s:
        s.add(
            Trade(
                market=Market.STOCKS.value,
                symbol="SPY",
                action="open_long",
                size_usd=500.0,
                entry_price=450.0,
                status=TradeStatus.OPEN,
                broker_order_id="ord-live",
                paper_mode=True,
            )
        )
        await s.commit()

    broker = _Broker(
        {"ord-live": OrderFill(status="filled", fill_price=999.99)}
    )
    rec = PendingReconciler(broker=broker, session_factory=session_factory)
    assert await rec.tick() == 0

    async with session_factory() as s:
        rows = (await s.execute(select(Trade))).scalars().all()
        assert rows[0].entry_price == pytest.approx(450.0)
        assert rows[0].status == TradeStatus.OPEN


# ── close-fill back-fill ─────────────────────────────────────────────
# Regression: ``broker.close_position`` returns the just-submitted close
# order whose ``filled_avg_price`` is still None. Pre-fix, the caller
# stamped the row CLOSED with exit_price=NULL and pnl=0 forever. This
# reconciler must poll and back-fill the fill price + recompute P/L.


async def _seed_closed_awaiting_fill(
    factory,
    *,
    action: str = "open_short",
    entry_price: float = 80.0,
    size_usd: float = 3750.0,
    close_order_id: str = "close-1",
) -> int:
    async with factory() as s:
        t = Trade(
            market=Market.STOCKS.value,
            symbol="ASTS",
            action=action,
            size_usd=size_usd,
            entry_price=entry_price,
            status=TradeStatus.CLOSED,
            broker_order_id="open-1",
            broker_close_order_id=close_order_id,
            exit_price=None,
            realized_pnl_usd=0.0,
            paper_mode=True,
        )
        s.add(t)
        await s.commit()
        return t.id


async def test_closed_without_exit_price_is_backfilled(session_factory):
    tid = await _seed_closed_awaiting_fill(session_factory)
    broker = _Broker(
        {"close-1": OrderFill(status="filled", fill_price=78.5)}
    )
    rec = PendingReconciler(broker=broker, session_factory=session_factory)

    assert await rec.tick() == 1

    async with session_factory() as s:
        t = await s.get(Trade, tid)
        assert t.exit_price == pytest.approx(78.5)
        # short @ 80 → exit 78.5 = +1.875% of 3750 = ~+70.31 (before
        # paper-cost bps). Just check sign + nonzero.
        assert t.realized_pnl_usd is not None
        assert t.realized_pnl_usd > 0
        assert t.status == TradeStatus.CLOSED


async def test_closed_stays_null_while_broker_reports_pending(session_factory):
    tid = await _seed_closed_awaiting_fill(session_factory)
    broker = _Broker({"close-1": OrderFill(status="pending")})
    rec = PendingReconciler(broker=broker, session_factory=session_factory)

    assert await rec.tick() == 0

    async with session_factory() as s:
        t = await s.get(Trade, tid)
        assert t.exit_price is None
        assert t.realized_pnl_usd == 0.0


async def test_closed_with_exit_price_untouched(session_factory):
    """Rows that already have exit_price must be left alone."""
    async with session_factory() as s:
        t = Trade(
            market=Market.STOCKS.value,
            symbol="ASTS",
            action="open_short",
            size_usd=500.0,
            entry_price=78.35,
            exit_price=79.14,
            realized_pnl_usd=-5.29,
            status=TradeStatus.CLOSED,
            broker_order_id="open-1",
            broker_close_order_id="close-ok",
            paper_mode=True,
        )
        s.add(t)
        await s.commit()
        tid = t.id

    broker = _Broker(
        {"close-ok": OrderFill(status="filled", fill_price=999.99)}
    )
    rec = PendingReconciler(broker=broker, session_factory=session_factory)
    assert await rec.tick() == 0

    async with session_factory() as s:
        t = await s.get(Trade, tid)
        assert t.exit_price == pytest.approx(79.14)
        assert t.realized_pnl_usd == pytest.approx(-5.29)


async def test_option_close_rows_skipped(session_factory):
    """Option rows price from combo snapshots, not the parent order."""
    async with session_factory() as s:
        t = Trade(
            market=Market.STOCKS.value,
            symbol="SPY",
            action="open_long",
            size_usd=100.0,
            entry_price=2.5,
            exit_price=None,
            status=TradeStatus.CLOSED,
            broker_order_id="open-opt",
            broker_close_order_id="close-opt",
            option_json={"legs": [{"option_symbol": "SPY", "ratio": 1}]},
            paper_mode=True,
        )
        s.add(t)
        await s.commit()
        tid = t.id

    broker = _Broker(
        {"close-opt": OrderFill(status="filled", fill_price=5.0)}
    )
    rec = PendingReconciler(broker=broker, session_factory=session_factory)
    assert await rec.tick() == 0

    async with session_factory() as s:
        t = await s.get(Trade, tid)
        assert t.exit_price is None
