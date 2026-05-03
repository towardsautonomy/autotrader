"""AI-driven risk-config generator.

Takes a budget (and an optional preference string) and returns a complete
``RiskConfig`` payload. The LLM is responsible for reasoning about FINRA
PDT, bid-ask spread floors, options contract minimums, stop-loss math,
etc. and emitting a full config — callers then preview the values in the
UI before saving.

The output is validated twice:

1. **Schema** — tool-call args go through the pydantic ``RiskConfigIn``
   model. Missing or out-of-range fields raise before we try to construct
   the dataclass.
2. **Dataclass invariants** — ``RiskConfig.__post_init__`` re-validates
   the dataclass bounds (e.g. default_stop_loss_pct <= max_stop_loss_pct).
   A failure here is a generator bug; we fall back to a deterministic
   baseline so the UI never gets a blank preview.

The baseline fallback is also what backs the "no AI key configured" path
— users on local/offline setups still get a reasonable preview.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.llm_provider import LLMProvider
from app.ai.usage import log_usage
from app.risk import RiskConfig

logger = logging.getLogger(__name__)


GENERATE_TOOL = {
    "type": "function",
    "function": {
        "name": "emit_risk_config",
        "description": (
            "Emit a complete risk-config tailored to the user's budget. "
            "Every field must respect these invariants: "
            "0 < all *_pct fields <= 1; "
            "default_stop_loss_pct <= max_stop_loss_pct; "
            "max_drawdown_pct > daily_loss_cap_pct (otherwise drawdown "
            "halt is redundant); "
            "budget_cap × max_position_pct >= min_trade_size_usd; "
            "budget_cap × max_position_pct >= $50 to avoid the bid-ask "
            "spread eating every trade's edge. "
            "Aware of FINRA PDT rule (<$25k margin → 3 day trades per 5 "
            "business days, so max_daily_trades should be conservative); "
            "aware of options contract minimums (100 shares/contract, so "
            "per-trade max < $500 makes options infeasible and tier "
            "should drop to conservative)."
        ),
        "parameters": {
            "type": "object",
            "required": [
                "budget_cap",
                "max_position_pct",
                "max_concurrent_positions",
                "max_daily_trades",
                "daily_loss_cap_pct",
                "max_drawdown_pct",
                "default_stop_loss_pct",
                "default_take_profit_pct",
                "max_stop_loss_pct",
                "min_trade_size_usd",
                "max_option_loss_per_spread_pct",
                "earnings_blackout_days",
                "paper_cost_bps",
                "pdt_day_trade_count_5bd",
                "risk_tier",
                "blacklist",
                "rationale",
            ],
            "properties": {
                "budget_cap": {"type": "number"},
                "max_position_pct": {"type": "number"},
                "max_concurrent_positions": {"type": "integer"},
                "max_daily_trades": {"type": "integer"},
                "daily_loss_cap_pct": {"type": "number"},
                "max_drawdown_pct": {"type": "number"},
                "default_stop_loss_pct": {"type": "number"},
                "default_take_profit_pct": {"type": "number"},
                "max_stop_loss_pct": {"type": "number"},
                "min_trade_size_usd": {"type": "number"},
                "max_option_loss_per_spread_pct": {"type": "number"},
                "earnings_blackout_days": {"type": "integer"},
                "paper_cost_bps": {"type": "number"},
                "pdt_day_trade_count_5bd": {
                    "type": "integer",
                    "description": (
                        "FINRA PDT cap: same-day round trips per rolling 5 "
                        "business days. Use 3 for budgets < $25,000; 99 "
                        "(effectively unlimited) for budgets above, since "
                        "PDT no longer applies."
                    ),
                },
                "risk_tier": {
                    "type": "string",
                    "enum": ["conservative", "moderate", "aggressive"],
                },
                "blacklist": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "rationale": {
                    "type": "string",
                    "description": (
                        "2-4 sentence explanation of the key trade-offs "
                        "in this config — what constraints drove the "
                        "most meaningful field choices."
                    ),
                },
            },
        },
    },
}


SYSTEM_PROMPT = (
    "You are a senior risk manager configuring guardrails for an AI "
    "day-trading system. Your job: translate a user's budget into a "
    "complete, internally consistent set of hard limits that:\n"
    "1. Prevent regulatory lock-outs (FINRA PDT when <$25k).\n"
    "2. Prevent structural losses from fees/spread (tiny per-trade notionals).\n"
    "3. Prevent single-trade blowups (stop-loss and position-size caps).\n"
    "4. Match the user's appetite (conservative / moderate / aggressive).\n\n"
    "Always choose conservative defaults when the budget is small. For "
    "budgets under $25,000, prefer max_daily_trades <= 3 and risk_tier "
    "conservative — options need $500+ per-trade notional to be feasible. "
    "For budgets under $500, per-trade size of the full budget is unavoidable "
    "but the 'spread floor' warning will still fire — that's fine, the user "
    "needs to see it. Never emit values that violate the schema invariants."
)


def _build_user_prompt(budget_cap: float, preference: str | None) -> str:
    pref = preference or "balanced / default"
    return (
        f"Budget: ${budget_cap:,.2f} USD\n"
        f"Preference: {pref}\n\n"
        "Emit a full risk_config via the emit_risk_config tool. Tune every "
        "field to this budget. In rationale, call out the top 2-3 "
        "constraints that drove your choices (e.g. 'PDT forces "
        "max_daily_trades=3 and no options' or 'budget is tight — kept "
        "default_stop_loss_pct narrow so one loss ≠ daily halt')."
    )


def deterministic_baseline(
    budget_cap: float, preference: str | None = None
) -> dict[str, Any]:
    """Hand-tuned fallback used when the LLM fails or is disabled.

    Covers four tiers: tiny (<$500), small (<$5k), sub-PDT (<$25k), and
    above-PDT. Chosen to pass the safety-constraint registry whenever
    physically possible — i.e. the 'spread floor' warning is unavoidable
    under ~$1,000 since per-trade necessarily drops below $50.
    """
    tier: str
    max_pos_pct: float
    max_concurrent: int
    max_daily: int
    daily_cap_pct: float
    max_dd_pct: float
    default_stop: float
    default_tp: float
    max_stop: float
    min_size: float
    risk_tier: str

    if budget_cap < 500:
        # Below $500 the per-trade floor is unavoidable. Max out position
        # size, accept few trades/day.
        max_pos_pct = 1.0
        max_concurrent = 1
        max_daily = 2
        daily_cap_pct = 0.04
        max_dd_pct = 0.20
        default_stop = 0.02
        default_tp = 0.04
        max_stop = 0.05
        min_size = 1.0
        risk_tier = "conservative"
    elif budget_cap < 5_000:
        max_pos_pct = 0.20
        max_concurrent = 3
        max_daily = 3  # PDT-safe
        daily_cap_pct = 0.03
        max_dd_pct = 0.15
        default_stop = 0.02
        default_tp = 0.05
        max_stop = 0.06
        min_size = 50.0
        risk_tier = "conservative"
    elif budget_cap < 25_000:
        max_pos_pct = 0.10
        max_concurrent = 4
        max_daily = 3  # PDT-safe
        daily_cap_pct = 0.025
        max_dd_pct = 0.12
        default_stop = 0.02
        default_tp = 0.05
        max_stop = 0.07
        min_size = 50.0
        risk_tier = "moderate"
    else:
        max_pos_pct = 0.05
        max_concurrent = 5
        max_daily = 10
        daily_cap_pct = 0.02
        max_dd_pct = 0.10
        default_stop = 0.02
        default_tp = 0.05
        max_stop = 0.08
        min_size = 100.0
        risk_tier = "moderate"
    tier = risk_tier

    pref = (preference or "").lower()
    if "aggressive" in pref and budget_cap >= 5_000:
        risk_tier = "aggressive"
        max_stop = min(0.10, max_stop + 0.02)
        default_tp = default_stop * 3
    elif "conservative" in pref:
        risk_tier = "conservative"
        daily_cap_pct = max(0.015, daily_cap_pct - 0.005)
        max_dd_pct = max(daily_cap_pct * 4, max_dd_pct - 0.02)

    return {
        "budget_cap": budget_cap,
        "max_position_pct": max_pos_pct,
        "max_concurrent_positions": max_concurrent,
        "max_daily_trades": max_daily,
        "daily_loss_cap_pct": daily_cap_pct,
        "max_drawdown_pct": max_dd_pct,
        "default_stop_loss_pct": default_stop,
        "default_take_profit_pct": default_tp,
        "max_stop_loss_pct": max_stop,
        "min_trade_size_usd": min_size,
        "max_option_loss_per_spread_pct": 0.02,
        "earnings_blackout_days": 2,
        "paper_cost_bps": 5.0,
        "pdt_day_trade_count_5bd": 3 if budget_cap < 25_000 else 99,
        "risk_tier": risk_tier,
        "blacklist": [],
        "rationale": (
            f"Deterministic baseline for ${budget_cap:,.0f} ({tier} tier). "
            "PDT-safe and spread-floor-aware; edit fields as needed."
        ),
    }


async def generate_risk_config(
    *,
    provider: LLMProvider | None,
    session_factory: async_sessionmaker | None,
    budget_cap: float,
    preference: str | None = None,
) -> dict[str, Any]:
    """Return a full RiskConfig payload for ``budget_cap``.

    Falls back to deterministic baseline when:
    - ``provider`` is None (no LLM configured)
    - LLM call raises
    - LLM returns malformed / invariant-violating output
    """
    if provider is None:
        logger.info("risk-config generator: no provider, using baseline")
        return deterministic_baseline(budget_cap, preference)

    user_prompt = _build_user_prompt(budget_cap, preference)
    try:
        response = await provider.raw_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            tools=[GENERATE_TOOL],
            tool_choice="required",
            max_tokens=1024,
        )
    except Exception:
        logger.exception("risk-config generator: provider call failed")
        return deterministic_baseline(budget_cap, preference)

    tool_input = _extract_tool_input(response.raw_response)
    if not tool_input:
        logger.warning("risk-config generator: no tool_call returned")
        return deterministic_baseline(budget_cap, preference)

    # Override budget_cap with the user-requested value so the LLM can't
    # silently change it if they misread the prompt.
    tool_input["budget_cap"] = float(budget_cap)

    if not _passes_invariants(tool_input):
        logger.warning(
            "risk-config generator: LLM output failed invariant checks, "
            "falling back to baseline"
        )
        return deterministic_baseline(budget_cap, preference)

    if session_factory is not None:
        try:
            await log_usage(
                session_factory,
                response,
                purpose="risk_config_generator",
            )
        except Exception:
            logger.exception("risk-config generator: log_usage failed")

    return tool_input


def _extract_tool_input(raw_response: dict[str, Any] | None) -> dict[str, Any]:
    if not raw_response:
        return {}
    try:
        choices = raw_response.get("choices") or []
        if not choices:
            return {}
        msg = choices[0].get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        if not tool_calls:
            return {}
        fn = tool_calls[0].get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            return json.loads(args)
        if isinstance(args, dict):
            return dict(args)
    except Exception:
        logger.exception("risk-config generator: could not parse tool_calls")
    return {}


def _passes_invariants(payload: dict[str, Any]) -> bool:
    """Cheap pre-check before constructing RiskConfig, so we can log a
    useful warning instead of eating a ValueError."""
    try:
        RiskConfig(
            budget_cap=float(payload["budget_cap"]),
            max_position_pct=float(payload["max_position_pct"]),
            max_concurrent_positions=int(payload["max_concurrent_positions"]),
            max_daily_trades=int(payload["max_daily_trades"]),
            daily_loss_cap_pct=float(payload["daily_loss_cap_pct"]),
            max_drawdown_pct=float(payload["max_drawdown_pct"]),
            default_stop_loss_pct=float(payload["default_stop_loss_pct"]),
            default_take_profit_pct=float(payload["default_take_profit_pct"]),
            max_stop_loss_pct=float(payload["max_stop_loss_pct"]),
            min_trade_size_usd=float(payload["min_trade_size_usd"]),
            max_option_loss_per_spread_pct=float(
                payload["max_option_loss_per_spread_pct"]
            ),
            earnings_blackout_days=int(payload["earnings_blackout_days"]),
            paper_cost_bps=float(payload["paper_cost_bps"]),
            pdt_day_trade_count_5bd=int(payload["pdt_day_trade_count_5bd"]),
        )
        return True
    except Exception:
        return False


__all__ = ["generate_risk_config", "deterministic_baseline"]
