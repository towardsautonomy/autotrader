from __future__ import annotations

from sqlalchemy import JSON, Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class LlmRateCardRow(Base, TimestampMixin):
    """Editable price table for LLM providers/models. Active row wins.

    `rates` is a JSON dict keyed by "provider::model" with values:
        {"prompt_per_1k_usd": 0.003, "completion_per_1k_usd": 0.015}

    Local/self-hosted models (LM Studio, Ollama) should be priced at 0 —
    we still log token counts so the UI can show throughput, just not cost.
    """

    __tablename__ = "llm_rate_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, index=True
    )
    rates: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    changed_by: Mapped[str | None] = mapped_column(String(128))

    def price(self, provider: str, model: str) -> tuple[float, float]:
        """(prompt_per_1k_usd, completion_per_1k_usd). Falls back through
        wildcards so a new model name doesn't silently price at $0.

        Lookup order:
          1. exact ``provider::model``
          2. provider wildcard ``provider::*``
          3. legacy ``provider::local-model`` (back-compat — older seeded
             rate cards used this as a lmstudio catch-all)
          4. (0.0, 0.0)
        """
        rates = self.rates or {}
        entry = (
            rates.get(f"{provider}::{model}")
            or rates.get(f"{provider}::*")
            or rates.get(f"{provider}::local-model")
            or {}
        )
        return (
            float(entry.get("prompt_per_1k_usd") or 0.0),
            float(entry.get("completion_per_1k_usd") or 0.0),
        )


DEFAULT_RATES = {
    # Sonnet 4.5 via OpenRouter — input $3/M, output $15/M as of 2026-04.
    "openrouter::anthropic/claude-sonnet-4.5": {
        "prompt_per_1k_usd": 0.003,
        "completion_per_1k_usd": 0.015,
    },
    "openrouter::anthropic/claude-opus-4": {
        "prompt_per_1k_usd": 0.015,
        "completion_per_1k_usd": 0.075,
    },
    "openrouter::anthropic/claude-haiku-4.5": {
        "prompt_per_1k_usd": 0.0008,
        "completion_per_1k_usd": 0.004,
    },
    # LM Studio / local — notional cost so usage is visible in the UI.
    # Any lmstudio model resolves to this wildcard regardless of weight.
    "lmstudio::*": {
        "prompt_per_1k_usd": 0.001,
        "completion_per_1k_usd": 0.01,
    },
    # Kept for back-compat with older rate cards that seeded this key.
    "lmstudio::local-model": {
        "prompt_per_1k_usd": 0.001,
        "completion_per_1k_usd": 0.01,
    },
}
