"""Pure builders for defined-risk option proposals.

These builders exist to compute the *math* (net debit, max_loss, max_gain)
from a set of legs — the AI picks the structure and the strikes, we
validate the arithmetic. Getting max_loss wrong is how accounts blow up,
so the numeric contract stays deterministic even while selection moves
upstream.

Each builder accepts either:
  - explicit `expiry` + per-leg `*_strike` values (AI-chosen), or
  - a `min_dte`/`max_dte` window + per-leg `*_delta` targets (fallback
    convenience when the AI has picked the structure but not the strikes).

Strike is preferred when both are given. Omitting both raises.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable

from app.clock import ny_today
from app.market_data.options import OptionChain, OptionContract
from app.risk import (
    OptionLeg,
    OptionProposal,
    OptionSide,
    OptionStructure,
)

logger = logging.getLogger(__name__)


class BuilderError(Exception):
    """Cannot build a viable proposal from the given chain + inputs."""


def _pick_by_strike(
    side: OptionSide, contracts: list[OptionContract], *, strike: float
) -> OptionContract:
    """Find the contract at exactly `strike` on this side, or nearest
    matching strike within 0.5 (to tolerate AI-supplied rounding)."""
    same_side = [c for c in contracts if c.side == side]
    if not same_side:
        raise BuilderError(f"no {side.value} contracts in expiry")
    exact = [c for c in same_side if abs(c.strike - strike) < 1e-6]
    if exact:
        return exact[0]
    nearest = min(same_side, key=lambda c: abs(c.strike - strike))
    if abs(nearest.strike - strike) <= 0.5:
        return nearest
    raise BuilderError(
        f"no {side.value} strike near {strike:.2f} "
        f"(closest: {nearest.strike:.2f})"
    )


def _pick_by_delta(
    side: OptionSide, contracts: list[OptionContract], *, delta: float
) -> OptionContract:
    """Pick the contract whose |delta| is closest to the target.

    Falls back to a strike-index heuristic when the chain lacks greeks
    — deep-OTM strikes routinely come back without delta from Alpaca's
    snapshot endpoint."""
    same_side = [c for c in contracts if c.side == side]
    if not same_side:
        raise BuilderError(f"no {side.value} contracts in expiry")
    with_delta = [c for c in same_side if c.delta is not None]
    target_abs = abs(delta)
    if with_delta:
        return min(with_delta, key=lambda c: abs(abs(c.delta or 0) - target_abs))
    sorted_strikes = sorted(same_side, key=lambda c: c.strike)
    idx = min(len(sorted_strikes) - 1, max(0, int(len(sorted_strikes) * target_abs)))
    return sorted_strikes[idx]


def _resolve(
    side: OptionSide,
    contracts: list[OptionContract],
    *,
    strike: float | None,
    delta: float | None,
    role: str,
) -> OptionContract:
    if strike is not None:
        return _pick_by_strike(side, contracts, strike=strike)
    if delta is not None:
        return _pick_by_delta(side, contracts, delta=delta)
    raise BuilderError(f"{role} leg needs either strike or delta")


def _pick_expiry(
    chain: OptionChain,
    *,
    expiry: str | None,
    min_dte: int,
    max_dte: int,
) -> str:
    if expiry is not None:
        if expiry not in chain.expiries():
            raise BuilderError(
                f"expiry {expiry} not in chain "
                f"(available: {', '.join(chain.expiries()[:5])}...)"
            )
        return expiry
    today = ny_today()
    viable: list[tuple[int, str]] = []
    for e in chain.expiries():
        try:
            d = date.fromisoformat(e)
        except ValueError:
            continue
        dte = (d - today).days
        if min_dte <= dte <= max_dte:
            viable.append((dte, e))
    if not viable:
        raise BuilderError(
            f"no expiry in {min_dte}..{max_dte} DTE (chain has "
            f"{len(chain.expiries())} expiries)"
        )
    viable.sort(key=lambda pair: abs(pair[0] - (min_dte + max_dte) // 2))
    return viable[0][1]


def _leg(contract: OptionContract, ratio: int) -> OptionLeg:
    return OptionLeg(
        option_symbol=contract.symbol,
        side=contract.side,
        strike=contract.strike,
        expiry=contract.expiry,
        ratio=ratio,
        mid_price=contract.mid_or_last,
    )


def _require_prices(contracts: Iterable[OptionContract]) -> None:
    missing = [c.symbol for c in contracts if c.mid_or_last is None]
    if missing:
        raise BuilderError(f"no mid/last for contracts: {', '.join(missing)}")


_CONTRACT_MULTIPLIER = 100


def build_vertical_debit(
    chain: OptionChain,
    *,
    direction: str,
    expiry: str | None = None,
    long_strike: float | None = None,
    short_strike: float | None = None,
    long_delta: float | None = None,
    short_delta: float | None = None,
    min_dte: int = 25,
    max_dte: int = 45,
    contracts: int = 1,
) -> OptionProposal:
    """Bull call / bear put debit spread.

    AI-picked path: pass `expiry`, `long_strike`, `short_strike`.
    Convenience path: pass deltas + DTE window; the builder picks strikes
    closest to those deltas.
    """
    if direction not in {"bull", "bear"}:
        raise BuilderError(f"direction must be bull|bear, got {direction}")
    if contracts < 1:
        raise BuilderError("contracts must be >= 1")
    side = OptionSide.CALL if direction == "bull" else OptionSide.PUT
    chosen_expiry = _pick_expiry(chain, expiry=expiry, min_dte=min_dte, max_dte=max_dte)
    legs_in_expiry = chain.for_expiry(chosen_expiry)

    long_c = _resolve(
        side, legs_in_expiry, strike=long_strike, delta=long_delta, role="long"
    )
    short_c = _resolve(
        side, legs_in_expiry, strike=short_strike, delta=short_delta, role="short"
    )
    if long_c.strike == short_c.strike:
        raise BuilderError("long and short strikes collapsed to same contract")

    if direction == "bull" and long_c.strike >= short_c.strike:
        raise BuilderError("bull debit: long call strike must be < short call strike")
    if direction == "bear" and long_c.strike <= short_c.strike:
        raise BuilderError("bear debit: long put strike must be > short put strike")

    _require_prices([long_c, short_c])
    width = abs(long_c.strike - short_c.strike)
    debit_per_share = (long_c.mid_or_last or 0) - (short_c.mid_or_last or 0)
    if debit_per_share <= 0:
        raise BuilderError(
            f"debit spread requires positive debit, got {debit_per_share:.2f}/share"
        )

    mult = _CONTRACT_MULTIPLIER * contracts
    net_debit = debit_per_share * mult
    max_loss = net_debit
    max_gain = width * mult - net_debit

    legs = (_leg(long_c, +contracts), _leg(short_c, -contracts))
    return OptionProposal(
        structure=OptionStructure.VERTICAL_DEBIT,
        underlying=chain.underlying,
        legs=legs,
        net_debit_usd=float(net_debit),
        max_loss_usd=float(max_loss),
        max_gain_usd=float(max_gain),
        expiry=chosen_expiry,
    )


def build_vertical_credit(
    chain: OptionChain,
    *,
    direction: str,
    expiry: str | None = None,
    short_strike: float | None = None,
    long_strike: float | None = None,
    short_delta: float | None = None,
    long_delta: float | None = None,
    min_dte: int = 25,
    max_dte: int = 45,
    contracts: int = 1,
) -> OptionProposal:
    """Cash-backed credit spread. Short leg is nearer the money."""
    if direction not in {"bull", "bear"}:
        raise BuilderError(f"direction must be bull|bear, got {direction}")
    side = OptionSide.PUT if direction == "bull" else OptionSide.CALL
    chosen_expiry = _pick_expiry(chain, expiry=expiry, min_dte=min_dte, max_dte=max_dte)
    legs_in_expiry = chain.for_expiry(chosen_expiry)

    short_c = _resolve(
        side, legs_in_expiry, strike=short_strike, delta=short_delta, role="short"
    )
    long_c = _resolve(
        side, legs_in_expiry, strike=long_strike, delta=long_delta, role="long"
    )
    if long_c.strike == short_c.strike:
        raise BuilderError("long/short strikes collapsed to same contract")

    if direction == "bull" and long_c.strike >= short_c.strike:
        raise BuilderError("bull credit (short put): long put < short put strike")
    if direction == "bear" and long_c.strike <= short_c.strike:
        raise BuilderError("bear credit (short call): long call > short call strike")

    _require_prices([long_c, short_c])
    width = abs(long_c.strike - short_c.strike)
    credit_per_share = (short_c.mid_or_last or 0) - (long_c.mid_or_last or 0)
    if credit_per_share <= 0:
        raise BuilderError(
            f"credit spread requires positive credit, got {credit_per_share:.2f}/share"
        )

    mult = _CONTRACT_MULTIPLIER * contracts
    credit = credit_per_share * mult
    max_gain = credit
    max_loss = width * mult - credit

    legs = (_leg(short_c, -contracts), _leg(long_c, +contracts))
    return OptionProposal(
        structure=OptionStructure.VERTICAL_CREDIT,
        underlying=chain.underlying,
        legs=legs,
        net_debit_usd=float(-credit),
        max_loss_usd=float(max_loss),
        max_gain_usd=float(max_gain),
        expiry=chosen_expiry,
    )


def build_iron_condor(
    chain: OptionChain,
    *,
    expiry: str | None = None,
    short_put_strike: float | None = None,
    long_put_strike: float | None = None,
    short_call_strike: float | None = None,
    long_call_strike: float | None = None,
    wing_short_delta: float | None = None,
    wing_long_delta: float | None = None,
    min_dte: int = 30,
    max_dte: int = 55,
    contracts: int = 1,
) -> OptionProposal:
    """Short put + long put (lower) + short call + long call (upper).
    Defined risk = wider wing × contracts."""
    chosen_expiry = _pick_expiry(chain, expiry=expiry, min_dte=min_dte, max_dte=max_dte)
    in_e = chain.for_expiry(chosen_expiry)
    short_put = _resolve(
        OptionSide.PUT, in_e,
        strike=short_put_strike, delta=wing_short_delta, role="short_put",
    )
    long_put = _resolve(
        OptionSide.PUT, in_e,
        strike=long_put_strike, delta=wing_long_delta, role="long_put",
    )
    short_call = _resolve(
        OptionSide.CALL, in_e,
        strike=short_call_strike, delta=wing_short_delta, role="short_call",
    )
    long_call = _resolve(
        OptionSide.CALL, in_e,
        strike=long_call_strike, delta=wing_long_delta, role="long_call",
    )

    if not (long_put.strike < short_put.strike < short_call.strike < long_call.strike):
        raise BuilderError(
            "iron condor legs not in canonical order "
            f"({long_put.strike} < {short_put.strike} < "
            f"{short_call.strike} < {long_call.strike})"
        )

    _require_prices([long_put, short_put, short_call, long_call])
    credit_per_share = (
        (short_put.mid_or_last or 0)
        - (long_put.mid_or_last or 0)
        + (short_call.mid_or_last or 0)
        - (long_call.mid_or_last or 0)
    )
    if credit_per_share <= 0:
        raise BuilderError(f"iron condor credit non-positive: {credit_per_share:.2f}/share")

    put_width = short_put.strike - long_put.strike
    call_width = long_call.strike - short_call.strike
    worst_width = max(put_width, call_width)

    mult = _CONTRACT_MULTIPLIER * contracts
    credit = credit_per_share * mult
    max_loss = worst_width * mult - credit
    max_gain = credit

    legs = (
        _leg(long_put, +contracts),
        _leg(short_put, -contracts),
        _leg(short_call, -contracts),
        _leg(long_call, +contracts),
    )
    return OptionProposal(
        structure=OptionStructure.IRON_CONDOR,
        underlying=chain.underlying,
        legs=legs,
        net_debit_usd=float(-credit),
        max_loss_usd=float(max_loss),
        max_gain_usd=float(max_gain),
        expiry=chosen_expiry,
    )


def build_long_option(
    chain: OptionChain,
    *,
    side: OptionSide,
    expiry: str | None = None,
    strike: float | None = None,
    delta: float | None = None,
    min_dte: int = 25,
    max_dte: int = 60,
    contracts: int = 1,
) -> OptionProposal:
    chosen_expiry = _pick_expiry(chain, expiry=expiry, min_dte=min_dte, max_dte=max_dte)
    in_e = chain.for_expiry(chosen_expiry)
    c = _resolve(side, in_e, strike=strike, delta=delta, role="long")
    _require_prices([c])
    mult = _CONTRACT_MULTIPLIER * contracts
    debit = (c.mid_or_last or 0) * mult
    return OptionProposal(
        structure=(
            OptionStructure.LONG_CALL
            if side == OptionSide.CALL
            else OptionStructure.LONG_PUT
        ),
        underlying=chain.underlying,
        legs=(_leg(c, +contracts),),
        net_debit_usd=float(debit),
        max_loss_usd=float(debit),
        max_gain_usd=(
            None if side == OptionSide.CALL else float(c.strike * mult - debit)
        ),
        expiry=chosen_expiry,
    )


__all__ = [
    "BuilderError",
    "build_iron_condor",
    "build_long_option",
    "build_vertical_credit",
    "build_vertical_debit",
]
