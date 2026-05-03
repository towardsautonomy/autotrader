"""Post-mortem agent — one tiny LLM call per closed trade.

Writes a structured lesson (verdict + one-sentence takeaway) so the
decision prompt can surface recent lessons and avoid repeated mistakes.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import EventSeverity, get_bus
from app.ai.llm_provider import LLMProvider
from app.ai.usage import log_usage

logger = logging.getLogger(__name__)


RECORD_LESSON_TOOL = {
    "type": "function",
    "function": {
        "name": "record_lesson",
        "description": (
            "Record a terse post-mortem for a just-closed trade. One call "
            "only. Be direct — future you reads this before opening new "
            "positions."
        ),
        "parameters": {
            "type": "object",
            "required": ["verdict", "lesson"],
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": [
                        "good_trade",
                        "good_luck",
                        "bad_entry",
                        "bad_exit",
                        "thesis_wrong",
                        "noise",
                    ],
                },
                "lesson": {
                    "type": "string",
                    "description": (
                        "One or two sentences. What went right or wrong, and "
                        "the specific pattern to repeat or avoid. Cite the "
                        "catalyst/news/price-move that drove the outcome."
                    ),
                },
            },
        },
    },
}

SYSTEM_PROMPT = (
    "You are the POST-MORTEM agent. You write short, honest reviews of "
    "closed trades. Never hedge — 'bad_entry' if the entry was bad, "
    "'good_luck' if we got paid for the wrong reason, 'thesis_wrong' if "
    "the directional read was off. The goal is a sharp lesson the decision "
    "agent can read next cycle. Call record_lesson ONCE."
)


@dataclass
class PostMortemOutcome:
    verdict: str
    lesson: str
    call_id: int | None = None
    elapsed_sec: float = 0.0
    error: str | None = None


@dataclass
class TradeSummary:
    """Compact closed-trade summary passed to the post-mortem agent."""

    trade_id: int
    symbol: str
    action: str  # "open_long" / "open_short"
    size_usd: float
    entry_price: float | None
    exit_price: float | None
    stop_loss_pct: float | None
    take_profit_pct: float | None
    realized_pnl_usd: float
    hold_minutes: float | None
    option_structure: str | None
    entry_rationale: str | None


class PostMortemAgent:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        session_factory: async_sessionmaker | None = None,
        max_tokens: int = 512,
        agent_id: str = "post-mortem",
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory
        self._max_tokens = max_tokens
        self._agent_id = agent_id

    async def review(self, summary: TradeSummary) -> PostMortemOutcome:
        bus = get_bus()
        started = time.monotonic()

        user_msg = _format_trade_prompt(summary)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        try:
            response = await self._provider.raw_completion(
                messages=messages,
                tools=[RECORD_LESSON_TOOL],
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            bus.publish(
                "post_mortem.failed",
                f"[{summary.symbol}] LLM error: {exc}",
                severity=EventSeverity.WARN,
                data={"trade_id": summary.trade_id, "error": str(exc)},
            )
            return PostMortemOutcome(
                verdict="noise",
                lesson="",
                elapsed_sec=time.monotonic() - started,
                error=str(exc),
            )

        call_id: int | None = None
        if self._session_factory is not None:
            try:
                row = await log_usage(
                    self._session_factory,
                    response,
                    purpose="post_mortem",
                    agent_id=self._agent_id,
                    round_idx=0,
                    prompt_messages=list(messages),
                )
                call_id = row.id
            except Exception:
                logger.exception("failed to persist post-mortem call row")

        verdict = "noise"
        lesson = ""
        choice = response.raw_response.get("choices", [{}])[0]
        msg = choice.get("message") or {}
        for call in msg.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name")
            if name != "record_lesson":
                continue
            args_raw = (call.get("function") or {}).get("arguments") or "{}"
            try:
                payload = (
                    json.loads(args_raw)
                    if isinstance(args_raw, str)
                    else dict(args_raw)
                )
            except Exception:
                continue
            verdict = str(payload.get("verdict") or "noise").strip().lower()
            lesson = str(payload.get("lesson") or "").strip()[:800]
            break

        elapsed = time.monotonic() - started
        bus.publish(
            "post_mortem.done",
            f"[{summary.symbol}] verdict={verdict} — {lesson[:120]}",
            severity=EventSeverity.INFO,
            data={
                "trade_id": summary.trade_id,
                "symbol": summary.symbol,
                "verdict": verdict,
                "pnl_usd": summary.realized_pnl_usd,
                "elapsed_sec": elapsed,
            },
        )
        return PostMortemOutcome(
            verdict=verdict,
            lesson=lesson,
            call_id=call_id,
            elapsed_sec=elapsed,
        )


def _format_trade_prompt(s: TradeSummary) -> str:
    lines: list[str] = []
    lines.append(f"Closed trade review — {s.symbol}")
    lines.append(f"  action: {s.action}")
    lines.append(f"  size_usd: ${s.size_usd:.2f}")
    if s.entry_price is not None and s.exit_price is not None:
        move_pct = (
            (s.exit_price - s.entry_price) / s.entry_price * 100.0
            if s.entry_price
            else 0.0
        )
        lines.append(
            f"  prices: entry=${s.entry_price:.2f} → exit=${s.exit_price:.2f} "
            f"({move_pct:+.2f}%)"
        )
    if s.stop_loss_pct is not None:
        lines.append(f"  stop_loss_pct: {s.stop_loss_pct * 100:.2f}%")
    if s.take_profit_pct is not None:
        lines.append(f"  take_profit_pct: {s.take_profit_pct * 100:.2f}%")
    lines.append(f"  realized_pnl: ${s.realized_pnl_usd:+.2f}")
    if s.hold_minutes is not None:
        lines.append(f"  held: {s.hold_minutes:.1f} minutes")
    if s.option_structure:
        lines.append(f"  option_structure: {s.option_structure}")
    if s.entry_rationale:
        lines.append("")
        lines.append("Original entry rationale:")
        lines.append(f"  {s.entry_rationale[:800]}")
    lines.append("")
    lines.append(
        "Call record_lesson with the verdict and a terse 1–2 sentence "
        "lesson. Be direct."
    )
    return "\n".join(lines)


__all__ = [
    "PostMortemAgent",
    "PostMortemOutcome",
    "TradeSummary",
    "RECORD_LESSON_TOOL",
]
