from __future__ import annotations

from sqlalchemy import JSON, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class ActivityEventRow(Base, TimestampMixin):
    """Persistent copy of every activity event the bus publishes.

    Lets the frontend backfill recent history on page load and gives ops
    a durable audit trail of everything the scheduler did.
    """

    __tablename__ = "activity_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict | None] = mapped_column(JSON)
