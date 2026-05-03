"""Scout agent: raw scan → LLM-filtered candidate list.

The ScoutLoop already gathers raw movers + screener picks. This agent
adds an optional second pass: feed those picks to an LLM with a tight
scout prompt, let it call ``emit_candidates`` to keep only the names
with a real catalyst or setup this cycle. Keeps the specialist pool
focused so we don't burn tokens on noise.
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
from app.ai.tools import EMIT_CANDIDATES_TOOL
from app.ai.usage import log_usage

logger = logging.getLogger(__name__)


SCOUT_SYSTEM = (
    "You are the SCOUT agent. The trading system already ran a raw scan "
    "(movers + full-universe screener) and produced a list of tickers. Your "
    "job "
    "is to aggressively filter it to 3-8 names that actually deserve a "
    "specialist's attention RIGHT NOW for short-term options trades. "
    "Prefer names with a concrete catalyst (earnings, guidance, news, "
    "unusual flow, gap + follow-through). Reject obvious noise. Call "
    "emit_candidates exactly once with your picks."
)


@dataclass
class ScoutPick:
    symbol: str
    reason: str
    score: float | None = None


@dataclass
class ScoutResult:
    picks: list[ScoutPick]
    notes: str
    elapsed_sec: float
    call_id: int | None = None


class ScoutAgent:
    """Wraps an LLM call with the emit_candidates tool.

    One-shot — no research tools. The ScoutLoop is already doing the
    fast-signal plumbing; the LLM just applies judgment on top.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        session_factory: async_sessionmaker | None = None,
        max_tokens: int = 512,
        agent_id: str = "scout",
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory
        self._max_tokens = max_tokens
        self._agent_id = agent_id

    async def score(
        self,
        *,
        raw_candidates: list[dict[str, Any]],
        market_note: str = "",
        held_symbols: list[str] | None = None,
    ) -> ScoutResult:
        """Run the scout prompt over a raw candidate list.

        raw_candidates: list of {symbol, source, note, score?} dicts — the
        ScoutLoop's own picks pre-LLM. The scout agent sees the same
        context the decision loop would see and picks the short list.

        held_symbols: tickers we already hold — the scout is told NOT to
        re-propose these so the decision agent doesn't waste rounds on
        duplicates. Position-review owns existing holdings.
        """
        bus = get_bus()
        started = time.monotonic()

        if not raw_candidates:
            return ScoutResult(picks=[], notes="no raw candidates", elapsed_sec=0.0)

        user_msg = _format_scout_prompt(
            raw_candidates, market_note, held_symbols or []
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SCOUT_SYSTEM},
            {"role": "user", "content": user_msg},
        ]

        bus.publish(
            "agent.started",
            f"scout agent online over {len(raw_candidates)} raw candidates",
            data={
                "agent_id": self._agent_id,
                "role": "scout",
                "raw_count": len(raw_candidates),
            },
        )

        try:
            response = await self._provider.raw_completion(
                messages=messages,
                tools=[EMIT_CANDIDATES_TOOL],
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            bus.publish(
                "agent.failed",
                f"[{self._agent_id}] LLM error: {exc}",
                severity=EventSeverity.WARN,
                data={"agent_id": self._agent_id, "error": str(exc)},
            )
            return ScoutResult(
                picks=[],
                notes=f"scout failed: {exc}",
                elapsed_sec=time.monotonic() - started,
            )

        call_id: int | None = None
        if self._session_factory is not None:
            try:
                row = await log_usage(
                    self._session_factory,
                    response,
                    purpose="scout",
                    agent_id=self._agent_id,
                    round_idx=0,
                    prompt_messages=list(messages),
                )
                call_id = row.id
            except Exception:
                logger.exception("failed to persist scout call row")

        choice = response.raw_response.get("choices", [{}])[0]
        msg = choice.get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            bus.publish(
                "agent.failed",
                f"[{self._agent_id}] no tool_calls",
                severity=EventSeverity.WARN,
                data={"agent_id": self._agent_id, "call_id": call_id},
            )
            return ScoutResult(
                picks=[],
                notes="scout returned no tool_calls",
                elapsed_sec=time.monotonic() - started,
                call_id=call_id,
            )

        args_raw = (tool_calls[0].get("function") or {}).get("arguments") or "{}"
        try:
            payload = (
                json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
            )
        except Exception:
            payload = {}

        picks_raw = payload.get("picks") or []
        picks: list[ScoutPick] = []
        for p in picks_raw:
            sym = str(p.get("symbol") or "").strip().upper()
            if not sym:
                continue
            picks.append(
                ScoutPick(
                    symbol=sym,
                    reason=str(p.get("reason") or ""),
                    score=_opt_float(p.get("score")),
                )
            )
        notes = str(payload.get("notes") or "")
        elapsed = time.monotonic() - started

        bus.publish(
            "agent.done",
            f"[{self._agent_id}] emitted {len(picks)} picks in {elapsed:.1f}s",
            data={
                "agent_id": self._agent_id,
                "picks": [{"symbol": p.symbol, "reason": p.reason} for p in picks],
                "elapsed_sec": elapsed,
                "final_call_id": call_id,
            },
        )

        return ScoutResult(
            picks=picks,
            notes=notes,
            elapsed_sec=elapsed,
            call_id=call_id,
        )


def _format_scout_prompt(
    raw_candidates: list[dict[str, Any]],
    market_note: str,
    held_symbols: list[str],
) -> str:
    lines = ["Raw candidates from the last scan cycle:"]
    for c in raw_candidates[:40]:  # keep prompt bounded
        sym = c.get("symbol") or "?"
        source = c.get("source") or ""
        note = c.get("note") or ""
        lines.append(f"  · {sym} [{source}] {note}".rstrip())
    if held_symbols:
        lines.append("")
        lines.append(
            "Already held (do NOT re-propose these — the position-review "
            "agent owns existing holdings): "
            + ", ".join(sorted(set(s.upper() for s in held_symbols)))
        )
    if market_note:
        lines.append("")
        lines.append(f"Market note: {market_note}")
    lines.append("")
    lines.append(
        "Filter aggressively. 3-8 fresh names that really warrant a "
        "specialist. Skip tickers already in the held list above."
    )
    return "\n".join(lines)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = ["ScoutAgent", "ScoutResult", "ScoutPick"]
