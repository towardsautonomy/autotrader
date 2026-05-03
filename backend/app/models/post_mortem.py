from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class TradePostMortem(Base, TimestampMixin):
    """One LLM-written lesson per closed trade.

    Surfaced back into future decision prompts so the agent can avoid
    repeating avoidable mistakes (bad entries, poor exits, missed catalysts).
    """

    __tablename__ = "trade_post_mortems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trade_id: Mapped[int] = mapped_column(
        ForeignKey("trades.id"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    lesson: Mapped[str] = mapped_column(Text, nullable=False)
    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    call_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
