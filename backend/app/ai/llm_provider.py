"""OpenAI-compatible LLM provider used for both OpenRouter and LM Studio.

Both endpoints implement the OpenAI Chat Completions API with tool-calling,
so we use a single client. Selection is driven by AI_PROVIDER env:

- ``openrouter``  → https://openrouter.ai/api/v1     (paid, any frontier model)
- ``lmstudio``    → http://localhost:1234/v1         (local, free, private)

Both paths enforce a `propose_trade` tool-call so the model can't emit
free-form prose that bypasses the risk engine.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


TRADE_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_trade",
        "description": (
            "Propose the next trading action. If no trade makes sense right now, "
            "call this tool with action='hold' and explain why in rationale."
        ),
        "parameters": {
            "type": "object",
            "required": ["action", "rationale", "confidence"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["open_long", "open_short", "close", "hold"],
                    "description": (
                        "open_long = enter a new long position (bullish). "
                        "open_short = enter a new short position (bearish — "
                        "borrow and sell; profit if price falls). "
                        "close = flatten an existing position you already "
                        "hold — use this proactively to take profit or cut "
                        "losses on open positions. "
                        "hold = do nothing this cycle. Holding is fine, but "
                        "don't neglect closes just because it's passive: an "
                        "open position with no thesis left is bleeding theta "
                        "or carry — close it."
                    ),
                },
                "symbol": {
                    "type": "string",
                    "description": "Ticker (open_long/open_short/close only).",
                },
                "size_usd": {
                    "type": "number",
                    "description": (
                        "Notional $ to deploy (open_long/open_short only). "
                        "Must respect per-trade cap disclosed in the prompt."
                    ),
                },
                "stop_loss_pct": {
                    "type": "number",
                    "description": "Positive decimal, e.g. 0.03 = -3% stop.",
                },
                "take_profit_pct": {
                    "type": "number",
                    "description": "Positive decimal, e.g. 0.06 = +6% take-profit.",
                },
                "rationale": {
                    "type": "string",
                    "description": "One-paragraph explanation in plain English.",
                },
                "confidence": {
                    "type": "number",
                    "description": "0 to 1; confidence this trade is +EV.",
                    "minimum": 0,
                    "maximum": 1,
                },
                "option": {
                    "type": "object",
                    "description": (
                        "Present ONLY when you want to express this trade "
                        "as an options structure instead of a plain stock "
                        "position. Omit for equity trades. When present, "
                        "size_usd is ignored (capital-at-risk is derived "
                        "from the legs). Action must be open_long or "
                        "open_short; both mean 'open this option position'."
                    ),
                    "properties": {
                        "structure": {
                            "type": "string",
                            "enum": [
                                "long_call",
                                "long_put",
                                "vertical_debit",
                                "vertical_credit",
                                "iron_condor",
                            ],
                            "description": (
                                "long_call / long_put = buy one contract. "
                                "vertical_debit = bull call or bear put debit "
                                "spread. vertical_credit = bull put or bear "
                                "call credit spread. iron_condor = short "
                                "put + long put + short call + long call."
                            ),
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["bull", "bear"],
                            "description": (
                                "Required for vertical_debit and "
                                "vertical_credit — picks call vs put side."
                            ),
                        },
                        "expiry": {
                            "type": "string",
                            "description": (
                                "ISO date of the expiry (e.g. 2026-05-16). "
                                "Required. Must be a valid expiry on the "
                                "chain; no 0dte."
                            ),
                        },
                        "contracts": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Contract count. Each = 100 shares.",
                        },
                        "long_strike": {"type": "number"},
                        "short_strike": {"type": "number"},
                        "short_put_strike": {"type": "number"},
                        "long_put_strike": {"type": "number"},
                        "short_call_strike": {"type": "number"},
                        "long_call_strike": {"type": "number"},
                    },
                    "required": ["structure", "expiry", "contracts"],
                },
            },
        },
    },
}


@dataclass(frozen=True, slots=True)
class AIResponse:
    tool_input: dict[str, Any]
    raw_request: dict[str, Any]
    raw_response: dict[str, Any]
    model: str
    provider: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMProvider:
    """Thin wrapper over the OpenAI SDK pointed at any OpenAI-compatible host."""

    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        base_url: str,
    ) -> None:
        from openai import OpenAI

        # LM Studio doesn't require a key but the SDK does; dummy is fine.
        self._client = OpenAI(api_key=api_key or "lm-studio", base_url=base_url)
        self._provider = provider
        self._model = model
        self._base_url = base_url

    @property
    def model(self) -> str:
        return self._model

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def description(self) -> str:
        return f"{self._provider}::{self._model} @ {self._base_url}"

    async def raw_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
        tool_choice: str = "required",
    ) -> AIResponse:
        """Low-level one-shot completion. Returns the full raw response so
        the caller can inspect tool_calls directly. Used by the research
        loop where multiple tools are live and we can't force a single
        named tool."""
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
        }
        response = await asyncio.to_thread(
            self._client.chat.completions.create, **payload
        )
        raw = response.model_dump() if hasattr(response, "model_dump") else {}
        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(
            getattr(usage, "total_tokens", 0)
            or (prompt_tokens + completion_tokens)
        )
        return AIResponse(
            tool_input={},
            raw_request=_redact(payload),
            raw_response=raw,
            model=self._model,
            provider=self._provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

    async def propose(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 1024,
    ) -> AIResponse:
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [TRADE_TOOL],
            # "required" forces *some* tool call. The OpenAI-spec object form
            # {"type":"function","function":{"name":"propose_trade"}} is not
            # accepted by LM Studio, which only supports the string values
            # "none" | "auto" | "required". Since we expose exactly one tool,
            # "required" is equivalent to naming it explicitly.
            "tool_choice": "required",
        }
        response = await asyncio.to_thread(
            self._client.chat.completions.create, **payload
        )

        tool_input: dict[str, Any] = {}
        message = response.choices[0].message if response.choices else None
        tool_calls = getattr(message, "tool_calls", None) if message else None
        if tool_calls:
            args = tool_calls[0].function.arguments
            tool_input = json.loads(args) if isinstance(args, str) else dict(args)
        elif message and getattr(message, "content", None):
            # LM Studio sometimes returns plain text when the local model
            # ignores tool_choice. Try to parse a JSON block as a fallback.
            tool_input = _parse_fallback_json(message.content or "")

        if not tool_input:
            raise RuntimeError(
                f"{self._provider} response did not contain a tool_call for propose_trade"
            )

        usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(
            getattr(usage, "total_tokens", 0)
            or (prompt_tokens + completion_tokens)
        )

        return AIResponse(
            tool_input=tool_input,
            raw_request=_redact(payload),
            raw_response=response.model_dump() if hasattr(response, "model_dump") else {},
            model=self._model,
            provider=self._provider,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )


def _parse_fallback_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return {}


def _redact(payload: dict) -> dict:
    safe = dict(payload)
    safe.pop("api_key", None)
    return safe


def build_provider_from_settings(settings) -> LLMProvider:
    """Construct the configured provider. Called at scheduler startup."""
    which = (settings.ai_provider or "openrouter").lower()
    if which == "lmstudio":
        return LLMProvider(
            provider="lmstudio",
            api_key="lm-studio",
            model=settings.lmstudio_model,
            base_url=settings.lmstudio_base_url,
        )
    if which == "openrouter":
        return LLMProvider(
            provider="openrouter",
            api_key=settings.openrouter_api_key,
            model=settings.claude_model,
            base_url="https://openrouter.ai/api/v1",
        )
    raise ValueError(f"unknown AI_PROVIDER={which!r} (expected openrouter|lmstudio)")
