"""Research chat persistence.

One ``ResearchConversation`` row per user thread, one ``ResearchMessage``
row per turn (user prompt, assistant response, and any tool call / tool
result pairs the agent emitted in between).

Tool traffic is stored in ``tool_payload`` as JSON so the UI can render a
collapsible transcript of what the agent did. Message ordering is by
``id`` — inserts are monotonic within a conversation.
"""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .base import Base, TimestampMixin


class ResearchConversation(Base, TimestampMixin):
    __tablename__ = "research_conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    # Denormalized summary field — first ticker mentioned, surfaced in the
    # conversation list so the user can scan threads by symbol.
    pinned_symbols: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ResearchMessage(Base, TimestampMixin):
    __tablename__ = "research_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("research_conversations.id"), nullable=False, index=True
    )
    # "user" | "assistant" | "tool_call" | "tool_result"
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # For tool_call/tool_result rows: name + args/result JSON.
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Link to llm_usage_rows when this message originated from a LLM call.
    call_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
