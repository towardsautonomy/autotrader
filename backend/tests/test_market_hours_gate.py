"""Market-hours gate — strategy runs 24/7, broker call is skipped off-hours.

The loop now lets the strategy produce a proposal and logs a Decision with
rejection_code="market_closed" when the market is closed, rather than
skipping the entire tick. This preserves research/agent activity off-hours.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.base import BrokerAdapter, OrderResult
from app.clock import is_us_equities_regular_session
from app.models import Base, SystemState
from app.risk import (
    Market,
    Position,
    RiskConfig,
    RiskEngine,
    TradeAction,
    TradeProposal,
)
from app.scheduler.loop import TradingLoop
from app.scheduler.scout import ScoutLoop
from app.scheduler.candidate_queue import CandidateQueue
from app.strategies.base import Strategy, StrategyProposal


def test_regular_session_true_during_hours():
    # Wed 2026-05-20 at 10:30 America/New_York = 14:30 UTC.
    when = datetime(2026, 5, 20, 14, 30, tzinfo=UTC)
    assert is_us_equities_regular_session(when) is True


def test_regular_session_false_on_weekend():
    # Sat 2026-05-23 at 14:30 UTC.
    when = datetime(2026, 5, 23, 14, 30, tzinfo=UTC)
    assert is_us_equities_regular_session(when) is False


def test_regular_session_false_before_open():
    # Wed 2026-05-20 at 07:00 ET = 11:00 UTC (pre-market).
    when = datetime(2026, 5, 20, 11, 0, tzinfo=UTC)
    assert is_us_equities_regular_session(when) is False


class ClosedMarketBroker(BrokerAdapter):
    def __init__(self):
        self.orders: list[TradeProposal] = []

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
        return False

    async def get_price(self, symbol):
        return 500.0

    async def place_order(self, proposal):
        self.orders.append(proposal)
        return OrderResult(success=True, broker_order_id="should-not-happen")

    async def close_position(self, symbol):
        return OrderResult(success=False, error="should-not-happen")


class BuyStrategy(Strategy):
    def __init__(self):
        self.called = 0

    @property
    def market(self):
        return Market.STOCKS

    async def decide(self, snapshot):
        self.called += 1
        return StrategyProposal(
            market=Market.STOCKS,
            trade=TradeProposal(
                market=Market.STOCKS,
                action=TradeAction.OPEN_LONG,
                symbol="SPY",
                size_usd=50.0,
            ),
            rationale="buy the dip",
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


@pytest.mark.asyncio
async def test_trading_loop_runs_strategy_and_skips_order_when_market_closed(
    session_factory,
):
    """Strategy still runs (research continues 24/7); broker call is skipped
    and Decision is logged with rejection_code=market_closed."""
    broker = ClosedMarketBroker()
    strategy = BuyStrategy()
    loop = TradingLoop(
        broker=broker,
        strategy=strategy,
        risk_engine=RiskEngine(RiskConfig(budget_cap=1000, max_position_pct=0.10)),
        session_factory=session_factory,
        respect_market_hours=True,
    )

    decision = await loop.tick()

    assert decision is not None
    assert strategy.called == 1, "strategy must run even when market is closed"
    assert decision.approved is True
    assert decision.executed is False
    assert decision.rejection_code == "market_closed"
    assert broker.orders == [], "no order must be submitted off-hours"


class BrokenClockBroker(ClosedMarketBroker):
    """Mimics an Alpaca clock endpoint blowing up mid-tick."""

    async def is_market_open(self):  # type: ignore[override]
        raise RuntimeError("clock endpoint 500")


@pytest.mark.asyncio
async def test_broker_clock_failure_fails_closed(session_factory):
    """An erroring clock endpoint must be treated as closed.

    If the broker's ``is_market_open`` raises (network blip / 500), the
    loop treats the market as closed — fail-open would let orders fire
    during uncertain off-hours conditions, which violates the safety
    contract. Fail-closed pauses until the next successful check.
    """
    broker = BrokenClockBroker()
    strategy = BuyStrategy()
    loop = TradingLoop(
        broker=broker,
        strategy=strategy,
        risk_engine=RiskEngine(RiskConfig(budget_cap=1000, max_position_pct=0.10)),
        session_factory=session_factory,
        respect_market_hours=True,
    )

    decision = await loop.tick()

    assert decision is not None
    assert decision.rejection_code == "market_closed"
    assert broker.orders == []


@pytest.mark.asyncio
async def test_scout_loop_runs_when_market_closed(monkeypatch):
    """Scout loop no longer gates on market hours — research runs 24/7."""
    monkeypatch.setattr(
        "app.scheduler.scout.is_us_equities_regular_session", lambda: False
    )
    queue = CandidateQueue()
    # Default respect_market_hours=False — scout runs regardless of hours.
    scout = ScoutLoop(queue=queue)
    # With no movers source wired up, tick is a no-op but must not error.
    added = await scout.tick()
    assert added == 0
