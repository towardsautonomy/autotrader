"""Multi-leg option execution: loop dispatches to place_multileg_order,
serializes legs to Trade.option_json, and closes via inverse legs."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.brokers.base import BrokerAdapter, OrderResult
from app.models import Base, SystemState, Trade, TradeStatus
from app.risk import (
    Market,
    OptionLeg,
    OptionProposal,
    OptionSide,
    OptionStructure,
    Position,
    RiskConfig,
    RiskEngine,
    RiskTier,
    TradeAction,
    TradeProposal,
)
from app.scheduler import TradingLoop
from app.strategies.base import Strategy, StrategyProposal


class FakeOptionsBroker(BrokerAdapter):
    def __init__(self):
        self._cash = 5000.0
        self._positions: list[Position] = []
        self.multileg_calls: list[TradeProposal] = []
        self.close_option_calls: list[dict] = []
        self.stock_calls: list[TradeProposal] = []
        self.simple_closes: list[str] = []

    @property
    def market(self):
        return Market.STOCKS

    @property
    def paper_mode(self):
        return True

    async def get_cash_balance(self):
        return self._cash

    async def get_positions(self):
        return list(self._positions)

    async def is_market_open(self):
        return True

    async def get_price(self, symbol):
        return 150.0

    async def place_order(self, proposal):
        self.stock_calls.append(proposal)
        return OrderResult(success=True, broker_order_id="stock-1", fill_price=150.0)

    async def close_position(self, symbol):
        self.simple_closes.append(symbol)
        return OrderResult(success=True, broker_order_id="close-1", fill_price=150.0)

    async def place_multileg_order(self, proposal):
        self.multileg_calls.append(proposal)
        return OrderResult(success=True, broker_order_id="mleg-1", fill_price=2.50)

    async def close_option_position(self, option_json):
        self.close_option_calls.append(option_json)
        return OrderResult(
            success=True, broker_order_id="mleg-close-1", fill_price=1.80
        )


def _make_vertical_debit_proposal() -> TradeProposal:
    legs = (
        OptionLeg(
            option_symbol="AAPL260515C00150000",
            side=OptionSide.CALL,
            strike=150.0,
            expiry="2026-05-15",
            ratio=+1,
            mid_price=5.00,
        ),
        OptionLeg(
            option_symbol="AAPL260515C00155000",
            side=OptionSide.CALL,
            strike=155.0,
            expiry="2026-05-15",
            ratio=-1,
            mid_price=2.50,
        ),
    )
    option = OptionProposal(
        structure=OptionStructure.VERTICAL_DEBIT,
        underlying="AAPL",
        legs=legs,
        net_debit_usd=250.0,
        max_loss_usd=250.0,
        max_gain_usd=250.0,
        expiry="2026-05-15",
    )
    return TradeProposal(
        market=Market.STOCKS,
        action=TradeAction.OPEN_LONG,
        symbol="AAPL",
        size_usd=250.0,
        rationale="bull call debit spread",
        option=option,
    )


class OptionOpenStrategy(Strategy):
    def __init__(self, proposal: TradeProposal):
        self._prop = proposal

    @property
    def market(self):
        return Market.STOCKS

    async def decide(self, snapshot):
        return StrategyProposal(
            market=Market.STOCKS,
            trade=self._prop,
            rationale=self._prop.rationale,
        )


class OptionCloseStrategy(Strategy):
    @property
    def market(self):
        return Market.STOCKS

    async def decide(self, snapshot):
        return StrategyProposal(
            market=Market.STOCKS,
            trade=TradeProposal(
                market=Market.STOCKS,
                action=TradeAction.CLOSE,
                symbol="AAPL",
                size_usd=0.0,
                rationale="flatten",
            ),
            rationale="flatten",
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


async def test_open_option_routes_to_multileg_and_persists_legs(session_factory):
    broker = FakeOptionsBroker()
    engine = RiskEngine(
        RiskConfig(
            budget_cap=5000,
            max_position_pct=0.10,
            risk_tier=RiskTier.AGGRESSIVE,
            max_option_loss_per_spread_pct=0.10,
        )
    )
    loop = TradingLoop(
        broker=broker,
        strategy=OptionOpenStrategy(_make_vertical_debit_proposal()),
        risk_engine=engine,
        session_factory=session_factory,
        respect_market_hours=False,
    )

    decision = await loop.tick()

    assert decision.approved is True
    assert decision.executed is True
    assert len(broker.multileg_calls) == 1
    assert broker.stock_calls == []

    async with session_factory() as s:
        trade = (await s.execute(Trade.__table__.select())).all()
        assert len(trade) == 1
        row = trade[0]._mapping
        assert row["symbol"] == "AAPL"
        assert row["option_json"] is not None
        assert row["option_json"]["structure"] == "vertical_debit"
        assert len(row["option_json"]["legs"]) == 2


async def test_close_option_uses_stored_legs(session_factory):
    broker = FakeOptionsBroker()
    engine = RiskEngine(
        RiskConfig(
            budget_cap=5000,
            max_position_pct=0.10,
            risk_tier=RiskTier.AGGRESSIVE,
            max_option_loss_per_spread_pct=0.10,
        )
    )

    # Open first.
    open_loop = TradingLoop(
        broker=broker,
        strategy=OptionOpenStrategy(_make_vertical_debit_proposal()),
        risk_engine=engine,
        session_factory=session_factory,
        respect_market_hours=False,
    )
    await open_loop.tick()

    # Now close.
    close_loop = TradingLoop(
        broker=broker,
        strategy=OptionCloseStrategy(),
        risk_engine=engine,
        session_factory=session_factory,
        respect_market_hours=False,
    )
    decision = await close_loop.tick()

    assert decision.approved is True
    assert decision.executed is True
    assert len(broker.close_option_calls) == 1
    assert broker.simple_closes == []
    sent = broker.close_option_calls[0]
    assert sent["structure"] == "vertical_debit"
    assert {l["option_symbol"] for l in sent["legs"]} == {
        "AAPL260515C00150000",
        "AAPL260515C00155000",
    }

    async with session_factory() as s:
        trades = (await s.execute(Trade.__table__.select())).all()
        assert len(trades) == 1
        row = trades[0]._mapping
        assert row["status"] == TradeStatus.CLOSED.value


async def test_alpaca_combo_limit_price_open_debit():
    from app.brokers.alpaca import _combo_limit_price

    opt = _make_vertical_debit_proposal().option
    price = _combo_limit_price(opt, closing=False)
    # signed_sum = +5.00 -2.50 = 2.50 debit. Padded +5% = 2.625 → round 2.62/2.63
    assert price is not None
    assert 2.5 < price <= 2.7


async def test_alpaca_combo_limit_price_close_inverts_sign():
    from app.brokers.alpaca import _combo_limit_price_from_json
    from app.scheduler.loop import _option_to_dict

    opt = _make_vertical_debit_proposal().option
    option_json = _option_to_dict(opt)
    price = _combo_limit_price_from_json(option_json, closing=True)
    # Open was +2.50 debit; close flips → -2.50 credit we'd accept; padded.
    assert price is not None
    assert price < 0
