"""Options-specific risk-engine + structure-builder tests.

The arithmetic in option structure builders and the tier gate in the
engine are the two places where bad code loses real money. Cover the
canonical shapes here so future edits fail loudly.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.market_data.options import OptionChain, OptionContract
from app.risk import (
    AccountSnapshot,
    Market,
    OptionLeg,
    OptionProposal,
    OptionSide,
    OptionStructure,
    RejectionCode,
    RiskConfig,
    RiskEngine,
    RiskTier,
    TradeAction,
    TradeProposal,
)
from app.strategies.option_structures import (
    BuilderError,
    build_iron_condor,
    build_long_option,
    build_vertical_credit,
    build_vertical_debit,
)


def _snap() -> AccountSnapshot:
    return AccountSnapshot(
        cash_balance=1000.0,
        positions=(),
        day_realized_pnl=0.0,
        cumulative_pnl=0.0,
        daily_trade_count=0,
        trading_enabled=True,
    )


def _cfg(**overrides) -> RiskConfig:
    defaults = dict(
        budget_cap=1000.0,
        max_position_pct=0.50,  # not testing stock-side caps here
        risk_tier=RiskTier.MODERATE,
        max_option_loss_per_spread_pct=0.05,  # $50 cap
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


def _make_proposal(
    structure: OptionStructure,
    *,
    max_loss: float = 40.0,
    max_gain: float | None = 60.0,
    expiry_days: int = 30,
    size_usd: float = 40.0,
) -> TradeProposal:
    exp = (date.today() + timedelta(days=expiry_days)).isoformat()
    leg = OptionLeg(
        option_symbol="TEST",
        side=OptionSide.CALL,
        strike=100.0,
        expiry=exp,
        ratio=+1,
        mid_price=1.0,
    )
    opt = OptionProposal(
        structure=structure,
        underlying="TEST",
        legs=(leg,),
        net_debit_usd=max_loss,
        max_loss_usd=max_loss,
        max_gain_usd=max_gain,
        expiry=exp,
    )
    return TradeProposal(
        market=Market.STOCKS,
        action=TradeAction.OPEN_LONG,
        symbol="TEST",
        size_usd=size_usd,
        option=opt,
    )


# ---------- tier gate ----------


def test_conservative_tier_blocks_long_call():
    eng = RiskEngine(_cfg(risk_tier=RiskTier.CONSERVATIVE))
    res = eng.validate(_make_proposal(OptionStructure.LONG_CALL), _snap())
    assert not res.approved
    assert res.code == RejectionCode.STRUCTURE_NOT_ALLOWED


def test_moderate_tier_blocks_iron_condor():
    eng = RiskEngine(_cfg(risk_tier=RiskTier.MODERATE))
    res = eng.validate(_make_proposal(OptionStructure.IRON_CONDOR), _snap())
    assert not res.approved
    assert res.code == RejectionCode.STRUCTURE_NOT_ALLOWED


def test_aggressive_tier_allows_iron_condor():
    eng = RiskEngine(_cfg(risk_tier=RiskTier.AGGRESSIVE))
    res = eng.validate(_make_proposal(OptionStructure.IRON_CONDOR), _snap())
    assert res.approved


# ---------- max loss cap ----------


def test_per_spread_loss_cap_rejects_oversized_long_call():
    eng = RiskEngine(_cfg(max_option_loss_per_spread_pct=0.01))  # $10 cap
    res = eng.validate(
        _make_proposal(OptionStructure.LONG_CALL, max_loss=50.0), _snap()
    )
    assert not res.approved
    assert res.code == RejectionCode.OPTION_MAX_LOSS_EXCEEDED


def test_per_spread_loss_cap_does_not_apply_to_covered_call():
    """Covered-call 'loss' = assignment at strike; not the metric we cap."""
    eng = RiskEngine(_cfg(max_option_loss_per_spread_pct=0.01))  # $10 cap
    res = eng.validate(
        _make_proposal(OptionStructure.COVERED_CALL, max_loss=9999.0), _snap()
    )
    assert res.approved


# ---------- expiry gate ----------


def test_0dte_rejected():
    eng = RiskEngine(_cfg())
    res = eng.validate(
        _make_proposal(OptionStructure.LONG_CALL, expiry_days=0), _snap()
    )
    assert not res.approved
    assert res.code == RejectionCode.EXPIRY_TOO_CLOSE


# ---------- structure builders — math invariants ----------


def _synthetic_chain() -> OptionChain:
    expiry = (date.today() + timedelta(days=35)).isoformat()
    # Underlying ~$100. Straddle-ish chain.
    contracts: list[OptionContract] = []
    for strike, delta_c, delta_p, mid_c, mid_p in [
        (90, 0.85, -0.15, 10.5, 0.50),
        (95, 0.65, -0.35, 6.2, 1.20),
        (100, 0.45, -0.55, 3.1, 3.00),
        (105, 0.28, -0.72, 1.4, 6.10),
        (110, 0.14, -0.86, 0.55, 10.40),
    ]:
        contracts.append(
            OptionContract(
                symbol=f"SYN{int(strike)}C",
                underlying="SYN",
                side=OptionSide.CALL,
                strike=strike,
                expiry=expiry,
                bid=mid_c - 0.05,
                ask=mid_c + 0.05,
                mid=mid_c,
                last=mid_c,
                implied_volatility=0.30,
                delta=delta_c,
                gamma=0.02,
                theta=-0.03,
                vega=0.15,
                open_interest=100,
                volume=10,
            )
        )
        contracts.append(
            OptionContract(
                symbol=f"SYN{int(strike)}P",
                underlying="SYN",
                side=OptionSide.PUT,
                strike=strike,
                expiry=expiry,
                bid=mid_p - 0.05,
                ask=mid_p + 0.05,
                mid=mid_p,
                last=mid_p,
                implied_volatility=0.30,
                delta=delta_p,
                gamma=0.02,
                theta=-0.03,
                vega=0.15,
                open_interest=100,
                volume=10,
            )
        )
    from datetime import UTC, datetime as _dt

    return OptionChain(
        underlying="SYN",
        contracts=tuple(contracts),
        fetched_at=_dt.now(UTC),
    )


def test_bull_call_debit_max_loss_equals_net_debit():
    chain = _synthetic_chain()
    p = build_vertical_debit(chain, direction="bull", long_delta=0.45, short_delta=0.28)
    # long 100 call @ 3.10, short 105 call @ 1.40 -> 1.70 debit -> $170
    assert p.structure == OptionStructure.VERTICAL_DEBIT
    assert abs(p.net_debit_usd - 170.0) < 1e-6
    assert abs(p.max_loss_usd - 170.0) < 1e-6
    # width = 5 × 100 = 500; max gain = 500 - 170 = 330
    assert abs((p.max_gain_usd or 0) - 330.0) < 1e-6


def test_bull_put_credit_loss_equals_width_minus_credit():
    chain = _synthetic_chain()
    p = build_vertical_credit(chain, direction="bull", short_delta=0.35, long_delta=0.15)
    # short 95 put @ 1.20, long 90 put @ 0.50 -> 0.70 credit -> $70
    assert abs(p.max_gain_usd - 70.0) < 1e-6
    assert abs(p.max_loss_usd - (500 - 70)) < 1e-6


def test_iron_condor_defined_risk():
    chain = _synthetic_chain()
    p = build_iron_condor(chain, wing_short_delta=0.35, wing_long_delta=0.15)
    # Closest-delta picks (abs delta):
    #   short_put=95  |-0.35|=0.35 → distance 0 → pick 95 put @ 1.20
    #   long_put=90   |-0.15|=0.15 → distance 0 → pick 90 put @ 0.50
    #   short_call=105 delta 0.28  → dist 0.07 beats 100's 0.10 → pick 105 @ 1.40
    #   long_call=110  delta 0.14  → dist 0.01 → pick 110 @ 0.55
    # credit_per_share = 1.20 - 0.50 + 1.40 - 0.55 = 1.55 → $155
    # worst wing = max(95-90, 110-105) = 5 → $500; max_loss = 500 - 155 = 345
    assert p.structure == OptionStructure.IRON_CONDOR
    assert abs(p.max_gain_usd - 155.0) < 1e-6
    assert abs(p.max_loss_usd - 345.0) < 1e-6
    # Defined-risk invariant: max_loss + max_gain = worst_wing * 100
    assert abs(p.max_loss_usd + p.max_gain_usd - 500.0) < 1e-6


def test_long_call_max_loss_is_debit_upside_uncapped():
    chain = _synthetic_chain()
    p = build_long_option(chain, side=OptionSide.CALL, delta=0.45)
    assert p.structure == OptionStructure.LONG_CALL
    assert p.max_gain_usd is None
    # long 100 call @ 3.10 → $310 debit == max loss
    assert abs(p.max_loss_usd - 310.0) < 1e-6
    assert abs(p.net_debit_usd - 310.0) < 1e-6


def test_invalid_bull_debit_strikes_rejected():
    """Flipped strike order should raise, not silently build an upside-down spread."""
    chain = _synthetic_chain()
    with pytest.raises(BuilderError):
        # Force short delta > long delta → short strike below long — inverted.
        build_vertical_debit(chain, direction="bull", long_delta=0.14, short_delta=0.85)
