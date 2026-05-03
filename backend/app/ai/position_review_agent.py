"""Position-review agent — fast-cadence news-driven exit scanner.

Runs on its own (short) interval, INDEPENDENT of the main decision loop.
Its single job: look at every open position alongside the freshest news
and quote, and decide hold / close / tighten_stop. Speed matters — when
a headline turns an open thesis into a sell, we can't wait until the
next 5-minute decision tick.

Design:
  - One LLM round per tick. The model is told which symbols we hold and
    what tools it has; it emits one ``review_position`` call per symbol
    in parallel (single round, N tool calls). Parallel tool calls scale
    far better than N sequential LLM calls.
  - Defined-risk only: ``close`` triggers a broker close; ``tighten_stop``
    updates ``Trade.stop_loss_pct`` so the runtime monitor auto-exits on
    the tightened threshold; ``hold`` is the no-op.
  - The agent never *opens* positions. Opens go through the slow decision
    loop with risk-engine validation.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import EventSeverity, get_bus
from app.ai.llm_provider import LLMProvider
from app.ai.usage import log_usage

logger = logging.getLogger(__name__)


REVIEW_POSITION_TOOL = {
    "type": "function",
    "function": {
        "name": "review_position",
        "description": (
            "Decide what to do with ONE open position right now given the "
            "latest news and quote. Call this tool ONCE per position we "
            "currently hold (parallel tool calls, one round). If nothing "
            "has changed, decide='hold'. If the thesis is broken by news "
            "or price action, decide='close'. If the position is still "
            "good but risk is rising, decide='tighten_stop' with a new "
            "stop_loss_pct that is STRICTLY tighter than the current one."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol", "decide", "rationale"],
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Underlying ticker for the position.",
                },
                "decide": {
                    "type": "string",
                    "enum": ["hold", "close", "tighten_stop"],
                },
                "new_stop_loss_pct": {
                    "type": "number",
                    "description": (
                        "Required only when decide='tighten_stop'. Decimal, "
                        "e.g. 0.02 for a 2% stop. Must be strictly smaller "
                        "than the current stop_loss_pct — otherwise the "
                        "update is ignored."
                    ),
                    "minimum": 0.001,
                    "maximum": 0.5,
                },
                "urgency": {
                    "type": "string",
                    "enum": ["low", "normal", "high"],
                    "description": (
                        "'high' means act now (breaking news); used only "
                        "for logging priority."
                    ),
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "One to three sentences. If decide='close', cite "
                        "the specific headline or tape move that killed "
                        "the thesis."
                    ),
                },
            },
        },
    },
}


SYSTEM_PROMPT = (
    "You are the POSITION-REVIEW agent. You do NOT open new positions. "
    "For each open position below, decide one of:\n"
    "  · hold          — thesis intact, nothing material has changed.\n"
    "  · close         — thesis broken; exit now.\n"
    "  · tighten_stop  — thesis intact but risk rising; pull the stop in.\n"
    "\n"
    "DEFAULT IS HOLD. The bracket orders on the position already enforce "
    "stop-loss and take-profit exits mechanically. You should override "
    "them only when there is a *specific new reason* since the trade was "
    "opened:\n"
    "  - A fresh headline (cited with the source line) that invalidates "
    "    the original thesis.\n"
    "  - A structural tape move (gap, unusual volume, circuit-breaker "
    "    neighbor) the stop-loss bracket won't catch in time.\n"
    "  - Risk clearly rising beyond what the opening stop allowed for — "
    "    then tighten_stop, not close.\n"
    "\n"
    "Do NOT close a position because:\n"
    "  - It is red on unrealized P&L. That's what the stop-loss is for.\n"
    "  - The news feed is quiet. Quiet = nothing changed = hold.\n"
    "  - You're uncertain. Uncertainty is the default — hold.\n"
    "\n"
    "Churning closes on noise costs us more than letting the bracket "
    "work. Past audits showed we close winners too early and losers at "
    "the bottom — fight that instinct. A position that is -1% on the day "
    "is NOT news; it is normal intraday variance.\n"
    "\n"
    "Emit ONE review_position tool call per position, in parallel, in a "
    "single round. In rationale, if decide=close or tighten_stop, cite "
    "the specific new information that justifies overriding the bracket. "
    "Options positions (shown with option_structure=...) stay with "
    "decide=hold unless news clearly invalidates the thesis — the runtime "
    "monitor does not enforce option stops, so close is the only exit."
)


@dataclass
class ReviewDecision:
    symbol: str
    decide: str  # "hold" | "close" | "tighten_stop"
    rationale: str
    urgency: str = "normal"
    new_stop_loss_pct: float | None = None


@dataclass
class PositionReviewResult:
    decisions: list[ReviewDecision] = field(default_factory=list)
    elapsed_sec: float = 0.0
    call_id: int | None = None
    error: str | None = None


@dataclass
class PositionContext:
    """Everything the LLM needs for one position row."""

    symbol: str
    size_usd: float
    entry_price: float
    current_price: float
    unrealized_pnl_usd: float
    stop_loss_pct: float | None
    take_profit_pct: float | None
    option_structure: str | None = None
    opened_at_iso: str | None = None
    news_lines: list[str] = field(default_factory=list)


class PositionReviewAgent:
    """Single-round parallel-tool-call agent for exit decisions."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        session_factory: async_sessionmaker | None = None,
        max_tokens: int = 1024,
        agent_id: str = "position-review",
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory
        self._max_tokens = max_tokens
        self._agent_id = agent_id

    async def review(
        self,
        positions: list[PositionContext],
        *,
        market_note: str = "",
    ) -> PositionReviewResult:
        bus = get_bus()
        started = time.monotonic()

        if not positions:
            return PositionReviewResult()

        user_msg = _format_review_prompt(positions, market_note)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        bus.publish(
            "agent.started",
            f"position-review over {len(positions)} open positions",
            data={
                "agent_id": self._agent_id,
                "role": "position-review",
                "count": len(positions),
            },
        )

        try:
            response = await self._provider.raw_completion(
                messages=messages,
                tools=[REVIEW_POSITION_TOOL],
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            bus.publish(
                "agent.failed",
                f"[{self._agent_id}] LLM error: {exc}",
                severity=EventSeverity.WARN,
                data={"agent_id": self._agent_id, "error": str(exc)},
            )
            return PositionReviewResult(
                elapsed_sec=time.monotonic() - started,
                error=str(exc),
            )

        call_id: int | None = None
        if self._session_factory is not None:
            try:
                row = await log_usage(
                    self._session_factory,
                    response,
                    purpose="position_review",
                    agent_id=self._agent_id,
                    round_idx=0,
                    prompt_messages=list(messages),
                )
                call_id = row.id
            except Exception:
                logger.exception("failed to persist position-review call row")

        choice = response.raw_response.get("choices", [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        decisions: list[ReviewDecision] = []
        held_symbols = {p.symbol.upper() for p in positions}

        for call in tool_calls:
            name = (call.get("function") or {}).get("name")
            if name != "review_position":
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
            sym = str(payload.get("symbol") or "").strip().upper()
            if not sym or sym not in held_symbols:
                continue
            decide = str(payload.get("decide") or "hold").strip().lower()
            if decide not in ("hold", "close", "tighten_stop"):
                decide = "hold"
            new_stop = payload.get("new_stop_loss_pct")
            try:
                new_stop_f = float(new_stop) if new_stop is not None else None
            except (TypeError, ValueError):
                new_stop_f = None
            decisions.append(
                ReviewDecision(
                    symbol=sym,
                    decide=decide,
                    rationale=str(payload.get("rationale") or "")[:500],
                    urgency=str(payload.get("urgency") or "normal").lower(),
                    new_stop_loss_pct=new_stop_f,
                )
            )

        # Dedup per symbol. The model occasionally emits the same
        # ``review_position`` call N times (seen: 11 identical close-ASTS
        # calls in one tick). Each duplicate downstream fired another
        # broker close order, and the 10 that raced the bracket lock
        # burned through Alpaca's rate budget. Keep the first occurrence.
        seen: set[str] = set()
        unique: list[ReviewDecision] = []
        for d in decisions:
            if d.symbol in seen:
                continue
            seen.add(d.symbol)
            unique.append(d)
        decisions = unique

        elapsed = time.monotonic() - started
        action_counts = {"hold": 0, "close": 0, "tighten_stop": 0}
        for d in decisions:
            action_counts[d.decide] = action_counts.get(d.decide, 0) + 1

        bus.publish(
            "agent.done",
            (
                f"[{self._agent_id}] {len(decisions)} decisions "
                f"(close={action_counts['close']}, "
                f"tighten={action_counts['tighten_stop']}, "
                f"hold={action_counts['hold']}) in {elapsed:.1f}s"
            ),
            data={
                "agent_id": self._agent_id,
                "decisions": [
                    {
                        "symbol": d.symbol,
                        "decide": d.decide,
                        "urgency": d.urgency,
                    }
                    for d in decisions
                ],
                "elapsed_sec": elapsed,
                "final_call_id": call_id,
            },
        )

        return PositionReviewResult(
            decisions=decisions,
            elapsed_sec=elapsed,
            call_id=call_id,
        )


def _format_review_prompt(
    positions: list[PositionContext], market_note: str
) -> str:
    lines: list[str] = []
    if market_note:
        lines.append(f"Market note: {market_note}")
        lines.append("")
    lines.append("Open positions to review:")
    for p in positions:
        stop = (
            f"{p.stop_loss_pct * 100:.2f}%"
            if p.stop_loss_pct is not None
            else "(none)"
        )
        tp = (
            f"{p.take_profit_pct * 100:.2f}%"
            if p.take_profit_pct is not None
            else "(none)"
        )
        struct = (
            f"  option_structure={p.option_structure}"
            if p.option_structure
            else ""
        )
        opened = (
            f"  opened={p.opened_at_iso}"
            if p.opened_at_iso
            else ""
        )
        lines.append(
            f"  · {p.symbol}: size=${p.size_usd:.2f} "
            f"entry=${p.entry_price:.2f} now=${p.current_price:.2f} "
            f"uPnL=${p.unrealized_pnl_usd:+.2f} "
            f"stop={stop} tp={tp}{struct}{opened}"
        )
        if p.news_lines:
            for nl in p.news_lines[:4]:
                lines.append(f"      · {nl[:200]}")
        else:
            lines.append("      (no recent headlines)")
    lines.append("")
    lines.append(
        "Emit one review_position call per position above, in parallel. "
        "Do not emit any other tool. Do not propose new positions."
    )
    return "\n".join(lines)


__all__ = [
    "PositionReviewAgent",
    "PositionReviewResult",
    "PositionContext",
    "ReviewDecision",
    "REVIEW_POSITION_TOOL",
]
