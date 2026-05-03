"""Tests for ClaudeStockStrategy with a fake AI provider.

The real provider requires network + key; we swap in a stub that returns
canned tool_input dicts to exercise every branch of the translator.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ai.llm_provider import AIResponse
from app.risk import AccountSnapshot, Market, Position, RiskConfig, TradeAction
from app.strategies.claude_stocks import ClaudeStockStrategy


@dataclass
class StubProvider:
    tool_input: dict
    model: str = "test-model"
    provider: str = "stub"
    description: str = "stub::test-model"

    async def propose(self, *, system, user, max_tokens=1024):
        return AIResponse(
            tool_input=self.tool_input,
            raw_request={"system": system, "user": user},
            raw_response={"stub": True},
            model=self.model,
            provider=self.provider,
        )


class StubBroker:
    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    async def get_price(self, symbol):
        return self._prices.get(symbol, 0.0)


def make_snapshot():
    return AccountSnapshot(
        cash_balance=1000.0,
        positions=tuple(),
        day_realized_pnl=0.0,
        cumulative_pnl=0.0,
        daily_trade_count=0,
        trading_enabled=True,
    )


async def test_open_long_translated():
    provider = StubProvider(
        tool_input={
            "action": "open_long",
            "symbol": "spy",
            "size_usd": 75,
            "stop_loss_pct": 0.02,
            "take_profit_pct": 0.05,
            "rationale": "morning momentum",
            "confidence": 0.6,
        }
    )
    strategy = ClaudeStockStrategy(
        broker=StubBroker({"SPY": 500.0}),
        provider=provider,
        risk_config=RiskConfig(),

    )

    proposal = await strategy.decide(make_snapshot())
    assert proposal.trade is not None
    assert proposal.trade.action == TradeAction.OPEN_LONG
    assert proposal.trade.symbol == "SPY"
    assert proposal.trade.size_usd == 75
    assert proposal.trade.stop_loss_pct == 0.02
    assert proposal.trade.take_profit_pct == 0.05
    assert proposal.trade.confidence == 0.6
    assert proposal.model == "test-model"


async def test_hold_returns_no_trade():
    provider = StubProvider(
        tool_input={
            "action": "hold",
            "rationale": "no catalyst",
            "confidence": 0.2,
        }
    )
    strategy = ClaudeStockStrategy(
        broker=StubBroker({"SPY": 500.0}),
        provider=provider,
        risk_config=RiskConfig(),

    )

    proposal = await strategy.decide(make_snapshot())
    assert proposal.trade is None
    assert proposal.rationale == "no catalyst"


async def test_close_emits_close_action():
    provider = StubProvider(
        tool_input={
            "action": "close",
            "symbol": "SPY",
            "rationale": "hit resistance",
            "confidence": 0.5,
        }
    )
    snapshot = AccountSnapshot(
        cash_balance=950.0,
        positions=(
            Position(
                market=Market.STOCKS,
                symbol="SPY",
                size_usd=50.0,
                entry_price=500.0,
                current_price=505.0,
            ),
        ),
        day_realized_pnl=0.0,
        cumulative_pnl=0.0,
        daily_trade_count=1,
        trading_enabled=True,
    )
    strategy = ClaudeStockStrategy(
        broker=StubBroker({"SPY": 505.0}),
        provider=provider,
        risk_config=RiskConfig(),

    )
    proposal = await strategy.decide(snapshot)
    assert proposal.trade is not None
    assert proposal.trade.action == TradeAction.CLOSE
    assert proposal.trade.symbol == "SPY"


async def test_unknown_action_becomes_hold():
    provider = StubProvider(tool_input={"action": "lol", "rationale": "?", "confidence": 0.1})
    strategy = ClaudeStockStrategy(
        broker=StubBroker({}),
        provider=provider,
        risk_config=RiskConfig(),

    )
    proposal = await strategy.decide(make_snapshot())
    assert proposal.trade is None
    assert "unknown action" in proposal.rationale


async def test_open_without_symbol_becomes_hold():
    provider = StubProvider(
        tool_input={"action": "open_long", "rationale": "?", "confidence": 0.5}
    )
    strategy = ClaudeStockStrategy(
        broker=StubBroker({}),
        provider=provider,
        risk_config=RiskConfig(),

    )
    proposal = await strategy.decide(make_snapshot())
    assert proposal.trade is None
    assert "omitted symbol" in proposal.rationale
