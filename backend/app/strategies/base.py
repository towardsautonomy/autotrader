from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.risk import AccountSnapshot, Market, TradeProposal


@dataclass(frozen=True, slots=True)
class StrategyProposal:
    """What the strategy decided this cycle.

    A strategy may return no proposal (e.g., AI chose to hold); in that case
    `trade` is None and `rationale` explains the no-op. The scheduler still
    logs the decision so there's a record that the cycle ran.
    """

    market: Market
    trade: TradeProposal | None
    rationale: str
    raw_prompt: dict | None = None
    raw_response: dict | None = None
    model: str = "strategy"
    research: list[dict[str, Any]] = field(default_factory=list)


class Strategy(ABC):
    @property
    @abstractmethod
    def market(self) -> Market: ...

    @abstractmethod
    async def decide(self, snapshot: AccountSnapshot) -> StrategyProposal: ...
