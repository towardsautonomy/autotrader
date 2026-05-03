"""Deterministic portfolio-risk sentinel — pure function over the snapshot.

Computes concentration, directional exposure tilt, budget utilization, and
cash ratio so the decision prompt can reason about the CURRENT shape of
the book before proposing another trade. No LLM call here; this is a
cheap block the agent always sees."""

from __future__ import annotations

from dataclasses import dataclass

from .types import AccountSnapshot, RiskConfig


@dataclass(frozen=True, slots=True)
class PortfolioRisk:
    total_exposure_usd: float
    cash_balance: float
    budget_cap: float
    budget_utilization_pct: float  # 0.0–1.0+ (can exceed 1 if over budget)
    cash_ratio: float  # cash / (cash + exposure); 1.0 means all cash
    position_count: int
    largest_position_symbol: str | None
    largest_position_pct_of_exposure: float  # 0.0–1.0
    unrealized_pnl_usd: float
    biggest_winner_symbol: str | None
    biggest_winner_pnl_usd: float
    biggest_loser_symbol: str | None
    biggest_loser_pnl_usd: float
    concentration_warning: str | None  # human-readable flag or None


def compute_portfolio_risk(
    snapshot: AccountSnapshot, config: RiskConfig
) -> PortfolioRisk:
    exposure = snapshot.total_exposure_usd
    cash = snapshot.cash_balance
    denom = cash + exposure
    cash_ratio = cash / denom if denom > 0 else 1.0
    util = exposure / config.budget_cap if config.budget_cap > 0 else 0.0

    largest_sym: str | None = None
    largest_pct = 0.0
    winner_sym: str | None = None
    winner_pnl = 0.0
    loser_sym: str | None = None
    loser_pnl = 0.0
    unrealized = 0.0

    for p in snapshot.positions:
        unrealized += p.unrealized_pnl
        if exposure > 0:
            pct = p.size_usd / exposure
            if pct > largest_pct:
                largest_pct = pct
                largest_sym = p.symbol
        if p.unrealized_pnl > winner_pnl:
            winner_pnl = p.unrealized_pnl
            winner_sym = p.symbol
        if p.unrealized_pnl < loser_pnl:
            loser_pnl = p.unrealized_pnl
            loser_sym = p.symbol

    warning: str | None = None
    if largest_pct >= 0.5 and len(snapshot.positions) >= 2:
        warning = (
            f"{largest_sym} is {largest_pct * 100:.0f}% of deployed capital — "
            "concentration risk. Prefer closes/trims over new opens on this name."
        )
    elif util >= 1.0:
        warning = (
            "Over budget — propose closes, not opens. Risk engine will reject "
            "every open this cycle."
        )
    elif util >= 0.9:
        warning = (
            "Near budget cap — room for at most one small new open; favor "
            "rotation (close something first)."
        )

    return PortfolioRisk(
        total_exposure_usd=exposure,
        cash_balance=cash,
        budget_cap=config.budget_cap,
        budget_utilization_pct=util,
        cash_ratio=cash_ratio,
        position_count=len(snapshot.positions),
        largest_position_symbol=largest_sym,
        largest_position_pct_of_exposure=largest_pct,
        unrealized_pnl_usd=unrealized,
        biggest_winner_symbol=winner_sym,
        biggest_winner_pnl_usd=winner_pnl,
        biggest_loser_symbol=loser_sym,
        biggest_loser_pnl_usd=loser_pnl,
        concentration_warning=warning,
    )


def format_portfolio_risk_block(risk: PortfolioRisk) -> str:
    """Render a compact multi-line block for injection into the decision
    prompt. Formatting is terse so the LLM scans it fast."""
    lines: list[str] = []
    lines.append(
        f"  - Exposure: ${risk.total_exposure_usd:.2f} / ${risk.budget_cap:.2f} cap "
        f"({risk.budget_utilization_pct * 100:.0f}% utilized)"
    )
    lines.append(
        f"  - Cash ratio: {risk.cash_ratio * 100:.0f}% "
        f"({risk.position_count} open position{'s' if risk.position_count != 1 else ''})"
    )
    if risk.largest_position_symbol is not None:
        lines.append(
            f"  - Largest: {risk.largest_position_symbol} "
            f"({risk.largest_position_pct_of_exposure * 100:.0f}% of deployed)"
        )
    lines.append(f"  - Unrealized P&L: ${risk.unrealized_pnl_usd:+.2f}")
    if risk.biggest_winner_symbol is not None and risk.biggest_winner_pnl_usd > 0:
        lines.append(
            f"  - Biggest winner: {risk.biggest_winner_symbol} "
            f"${risk.biggest_winner_pnl_usd:+.2f}"
        )
    if risk.biggest_loser_symbol is not None and risk.biggest_loser_pnl_usd < 0:
        lines.append(
            f"  - Biggest loser: {risk.biggest_loser_symbol} "
            f"${risk.biggest_loser_pnl_usd:+.2f}"
        )
    if risk.concentration_warning:
        lines.append(f"  - ⚠ {risk.concentration_warning}")
    return "\n".join(lines)


__all__ = [
    "PortfolioRisk",
    "compute_portfolio_risk",
    "format_portfolio_risk_block",
]
