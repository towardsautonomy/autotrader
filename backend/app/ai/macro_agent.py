"""Macro/regime agent — one LLM call per session to set the tape mood.

The decision agent benefits from a stable, session-wide view of the
macro environment: is today risk-on, risk-off, ranging, volatile?
Rather than rederiving this every 5-minute tick, this agent runs once
(at the first decision of the US equity session) and caches a compact
label + one-sentence color. Subsequent decisions in the same session
read from the cache.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import EventSeverity, get_bus
from app.ai.llm_provider import LLMProvider
from app.ai.usage import log_usage
from app.clock import now_pacific

logger = logging.getLogger(__name__)


SET_REGIME_TOOL = {
    "type": "function",
    "function": {
        "name": "set_regime",
        "description": (
            "Label today's macro tape in one compact bucket + a "
            "one-sentence color. Called once per session."
        ),
        "parameters": {
            "type": "object",
            "required": ["regime", "color"],
            "properties": {
                "regime": {
                    "type": "string",
                    "enum": ["risk_on", "risk_off", "ranging", "volatile"],
                },
                "color": {
                    "type": "string",
                    "description": (
                        "One sentence, concrete: what's driving the tape "
                        "today. Cite specific indices / catalysts / tone."
                    ),
                },
            },
        },
    },
}

SYSTEM_PROMPT = (
    "You are the MACRO agent. Output ONE label for today's tape: risk_on, "
    "risk_off, ranging, or volatile. Be honest, not bullish by default. "
    "Call set_regime exactly once."
)


@dataclass
class MacroRegime:
    label: str  # "risk_on" | "risk_off" | "ranging" | "volatile"
    color: str
    generated_on: date  # Pacific date
    call_id: int | None = None


class MacroAgent:
    """Cached once-per-session regime snapshot."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        session_factory: async_sessionmaker | None = None,
        max_tokens: int = 512,
        agent_id: str = "macro",
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory
        self._max_tokens = max_tokens
        self._agent_id = agent_id
        self._cache: MacroRegime | None = None
        self._lock = asyncio.Lock()

    async def get(self, *, market_news: list[str] | None = None) -> MacroRegime | None:
        """Return today's regime, computing it on first call of the session."""
        today = now_pacific().date()
        if self._cache is not None and self._cache.generated_on == today:
            return self._cache
        async with self._lock:
            if self._cache is not None and self._cache.generated_on == today:
                return self._cache
            result = await self._compute(today, market_news or [])
            if result is not None:
                self._cache = result
            return result

    async def _compute(
        self, today: date, market_news: list[str]
    ) -> MacroRegime | None:
        bus = get_bus()
        started = time.monotonic()

        news_block = (
            "\n".join(f"  · {h[:200]}" for h in market_news[:8])
            or "  (no headlines available)"
        )
        user_msg = (
            f"Today's Pacific date: {today.isoformat()}.\n"
            "Recent market headlines (general tape):\n"
            f"{news_block}\n\n"
            "Call set_regime with today's label and color."
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ]

        try:
            response = await self._provider.raw_completion(
                messages=messages,
                tools=[SET_REGIME_TOOL],
                max_tokens=self._max_tokens,
            )
        except Exception as exc:
            bus.publish(
                "macro.failed",
                f"macro agent LLM error: {exc}",
                severity=EventSeverity.WARN,
                data={"error": str(exc)},
            )
            return None

        call_id: int | None = None
        if self._session_factory is not None:
            try:
                row = await log_usage(
                    self._session_factory,
                    response,
                    purpose="macro_regime",
                    agent_id=self._agent_id,
                    round_idx=0,
                    prompt_messages=list(messages),
                )
                call_id = row.id
            except Exception:
                logger.exception("failed to persist macro call row")

        label = "ranging"
        color = ""
        choice = response.raw_response.get("choices", [{}])[0]
        msg = choice.get("message") or {}
        for call in msg.get("tool_calls") or []:
            name = (call.get("function") or {}).get("name")
            if name != "set_regime":
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
            label = str(payload.get("regime") or "ranging").strip().lower()
            if label not in ("risk_on", "risk_off", "ranging", "volatile"):
                label = "ranging"
            color = str(payload.get("color") or "").strip()[:400]
            break

        elapsed = time.monotonic() - started
        bus.publish(
            "macro.set",
            f"regime={label} — {color[:160]}",
            severity=EventSeverity.INFO,
            data={
                "regime": label,
                "color": color,
                "generated_on": today.isoformat(),
                "elapsed_sec": elapsed,
            },
        )
        return MacroRegime(
            label=label,
            color=color,
            generated_on=today,
            call_id=call_id,
        )


__all__ = ["MacroAgent", "MacroRegime", "SET_REGIME_TOOL"]
