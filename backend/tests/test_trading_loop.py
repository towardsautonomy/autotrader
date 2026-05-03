"""End-to-end test of TradingLoop against FakeBroker + DummyStrategy."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.base import BrokerAdapter, OrderResult
from app.models import Base, Decision, SystemState, Trade
from app.risk import Market, Position, RiskConfig, RiskEngine, TradeAction, TradeProposal
from app.scheduler import TradingLoop
from app.strategies import DummySpyStrategy
from app.strategies.base import Strategy, StrategyProposal


class FakeBroker(BrokerAdapter):
    def __init__(self):
        self._cash = 1000.0
        self._positions = {}
        self.orders: list[TradeProposal] = []

    @property
    def market(self):
        return Market.STOCKS

    @property
    def paper_mode(self):
        return True

    async def get_cash_balance(self):
        return self._cash

    async def get_positions(self):
        return list(self._positions.values())

    async def is_market_open(self):
        return True

    async def get_price(self, symbol):
        return 500.0

    async def place_order(self, proposal):
        self.orders.append(proposal)
        self._positions[proposal.symbol] = Position(
            market=Market.STOCKS,
            symbol=proposal.symbol,
            size_usd=proposal.size_usd,
            entry_price=500.0,
            current_price=500.0,
        )
        self._cash -= proposal.size_usd
        return OrderResult(success=True, broker_order_id="ord-1", fill_price=500.0)

    async def close_position(self, symbol):
        pos = self._positions.pop(symbol, None)
        if pos is None:
            return OrderResult(success=False, error="missing")
        self._cash += pos.size_usd
        return OrderResult(success=True, broker_order_id="close-1", fill_price=500.0)


class AlwaysRejectStrategy(Strategy):
    @property
    def market(self):
        return Market.STOCKS

    async def decide(self, snapshot):
        return StrategyProposal(
            market=Market.STOCKS,
            trade=TradeProposal(
                market=Market.STOCKS,
                action=TradeAction.OPEN_LONG,
                symbol="SPY",
                size_usd=99999.0,  # far over budget
            ),
            rationale="oversized on purpose",
        )


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


async def test_tick_executes_dummy_trade(session_factory):
    broker = FakeBroker()
    engine = RiskEngine(RiskConfig(budget_cap=1000, max_position_pct=0.10))
    loop = TradingLoop(
        broker=broker,
        strategy=DummySpyStrategy(size_usd=50),
        risk_engine=engine,
        session_factory=session_factory,
    )

    decision = await loop.tick()

    assert decision.approved is True
    assert decision.executed is True
    assert len(broker.orders) == 1
    assert broker.orders[0].symbol == "SPY"

    async with session_factory() as s:
        trades = (await s.execute(Trade.__table__.select())).all()
        assert len(trades) == 1
        decisions = (await s.execute(Decision.__table__.select())).all()
        assert len(decisions) == 1


async def test_tick_no_op_when_position_exists(session_factory):
    broker = FakeBroker()
    # Pre-seed SPY position so dummy strategy decides to hold
    broker._positions["SPY"] = Position(
        market=Market.STOCKS,
        symbol="SPY",
        size_usd=50.0,
        entry_price=500.0,
        current_price=500.0,
    )
    engine = RiskEngine(RiskConfig())
    loop = TradingLoop(
        broker=broker,
        strategy=DummySpyStrategy(size_usd=50),
        risk_engine=engine,
        session_factory=session_factory,
    )

    decision = await loop.tick()
    assert decision.approved is False
    assert decision.rejection_code == "strategy_no_op"
    assert broker.orders == []


async def test_tick_rejected_trade_logged_but_not_executed(session_factory):
    broker = FakeBroker()
    engine = RiskEngine(RiskConfig(budget_cap=1000, max_position_pct=0.10))
    loop = TradingLoop(
        broker=broker,
        strategy=AlwaysRejectStrategy(),
        risk_engine=engine,
        session_factory=session_factory,
    )

    decision = await loop.tick()

    assert decision.approved is False
    assert decision.executed is False
    assert decision.rejection_code == "per_trade_max_exceeded"
    assert broker.orders == []


class HangingStrategy(Strategy):
    """Sleeps longer than the loop's decide_timeout, simulating a hung LLM."""

    @property
    def market(self):
        return Market.STOCKS

    async def decide(self, snapshot):
        await asyncio.sleep(5.0)
        raise AssertionError("should have been cancelled by wait_for")


async def test_tick_decide_timeout_records_no_op(session_factory):
    broker = FakeBroker()
    engine = RiskEngine(RiskConfig())
    loop = TradingLoop(
        broker=broker,
        strategy=HangingStrategy(),
        risk_engine=engine,
        session_factory=session_factory,
        decide_timeout_sec=0.05,
    )

    decision = await loop.tick()

    assert decision is not None
    assert decision.approved is False
    assert decision.executed is False
    assert decision.rejection_code == "decide_timeout"
    # Must not have held the broker lock indefinitely / placed any order.
    assert broker.orders == []


async def test_tick_respects_kill_switch(session_factory):
    # Flip kill switch
    async with session_factory() as s:
        state = await s.get(SystemState, 1)
        state.trading_enabled = False
        await s.commit()

    broker = FakeBroker()
    engine = RiskEngine(RiskConfig())
    loop = TradingLoop(
        broker=broker,
        strategy=DummySpyStrategy(),
        risk_engine=engine,
        session_factory=session_factory,
    )
    decision = await loop.tick()
    assert decision.approved is False
    assert decision.rejection_code == "kill_switch"
    assert broker.orders == []
