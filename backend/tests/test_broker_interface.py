"""Tests for the BrokerAdapter interface contract using a FakeBroker.

Integration tests against real Alpaca/Polymarket live under tests/integration/
and require credentials; they're opt-in via -m integration.
"""

from __future__ import annotations

import pytest

from app.brokers.base import BrokerAdapter, OrderResult
from app.risk import Market, Position, TradeAction, TradeProposal


class FakeBroker(BrokerAdapter):
    def __init__(self, market: Market = Market.STOCKS, paper: bool = True):
        self._market = market
        self._paper = paper
        self._cash = 1000.0
        self._positions: dict[str, Position] = {}
        self._order_counter = 0
        self._market_open = True

    @property
    def market(self) -> Market:
        return self._market

    @property
    def paper_mode(self) -> bool:
        return self._paper

    async def get_cash_balance(self) -> float:
        return self._cash

    async def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def is_market_open(self) -> bool:
        return self._market_open

    async def get_price(self, symbol: str) -> float:
        return 100.0

    async def place_order(self, proposal: TradeProposal) -> OrderResult:
        self._order_counter += 1
        self._positions[proposal.symbol] = Position(
            market=self._market,
            symbol=proposal.symbol,
            size_usd=proposal.size_usd,
            entry_price=100.0,
            current_price=100.0,
        )
        self._cash -= proposal.size_usd
        return OrderResult(
            success=True,
            broker_order_id=f"ord-{self._order_counter}",
            fill_price=100.0,
        )

    async def close_position(self, symbol: str) -> OrderResult:
        if symbol not in self._positions:
            return OrderResult(success=False, error="no position")
        self._order_counter += 1
        pos = self._positions.pop(symbol)
        self._cash += pos.size_usd + pos.unrealized_pnl
        return OrderResult(
            success=True,
            broker_order_id=f"close-{self._order_counter}",
            fill_price=pos.current_price,
        )


@pytest.fixture
def broker():
    return FakeBroker()


async def test_initial_cash(broker):
    assert await broker.get_cash_balance() == 1000.0


async def test_place_order_updates_cash_and_positions(broker):
    result = await broker.place_order(
        TradeProposal(
            market=Market.STOCKS,
            action=TradeAction.OPEN_LONG,
            symbol="SPY",
            size_usd=100.0,
        )
    )
    assert result.success
    assert result.broker_order_id == "ord-1"
    assert await broker.get_cash_balance() == 900.0
    positions = await broker.get_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "SPY"


async def test_close_position_returns_cash(broker):
    await broker.place_order(
        TradeProposal(
            market=Market.STOCKS,
            action=TradeAction.OPEN_LONG,
            symbol="AAPL",
            size_usd=200.0,
        )
    )
    result = await broker.close_position("AAPL")
    assert result.success
    assert await broker.get_cash_balance() == 1000.0
    assert await broker.get_positions() == []


async def test_close_nonexistent_fails(broker):
    result = await broker.close_position("ZZZ")
    assert not result.success
    assert result.error == "no position"


async def test_market_property(broker):
    assert broker.market == Market.STOCKS


async def test_paper_mode_property(broker):
    assert broker.paper_mode is True
