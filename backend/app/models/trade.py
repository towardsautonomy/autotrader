from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class TradeStatus(StrEnum):
    PENDING = "pending"       # order submitted, not filled yet
    OPEN = "open"             # filled, position live
    CLOSED = "closed"         # position closed
    CANCELED = "canceled"     # order canceled before fill
    REJECTED = "rejected"     # broker rejected


class Trade(Base, TimestampMixin):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int | None] = mapped_column(
        ForeignKey("decisions.id"), nullable=True
    )

    market: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    size_usd: Mapped[float] = mapped_column(Float, nullable=False)

    entry_price: Mapped[float | None] = mapped_column(Float)
    exit_price: Mapped[float | None] = mapped_column(Float)
    stop_loss_pct: Mapped[float | None] = mapped_column(Float)
    take_profit_pct: Mapped[float | None] = mapped_column(Float)

    realized_pnl_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=TradeStatus.PENDING, nullable=False
    )

    broker_order_id: Mapped[str | None] = mapped_column(String(128), index=True)
    broker_close_order_id: Mapped[str | None] = mapped_column(String(128))

    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    paper_mode: Mapped[bool] = mapped_column(default=True, nullable=False)

    # Option structures (multi-leg) serialize the leg spec here so the close
    # path can submit inverse-intent legs. None for plain stock trades.
    option_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    post_mortem_done: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
