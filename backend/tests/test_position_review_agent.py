"""PositionReviewAgent: parses parallel review_position tool calls.

Covers the bug-prone behaviors:
  - Empty positions list short-circuits (no LLM call).
  - hold / close / tighten_stop are all parsed correctly.
  - Duplicate calls per symbol are de-duplicated to one decision (this
    bug burned through Alpaca's rate budget once — keep it covered).
  - Tool calls naming an unknown symbol are filtered out.
  - Malformed new_stop_loss_pct values become None instead of crashing.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.ai.llm_provider import AIResponse
from app.ai.position_review_agent import (
    PositionContext,
    PositionReviewAgent,
)


class _ScriptedProvider:
    def __init__(self, raw: dict) -> None:
        self._raw = raw
        self.calls: list[dict] = []

    @property
    def provider(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def raw_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
        tool_choice: str = "required",
    ) -> AIResponse:
        self.calls.append({"messages": messages, "tools": tools})
        return AIResponse(
            tool_input={},
            raw_request={},
            raw_response=self._raw,
            model=self.model,
            provider=self.provider,
        )


def _tool_call(call_id: str, args: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "review_position",
            "arguments": json.dumps(args),
        },
    }


def _wrap(tool_calls: list[dict]) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": tool_calls,
                }
            }
        ],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        },
    }


def _ctx(symbol: str, **kw: Any) -> PositionContext:
    base = {
        "symbol": symbol,
        "size_usd": 1000.0,
        "entry_price": 100.0,
        "current_price": 100.5,
        "unrealized_pnl_usd": 5.0,
        "stop_loss_pct": 0.03,
        "take_profit_pct": 0.06,
    }
    base.update(kw)
    return PositionContext(**base)


@pytest.mark.asyncio
async def test_empty_positions_short_circuits():
    provider = _ScriptedProvider(_wrap([]))
    agent = PositionReviewAgent(provider=provider)  # type: ignore[arg-type]
    res = await agent.review([])
    assert res.decisions == []
    assert provider.calls == []  # no LLM call when nothing to review


@pytest.mark.asyncio
async def test_parses_hold_close_tighten_stop():
    provider = _ScriptedProvider(
        _wrap(
            [
                _tool_call("1", {
                    "symbol": "NVDA",
                    "decide": "hold",
                    "rationale": "thesis intact",
                }),
                _tool_call("2", {
                    "symbol": "AAPL",
                    "decide": "close",
                    "rationale": "guidance cut",
                    "urgency": "high",
                }),
                _tool_call("3", {
                    "symbol": "MSFT",
                    "decide": "tighten_stop",
                    "rationale": "vol expanding",
                    "new_stop_loss_pct": 0.015,
                }),
            ]
        )
    )
    agent = PositionReviewAgent(provider=provider)  # type: ignore[arg-type]
    res = await agent.review(
        [_ctx("NVDA"), _ctx("AAPL"), _ctx("MSFT")]
    )

    by_symbol = {d.symbol: d for d in res.decisions}
    assert by_symbol["NVDA"].decide == "hold"
    assert by_symbol["AAPL"].decide == "close"
    assert by_symbol["AAPL"].urgency == "high"
    assert by_symbol["MSFT"].decide == "tighten_stop"
    assert by_symbol["MSFT"].new_stop_loss_pct == pytest.approx(0.015)


@pytest.mark.asyncio
async def test_dedupes_duplicate_close_calls():
    # The bug: model emitted 11 identical close-ASTS calls in one tick.
    # Each downstream fired a broker close, racing the bracket lock and
    # exhausting Alpaca's rate budget. Keep the first occurrence only.
    provider = _ScriptedProvider(
        _wrap(
            [
                _tool_call(
                    str(i),
                    {
                        "symbol": "ASTS",
                        "decide": "close",
                        "rationale": "headline X",
                    },
                )
                for i in range(11)
            ]
        )
    )
    agent = PositionReviewAgent(provider=provider)  # type: ignore[arg-type]
    res = await agent.review([_ctx("ASTS")])
    assert len(res.decisions) == 1
    assert res.decisions[0].symbol == "ASTS"
    assert res.decisions[0].decide == "close"


@pytest.mark.asyncio
async def test_filters_unknown_symbol():
    # The model occasionally hallucinates a symbol we don't actually
    # hold. The agent never opens new positions, so anything not in the
    # held set must be dropped.
    provider = _ScriptedProvider(
        _wrap(
            [
                _tool_call("1", {
                    "symbol": "GHOST",
                    "decide": "close",
                    "rationale": "we don't even own this",
                }),
                _tool_call("2", {
                    "symbol": "NVDA",
                    "decide": "hold",
                    "rationale": "ok",
                }),
            ]
        )
    )
    agent = PositionReviewAgent(provider=provider)  # type: ignore[arg-type]
    res = await agent.review([_ctx("NVDA")])
    assert [d.symbol for d in res.decisions] == ["NVDA"]


@pytest.mark.asyncio
async def test_malformed_new_stop_loss_pct_becomes_none():
    provider = _ScriptedProvider(
        _wrap(
            [
                _tool_call("1", {
                    "symbol": "NVDA",
                    "decide": "tighten_stop",
                    "rationale": "vol up",
                    "new_stop_loss_pct": "not-a-number",
                }),
            ]
        )
    )
    agent = PositionReviewAgent(provider=provider)  # type: ignore[arg-type]
    res = await agent.review([_ctx("NVDA")])
    assert len(res.decisions) == 1
    assert res.decisions[0].new_stop_loss_pct is None
