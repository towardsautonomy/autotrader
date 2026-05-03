from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class LlmUsageRow(Base, TimestampMixin):
    """One row per LLM completion. Captures tokens + cost plus the full
    prompt/response pair (truncated) so the UI can expand any call to see
    exactly what the model saw and said.

    `cost_usd` is computed at log time against the then-active rate card;
    we store it rather than recomputing so historical rows remain stable
    when rates are edited later."""

    __tablename__ = "llm_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    purpose: Mapped[str | None] = mapped_column(String(64), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(64), index=True)
    round_idx: Mapped[int | None] = mapped_column(Integer)
    # `cycle_id` groups all LLM calls that belong to a single TradingLoop
    # tick — scout, research-<sym>, decision — so the UI can render the
    # swarm hierarchy for one decision cycle.
    cycle_id: Mapped[str | None] = mapped_column(String(64), index=True)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    prompt_messages: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSON)
