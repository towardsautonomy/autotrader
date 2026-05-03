import json
from types import SimpleNamespace

import pytest

from app.ai.risk_config_generator import (
    deterministic_baseline,
    generate_risk_config,
)
from app.risk import RiskConfig


def _cfg_from(payload: dict) -> RiskConfig:
    """Construct a RiskConfig to ensure the baseline passes all invariants."""
    return RiskConfig(
        budget_cap=payload["budget_cap"],
        max_position_pct=payload["max_position_pct"],
        max_concurrent_positions=payload["max_concurrent_positions"],
        max_daily_trades=payload["max_daily_trades"],
        daily_loss_cap_pct=payload["daily_loss_cap_pct"],
        max_drawdown_pct=payload["max_drawdown_pct"],
        default_stop_loss_pct=payload["default_stop_loss_pct"],
        default_take_profit_pct=payload["default_take_profit_pct"],
        min_trade_size_usd=payload["min_trade_size_usd"],
        max_option_loss_per_spread_pct=payload["max_option_loss_per_spread_pct"],
        earnings_blackout_days=payload["earnings_blackout_days"],
        max_stop_loss_pct=payload["max_stop_loss_pct"],
        paper_cost_bps=payload["paper_cost_bps"],
    )


def test_baseline_sub_25k_respects_pdt():
    payload = deterministic_baseline(10_000.0)
    assert payload["max_daily_trades"] <= 3
    # Must construct without raising
    _cfg_from(payload)


def test_baseline_above_25k_loosens():
    payload = deterministic_baseline(50_000.0)
    assert payload["max_daily_trades"] > 3
    _cfg_from(payload)


def test_baseline_tiny_budget_accepts_full_position():
    payload = deterministic_baseline(200.0)
    # Below the spread floor — max_position_pct should be maxed out
    assert payload["max_position_pct"] == 1.0
    _cfg_from(payload)


def test_baseline_all_tiers_construct():
    for budget in [100.0, 500.0, 2_000.0, 10_000.0, 25_000.0, 100_000.0]:
        payload = deterministic_baseline(budget)
        _cfg_from(payload)


def test_baseline_drawdown_greater_than_daily_cap():
    for budget in [500.0, 5_000.0, 25_000.0, 100_000.0]:
        payload = deterministic_baseline(budget)
        assert payload["max_drawdown_pct"] > payload["daily_loss_cap_pct"]


def test_baseline_aggressive_preference_shifts_tier():
    base = deterministic_baseline(10_000.0)
    aggr = deterministic_baseline(10_000.0, preference="aggressive momentum")
    assert aggr["risk_tier"] == "aggressive"
    assert aggr["max_stop_loss_pct"] >= base["max_stop_loss_pct"]


def test_baseline_conservative_preference_tightens():
    base = deterministic_baseline(10_000.0)
    cons = deterministic_baseline(10_000.0, preference="conservative income")
    assert cons["risk_tier"] == "conservative"
    assert cons["daily_loss_cap_pct"] <= base["daily_loss_cap_pct"]


@pytest.mark.asyncio
async def test_generator_falls_back_when_provider_none():
    payload = await generate_risk_config(
        provider=None, session_factory=None, budget_cap=10_000.0
    )
    assert payload["budget_cap"] == 10_000.0
    _cfg_from(payload)


class _FakeProvider:
    def __init__(self, tool_args: dict):
        self._tool_args = tool_args

    async def raw_completion(self, **_kwargs):
        raw_response = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "emit_risk_config",
                                    "arguments": json.dumps(self._tool_args),
                                }
                            }
                        ]
                    }
                }
            ]
        }
        return SimpleNamespace(
            raw_response=raw_response,
            raw_request={},
            tool_input=self._tool_args,
            provider="test",
            model="test-model",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
        )


@pytest.mark.asyncio
async def test_generator_uses_llm_output_when_valid():
    llm_payload = dict(deterministic_baseline(10_000.0))
    llm_payload["rationale"] = "LLM-customized"
    llm_payload["max_daily_trades"] = 3  # PDT-aware
    provider = _FakeProvider(llm_payload)
    result = await generate_risk_config(
        provider=provider,
        session_factory=None,  # skip usage logging
        budget_cap=10_000.0,
    )
    assert result["rationale"] == "LLM-customized"
    assert result["budget_cap"] == 10_000.0  # server force-overrides


@pytest.mark.asyncio
async def test_generator_falls_back_on_invariant_violation():
    # Invariant: default_stop_loss_pct must be <= max_stop_loss_pct
    bad = dict(deterministic_baseline(10_000.0))
    bad["default_stop_loss_pct"] = 0.5
    bad["max_stop_loss_pct"] = 0.1
    provider = _FakeProvider(bad)
    result = await generate_risk_config(
        provider=provider,
        session_factory=None,
        budget_cap=10_000.0,
    )
    assert result["rationale"].startswith("Deterministic baseline")


@pytest.mark.asyncio
async def test_generator_overrides_llm_budget_cap():
    llm_payload = dict(deterministic_baseline(10_000.0))
    llm_payload["budget_cap"] = 99_999.0  # LLM tried to change it
    provider = _FakeProvider(llm_payload)
    result = await generate_risk_config(
        provider=provider,
        session_factory=None,
        budget_cap=10_000.0,  # what user asked for
    )
    assert result["budget_cap"] == 10_000.0
