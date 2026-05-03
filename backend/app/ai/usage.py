"""Token + cost accounting for LLM calls.

Every ``AIResponse`` gets priced against the currently-active rate card
and persisted to the ``llm_usage`` table. Full prompt + response bodies
are stored alongside (truncated at a safe cap) so the UI can expand any
call to show exactly what the model received and produced.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import get_bus
from app.ai.llm_provider import AIResponse
from app.ai.trace import get_cycle_id
from app.models import LlmRateCardRow, LlmUsageRow

logger = logging.getLogger(__name__)

# Per-call storage cap for prompt + response bodies. SQLite handles large
# JSON fine, but keeping rows bounded prevents one runaway call from
# blowing up the DB. 64 KB is enough for ~8k tokens of text.
_BODY_CAP_BYTES = 64 * 1024


def _truncate_jsonable(obj: Any, cap: int = _BODY_CAP_BYTES) -> Any:
    """Ensure a JSON-serializable payload fits under the cap.

    If the full object serializes under the cap, return it unchanged.
    Otherwise return a truncation marker plus a head/tail string slice
    so a human can still eyeball what was there.
    """
    if obj is None:
        return None
    try:
        text = json.dumps(obj, default=str)
    except Exception:
        text = str(obj)
    if len(text) <= cap:
        return obj
    head = text[: cap // 2]
    tail = text[-cap // 4 :]
    return {
        "_truncated": True,
        "_original_bytes": len(text),
        "head": head,
        "tail": tail,
    }


async def log_usage(
    factory: async_sessionmaker,
    response: AIResponse,
    *,
    purpose: str | None = None,
    decision_id: int | None = None,
    agent_id: str | None = None,
    round_idx: int | None = None,
    prompt_messages: list[dict[str, Any]] | None = None,
    cycle_id: str | None = None,
) -> LlmUsageRow:
    """Compute cost, persist a usage row, publish `ai.usage`.

    ``prompt_messages`` override the messages extracted from
    ``response.raw_request`` so callers that assemble messages mid-loop
    can pass the exact snapshot that produced this response.

    ``cycle_id`` defaults to whatever ``ai.trace.set_cycle_id`` has on
    the current asyncio context — callers inside a TradingLoop tick get
    it for free.
    """
    messages_to_store = prompt_messages
    if messages_to_store is None:
        raw_req = response.raw_request or {}
        messages_to_store = raw_req.get("messages") or []

    if cycle_id is None:
        cycle_id = get_cycle_id()

    async with factory() as s:
        card = (
            await s.execute(
                select(LlmRateCardRow)
                .where(LlmRateCardRow.is_active.is_(True))
                .order_by(desc(LlmRateCardRow.id))
                .limit(1)
            )
        ).scalars().first()
        prompt_rate, completion_rate = (
            card.price(response.provider, response.model) if card else (0.0, 0.0)
        )
        cost = (
            response.prompt_tokens / 1000.0 * prompt_rate
            + response.completion_tokens / 1000.0 * completion_rate
        )
        row = LlmUsageRow(
            provider=response.provider,
            model=response.model,
            purpose=purpose,
            agent_id=agent_id,
            round_idx=round_idx,
            cycle_id=cycle_id,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            total_tokens=response.total_tokens,
            cost_usd=cost,
            decision_id=decision_id,
            prompt_messages=_truncate_jsonable(messages_to_store),
            response_body=_truncate_jsonable(response.raw_response),
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)

    get_bus().publish(
        "ai.usage",
        f"{response.provider}::{response.model} "
        f"{response.total_tokens}tok ${cost:.4f}",
        data={
            "call_id": row.id,
            "provider": response.provider,
            "model": response.model,
            "purpose": purpose,
            "agent_id": agent_id,
            "round_idx": round_idx,
            "cycle_id": cycle_id,
            "prompt_tokens": response.prompt_tokens,
            "completion_tokens": response.completion_tokens,
            "total_tokens": response.total_tokens,
            "cost_usd": cost,
        },
    )
    return row


__all__ = ["log_usage"]
