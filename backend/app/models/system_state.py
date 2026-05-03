from __future__ import annotations

from sqlalchemy import Boolean, Integer
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class SystemState(Base, TimestampMixin):
    """Singleton row (id=1) holding global runtime flags."""

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    trading_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    # When true, every scheduled loop (decision, scout, monitor) no-ops.
    # Distinct from trading_enabled, which only blocks order placement
    # but lets the AI keep researching.
    agents_paused: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # When true, the decision + scout loops skip their tick while the US
    # equity market is closed. Monitor still runs (EOD close / stop-loss
    # management). Different from ``agents_paused`` — this one re-enables
    # itself automatically on the next open.
    pause_when_market_closed: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    # Runtime mode flag. Source of truth for what the user _wants_ — the
    # broker instance is built at boot from settings.paper_mode, so a flip
    # here is "queued" until the next backend restart. We show the pending
    # state in the UI so the mismatch isn't hidden.
    paper_mode: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
