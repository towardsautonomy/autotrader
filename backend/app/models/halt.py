from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Halt(Base, TimestampMixin):
    """Record of an automatic halt event.

    Daily-loss and drawdown trips insert rows here. UI shows halt history.
    """

    __tablename__ = "halts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    unpaused_by: Mapped[str | None] = mapped_column(String(128))
