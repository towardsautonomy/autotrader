"""Dummy strategy used to prove plumbing end-to-end before the AI layer.

Behavior: if no SPY position open, propose buying $50 of SPY. Otherwise
propose holding. Useful for integration-testing the scheduler against
Alpaca paper.
"""

from __future__ import annotations

from app.risk import AccountSnapshot, Market, TradeAction, TradeProposal

from .base import Strategy, StrategyProposal


class DummySpyStrategy(Strategy):
    def __init__(self, symbol: str = "SPY", size_usd: float = 50.0) -> None:
        self._symbol = symbol
        self._size_usd = size_usd

    @property
    def market(self) -> Market:
        return Market.STOCKS

    async def decide(self, snapshot: AccountSnapshot) -> StrategyProposal:
        existing = snapshot.find_position(Market.STOCKS, self._symbol)
        if existing is not None:
            return StrategyProposal(
                market=Market.STOCKS,
                trade=None,
                rationale=f"already long {self._symbol}, hold",
            )

        return StrategyProposal(
            market=Market.STOCKS,
            trade=TradeProposal(
                market=Market.STOCKS,
                action=TradeAction.OPEN_LONG,
                symbol=self._symbol,
                size_usd=self._size_usd,
                rationale="dummy: always buy SPY once",
            ),
            rationale="open SPY",
        )
