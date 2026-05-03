"""Safety-constraint registry.

Each constraint is a rule about the interaction between the user's
risk config and external reality (broker minimums, exchange rules,
bid-ask economics) that the engine *can't* enforce mechanically but
the UI *should* warn the user about.

Severity levels:

- ``error``: the configured setup can't trade at all or will produce
  structurally losing results. Users should fix before running.
- ``warn``: the setup can trade but the user should understand the
  trade-off (e.g., PDT limit at <$25k means ≤3 day trades per 5
  business days).

The frontend fetches this list via the API and renders red/yellow
banners on the risk-config page. Adding a new constraint is: append an
entry to ``_CONSTRAINTS`` with a ``check`` closure and a short remedy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from app.risk import RiskConfig

Severity = Literal["error", "warn", "info"]


@dataclass(frozen=True, slots=True)
class ConstraintDef:
    key: str
    severity: Severity
    title: str
    description: str
    remedy: str
    # Returns True when the constraint is *violated* by this config.
    # (Named positively — "check triggers" — so the caller semantics
    # read naturally.)
    check: Callable[[RiskConfig], bool]


@dataclass(frozen=True, slots=True)
class ConstraintViolation:
    key: str
    severity: Severity
    title: str
    description: str
    remedy: str


# ---------- Constraint definitions ----------


def _pdt_triggered(cfg: RiskConfig) -> bool:
    # PDT rule: FINRA requires $25,000 minimum equity in a margin account
    # for 4+ day trades in 5 business days. Below that, Alpaca blocks the
    # 4th day trade in a rolling 5-business-day window.
    return cfg.budget_cap < 25_000.0


def _per_trade_below_spread_floor(cfg: RiskConfig) -> bool:
    # Sub-$50 notional trades pay proportionally huge bid-ask. A 0.05%
    # spread on $50 is $0.025 per side — fine. On $5 it's $0.0025. But
    # the spread ISN'T 0.05% on a $5 trade — it's the minimum tick.
    # Below $50 per-trade, the effective spread swamps your edge.
    return cfg.per_trade_max_usd < 50.0


def _options_budget_too_low(cfg: RiskConfig) -> bool:
    # One option contract covers 100 shares. A $1 premium = $100/contract.
    # Under a ~$500 per-trade max, even a cheap long call won't fit; an
    # iron condor definitely won't. The engine will reject every option
    # proposal silently — user sees no options activity and can't tell
    # why.
    return cfg.per_trade_max_usd < 500.0


def _daily_loss_cap_below_single_stop(cfg: RiskConfig) -> bool:
    # If your daily loss cap is tighter than a single worst-case stop-loss
    # trigger, *every* full-sized losing trade instantly halts the day.
    # That's often unintended — user expects to absorb a few stops before
    # halting.
    worst_single_loss = cfg.per_trade_max_usd * cfg.max_stop_loss_pct
    daily_cap = cfg.budget_cap * cfg.daily_loss_cap_pct
    return worst_single_loss > daily_cap


def _stop_loss_wider_than_daily_cap(cfg: RiskConfig) -> bool:
    # default_stop_loss_pct > max_stop_loss_pct is prevented by
    # __post_init__. This check catches the subtler mismatch where the
    # default exceeds daily_loss_cap_pct — one trade can exhaust the
    # whole daily budget.
    return cfg.default_stop_loss_pct > cfg.daily_loss_cap_pct


def _max_drawdown_too_tight(cfg: RiskConfig) -> bool:
    # max_drawdown <= daily_loss_cap means the drawdown halt will trip
    # the first time the daily cap trips — making drawdown redundant.
    return cfg.max_drawdown_pct <= cfg.daily_loss_cap_pct


_CONSTRAINTS: tuple[ConstraintDef, ...] = (
    ConstraintDef(
        key="pdt_rule",
        severity="warn",
        title="Pattern Day Trader rule applies",
        description=(
            "Below $25,000 equity, FINRA limits you to 3 day trades "
            "(open+close same session) per 5 business days. Alpaca will "
            "block the 4th attempt. The system can still swing-trade "
            "overnight positions without tripping PDT."
        ),
        remedy=(
            "Options: (a) raise budget_cap to $25,000+, (b) accept the "
            "3-in-5 day-trade limit and let the AI hold positions "
            "overnight, or (c) open an Alpaca cash account (no margin, "
            "no PDT — but T+1 settlement means you can only reuse "
            "proceeds the next day)."
        ),
        check=_pdt_triggered,
    ),
    ConstraintDef(
        key="per_trade_spread_floor",
        severity="error",
        title="Per-trade size below fee/spread floor",
        description=(
            "Max per-trade size (budget_cap × max_position_pct) is under "
            "$50. The bid-ask spread alone on a $5 trade will often "
            "exceed the edge of even a 'correct' prediction. Analytics "
            "will show losses that are structural, not strategy."
        ),
        remedy=(
            "Raise budget_cap, raise max_position_pct, or wait to run "
            "the system until you have more capital. $500+ per-trade "
            "is where bid-ask stops dominating."
        ),
        check=_per_trade_below_spread_floor,
    ),
    ConstraintDef(
        key="options_budget_floor",
        severity="warn",
        title="Per-trade size too small for options",
        description=(
            "Option contracts cover 100 shares — a $1 premium already "
            "costs $100. Under a $500 per-trade max the risk engine "
            "will silently reject every options proposal. If you picked "
            "a risk_tier that allows options, they won't actually run."
        ),
        remedy=(
            "Either raise per-trade max (budget_cap × max_position_pct) "
            "to $500+, or set risk_tier=conservative to signal the LLM "
            "to stay in stock/ETF territory."
        ),
        check=_options_budget_too_low,
    ),
    ConstraintDef(
        key="daily_cap_smaller_than_one_stop",
        severity="warn",
        title="One full-size stop consumes the daily loss cap",
        description=(
            "A single worst-case stop-loss (per_trade_max × "
            "max_stop_loss_pct) is larger than the daily loss cap. "
            "The first losing trade each day halts all trading."
        ),
        remedy=(
            "Either raise daily_loss_cap_pct (accept more daily pain), "
            "lower max_position_pct (smaller positions), or lower "
            "max_stop_loss_pct (tighter stops)."
        ),
        check=_daily_loss_cap_below_single_stop,
    ),
    ConstraintDef(
        key="default_stop_above_daily_cap",
        severity="warn",
        title="Default stop-loss is wider than the daily loss cap",
        description=(
            "When the AI doesn't specify a stop, the engine injects "
            "default_stop_loss_pct. That default is wider than "
            "daily_loss_cap_pct — so one full-sized losing trade will "
            "exhaust the daily budget by itself."
        ),
        remedy=(
            "Tighten default_stop_loss_pct (e.g. to 0.02 or below) "
            "or raise daily_loss_cap_pct."
        ),
        check=_stop_loss_wider_than_daily_cap,
    ),
    ConstraintDef(
        key="drawdown_redundant",
        severity="info",
        title="Max-drawdown halt is redundant with daily-loss halt",
        description=(
            "max_drawdown_pct ≤ daily_loss_cap_pct means the drawdown "
            "halt can only fire *after* the daily halt has already "
            "fired. The drawdown halt adds no protection as configured."
        ),
        remedy=(
            "Raise max_drawdown_pct to be meaningfully larger than "
            "daily_loss_cap_pct (e.g. 0.10 vs 0.02 — drawdown covers "
            "sustained losing streaks; daily cap covers one bad day)."
        ),
        check=_max_drawdown_too_tight,
    ),
)


def list_constraints() -> list[dict]:
    """Return all constraint definitions as JSON-safe dicts.

    Used by the API to expose the registry to the frontend so the UI
    can render help text and remediations alongside the live check
    results.
    """
    return [
        {
            "key": c.key,
            "severity": c.severity,
            "title": c.title,
            "description": c.description,
            "remedy": c.remedy,
        }
        for c in _CONSTRAINTS
    ]


def evaluate_constraints(cfg: RiskConfig) -> list[ConstraintViolation]:
    """Evaluate every constraint against ``cfg`` and return violations."""
    out: list[ConstraintViolation] = []
    for c in _CONSTRAINTS:
        try:
            if c.check(cfg):
                out.append(
                    ConstraintViolation(
                        key=c.key,
                        severity=c.severity,
                        title=c.title,
                        description=c.description,
                        remedy=c.remedy,
                    )
                )
        except Exception:
            # A broken check must not block the API response; the UI can
            # survive a missing warning but not a 500.
            continue
    return out
