from __future__ import annotations

from sqlalchemy import JSON, Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin


class Decision(Base, TimestampMixin):
    """One AI decision cycle — whether or not it resulted in a trade.

    We log everything (prompt, response, validation verdict) so the user can
    audit why the AI made each call after the fact.
    """

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    market: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)

    # Raw Claude I/O for full auditability
    prompt_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    response_json: Mapped[dict | None] = mapped_column(JSON)
    rationale: Mapped[str | None] = mapped_column(Text)

    # Parsed proposal (None if AI declined to trade)
    proposal_json: Mapped[dict | None] = mapped_column(JSON)

    # Risk engine verdict
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rejection_code: Mapped[str | None] = mapped_column(String(64))
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    # Outcome
    executed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    execution_error: Mapped[str | None] = mapped_column(Text)

    # Research trail — list of {tool, arguments, result_preview} dicts
    # captured during the agentic loop (web_search / fetch_url calls).
    research_json: Mapped[list | None] = mapped_column(JSON)

    # Links this decision to every LLM call that ran in the same cycle
    # (scout, research-<sym>, decision). The UI joins on this to render
    # per-cycle swarm hierarchies.
    cycle_id: Mapped[str | None] = mapped_column(String(64), index=True)
