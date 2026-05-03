"""ScoutAgent: emit_candidates tool parsed into a filtered pick list."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.ai.llm_provider import AIResponse
from app.ai.scout_agent import ScoutAgent


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


def _emit(picks: list[dict], notes: str = "") -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "emit_candidates",
                                "arguments": json.dumps(
                                    {"picks": picks, "notes": notes}
                                ),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.asyncio
async def test_scout_filters_and_dedupes_via_tool():
    provider = _ScriptedProvider(
        _emit(
            [
                {"symbol": "nvda", "reason": "earnings tomorrow", "score": 0.9},
                {"symbol": "AAPL", "reason": "fresh breakout", "score": 0.6},
            ],
            notes="tech strong",
        )
    )
    agent = ScoutAgent(provider=provider)  # type: ignore[arg-type]
    res = await agent.score(
        raw_candidates=[
            {"symbol": "NVDA", "source": "gainer", "note": "+5%"},
            {"symbol": "AAPL", "source": "screener", "note": "high vol"},
            {"symbol": "XYZ", "source": "gainer", "note": "+20%"},
        ]
    )
    assert [p.symbol for p in res.picks] == ["NVDA", "AAPL"]
    assert res.picks[0].score == 0.9
    assert res.notes == "tech strong"


@pytest.mark.asyncio
async def test_scout_empty_raw_short_circuits():
    provider = _ScriptedProvider(_emit([]))
    agent = ScoutAgent(provider=provider)  # type: ignore[arg-type]
    res = await agent.score(raw_candidates=[])
    assert res.picks == []
    assert provider.calls == []  # no LLM call issued


@pytest.mark.asyncio
async def test_scout_handles_missing_tool_calls():
    provider = _ScriptedProvider(
        {"choices": [{"message": {"content": "oops", "tool_calls": []}}]}
    )
    agent = ScoutAgent(provider=provider)  # type: ignore[arg-type]
    res = await agent.score(
        raw_candidates=[{"symbol": "NVDA", "source": "x", "note": ""}]
    )
    assert res.picks == []
    assert "no tool_calls" in res.notes
