"""Orchestrator: parallel fan-out → per-agent findings captured."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.ai.llm_provider import AIResponse
from app.ai.orchestrator import Orchestrator, findings_to_prompt_block
from app.ai.research_loop import ResearchAgent


class _ScriptedProvider:
    """Returns a scripted response per call, keyed by round-robin."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)
        self.call_count = 0

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
        self.call_count += 1
        raw = self._responses.pop(0)
        return AIResponse(
            tool_input={},
            raw_request={},
            raw_response=raw,
            model=self.model,
            provider=self.provider,
        )


def _report_finding(symbol: str, bias: str, conf: float) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"c-{symbol}",
                            "type": "function",
                            "function": {
                                "name": "report_finding",
                                "arguments": json.dumps({
                                    "symbol": symbol,
                                    "bias": bias,
                                    "confidence": conf,
                                    "catalyst": f"{symbol} catalyst",
                                    "summary": f"{symbol} summary",
                                    "risks": "standard",
                                }),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
    }


@pytest.mark.asyncio
async def test_orchestrator_fans_out_and_collects():
    """Three symbols → three parallel research agents → three findings."""
    # Three agents, each reports immediately (no research tool calls).
    provider = _ScriptedProvider([
        _report_finding("AAPL", "bullish", 0.7),
        _report_finding("NVDA", "neutral", 0.5),
        _report_finding("TSLA", "bearish", 0.4),
    ])
    research_agent = ResearchAgent(
        provider=provider,  # type: ignore[arg-type]
    )
    orch = Orchestrator(
        provider=provider,  # type: ignore[arg-type]
        research_agent=research_agent,
        focus_count=3,
    )
    result = await orch.orchestrate(
        symbols=["AAPL", "NVDA", "TSLA", "IBM"],
        per_symbol_context={"AAPL": "ctx-a", "NVDA": "ctx-n", "TSLA": "ctx-t"},
    )
    assert len(result.findings) == 3
    biases = {f.symbol: f.bias for f in result.findings}
    assert biases == {"AAPL": "bullish", "NVDA": "neutral", "TSLA": "bearish"}
    # Focus pick takes only the first 3.
    symbols_picked = {f.symbol for f in result.findings}
    assert "IBM" not in symbols_picked


def _propose_structure(symbol: str, direction: str, structure: str) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"s-{symbol}",
                            "type": "function",
                            "function": {
                                "name": "propose_structure",
                                "arguments": json.dumps({
                                    "symbol": symbol,
                                    "direction": direction,
                                    "structure": structure,
                                    "legs": [
                                        {
                                            "side": "buy",
                                            "right": "call",
                                            "strike": 500,
                                            "expiry": "2026-05-16",
                                            "quantity": 1,
                                        },
                                        {
                                            "side": "sell",
                                            "right": "call",
                                            "strike": 510,
                                            "expiry": "2026-05-16",
                                            "quantity": 1,
                                        },
                                    ],
                                    "max_loss_usd": 300,
                                    "max_profit_usd": 700,
                                    "entry_price_estimate": 3.0,
                                    "confidence": 0.7,
                                    "catalyst": "earnings",
                                    "risks": "IV crush",
                                    "rationale": f"{structure} setup on {symbol}",
                                }),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10},
    }


@pytest.mark.asyncio
async def test_specialist_accepts_propose_structure():
    provider = _ScriptedProvider([
        _propose_structure("NVDA", "bullish", "debit_call_spread"),
    ])
    research_agent = ResearchAgent(provider=provider)  # type: ignore[arg-type]
    orch = Orchestrator(
        provider=provider,  # type: ignore[arg-type]
        research_agent=research_agent,
        focus_count=1,
    )
    result = await orch.orchestrate(
        symbols=["NVDA"], per_symbol_context={"NVDA": "ctx"}
    )
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.symbol == "NVDA"
    assert f.bias == "bullish"
    assert f.structure is not None
    assert f.structure["structure"] == "debit_call_spread"
    assert f.structure["max_loss_usd"] == 300

    block = findings_to_prompt_block(result.findings)
    assert "debit_call_spread" in block
    assert "max_loss" in block
    assert "legs:" in block


@pytest.mark.asyncio
async def test_findings_prompt_block_formatting():
    """Findings render into a readable block for the decision prompt."""
    provider = _ScriptedProvider([_report_finding("NVDA", "bullish", 0.85)])
    research_agent = ResearchAgent(provider=provider)  # type: ignore[arg-type]
    orch = Orchestrator(
        provider=provider,  # type: ignore[arg-type]
        research_agent=research_agent,
        focus_count=1,
    )
    result = await orch.orchestrate(
        symbols=["NVDA"], per_symbol_context={"NVDA": "ctx"}
    )
    block = findings_to_prompt_block(result.findings)
    assert "NVDA" in block
    assert "BULLISH" in block
    assert "0.85" in block
