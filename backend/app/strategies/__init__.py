from .base import Strategy, StrategyProposal
from .claude_stocks import ClaudeStockStrategy
from .dummy import DummySpyStrategy

__all__ = [
    "ClaudeStockStrategy",
    "DummySpyStrategy",
    "Strategy",
    "StrategyProposal",
]
