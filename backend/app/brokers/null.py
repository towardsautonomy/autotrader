"""Safe no-op broker used when real credentials are placeholders.

Lets the API and UI boot cleanly in paper-mode before the user has filled
in Alpaca / Polymarket keys. Every method returns empty/zero state and
order submissions fail loudly so the scheduler knows not to pretend.
"""

from __future__ import annotations

import logging

from app.risk import Market, Position, TradeProposal

from .base import BrokerAdapter, OrderResult

logger = logging.getLogger(__name__)


class NullBroker(BrokerAdapter):
    def __init__(self, market: Market, reason: str) -> None:
        self._market = market
        self._reason = reason
        logger.warning(
            "NullBroker active for %s — %s. Fill in credentials to enable trading.",
            market.value,
            reason,
        )

    @property
    def market(self) -> Market:
        return self._market

    @property
    def paper_mode(self) -> bool:
        return True

    async def get_cash_balance(self) -> float:
        return 0.0

    async def get_positions(self) -> list[Position]:
        return []

    async def is_market_open(self) -> bool:
        return False

    async def get_price(self, symbol: str) -> float:
        return 0.0

    async def place_order(self, proposal: TradeProposal) -> OrderResult:
        return OrderResult(success=False, error=f"NullBroker: {self._reason}")

    async def close_position(self, symbol: str) -> OrderResult:
        return OrderResult(success=False, error=f"NullBroker: {self._reason}")
