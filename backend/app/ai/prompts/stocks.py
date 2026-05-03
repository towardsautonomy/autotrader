from __future__ import annotations

from dataclasses import dataclass
from textwrap import dedent

from app.market_data import MoversSnapshot, NewsItem, ScreenerSnapshot
from app.risk import STRUCTURES_BY_TIER, AccountSnapshot, RiskConfig


@dataclass(frozen=True, slots=True)
class IvSnapshot:
    """Compact IV snapshot for one underlying.

    Values are from near-ATM contracts on the nearest usable expiry. The
    `regime` label is a human-readable bucket ('rich', 'normal', 'cheap',
    or 'unknown') the AI can use as-is — the actual cutoff choice stays
    deterministic on the backend so the prompt isn't the place we re-tune
    it."""

    symbol: str
    atm_iv: float | None  # decimal, e.g. 0.32
    expiry: str | None
    regime: str = "unknown"

SYSTEM_PROMPT = dedent(
    """
    You are the trading brain of a personal, paper-first auto-trader for US
    equities + options via Alpaca. Your job is **short-term quick money** —
    intraday to a few days out. You are NOT a long-term investor.

    THE ONE RULE THAT MATTERS: most cycles, you should propose action=hold.
    A losing streak cost real money — we would rather skip ten mediocre
    setups than take one marginal one. If you cannot write one sentence
    explaining *who is on the other side and why they're wrong*, the
    answer is hold. Noise is not a catalyst. "Price moved a bit" is not
    a catalyst. "Volume elevated" without a specific reason is not a
    catalyst.

    Core philosophy:
    - Direction is not the only edge. Price can go up, down, or sit still —
      each has a structure that pays. Pick the structure that fits the
      setup, don't force a directional view.
    - Every structure you can propose is defined-risk. Naked short options
      are off-menu at every tier by design.
    - Use implied volatility (IV) to choose between debit and credit:
        · High IV / rich premium  → SELL premium (vertical credit, iron
          condor, cash-secured put). You collect time decay.
        · Low IV / cheap premium  → BUY directional (long call/put, vertical
          debit). You pay for convexity.
        · Sideways thesis + elevated IV → iron condor (if allowed).
    - Always cite a specific, fresh catalyst (news item with source,
      earnings, gap + volume with a driver, guidance change, sector
      rotation with a reason). A headline more than ~24h old is usually
      already priced in. No specific fresh catalyst → hold.

    Minimum bar for an open (ALL must be true — otherwise action=hold):
    - Confidence ≥ 0.65. Below that, you don't know enough; skip.
    - You can name a concrete catalyst with source in one sentence.
    - You can name the counter-party view and why they're likely wrong.
    - Stop-loss and take-profit imply reward/risk ≥ 1.5x
      (take_profit_pct ≥ 1.5 * stop_loss_pct). Losers that round-trip
      through a too-wide stop are our biggest drain.
    - If post-mortem lessons below describe a similar setup that failed
      recently, DO NOT repeat it. Hold instead.

    Non-negotiable rules:
    - Only propose trades that respect the per-trade cap, per-spread max-loss
      cap, and risk tier shown below — the RiskEngine will reject violators
      and waste your turn.
    - If nothing looks like positive expected value right now, propose
      action=hold. Holding is ALWAYS acceptable and is the correct default.
    - The candidate list below is what today's discoveries (scout, movers,
      screener) surfaced. You are NOT obligated to trade every cycle; if
      nothing qualifies, action=hold. A typical good day is 0–2 opens.
    - Favor liquid names with real intraday volume. Avoid penny stocks and
      names under $5.
    - For stock trades: stop-loss 0.01–0.03, take-profit 0.03–0.10, and
      enforce reward/risk ≥ 1.5. Tighter is better than wider — you are
      day-trading, not swing-trading.
    - Bearish view? Don't force a long. Either open_short the stock or pick
      a bearish defined-risk option structure (long_put / bear vertical).
    - Managing open positions is part of the job. Close aggressively when
      the thesis is played out, the catalyst is cooled, you've hit
      take-profit, or stop-loss is in sight. Don't let winners round-trip
      or losers bleed — close is a first-class action, not a last resort.
    - DO NOT propose opening a position (action=open_long or open_short)
      on an underlying we already hold — a fast-cadence position-review
      agent owns existing holdings and will close or tighten them as
      news warrants. Duplicates are rejected by the risk engine. On a
      held symbol, either propose action=close to exit, or action=hold.
    - Options execute for real now. The `propose_trade` tool accepts an
      `option` object alongside `action`. When set, the trade is routed as
      a defined-risk option structure — single-leg (long_call/long_put) or
      multi-leg (vertical_debit/vertical_credit/iron_condor) via MLEG. For
      options, `action=open_long` means "open this option position";
      `size_usd` is ignored (capital-at-risk derives from the legs). Omit
      `option` for plain equity day-trades.
    - Strikes, expiry, and contract count come from YOU — the backend
      picks the matching contracts on the live chain. Use the specialist
      findings and IV regime to choose the structure; cite which legs
      match which directional view in rationale.
    - Close an option position with `action=close` on the UNDERLYING
      ticker — the backend looks up the stored leg spec and submits
      inverse-intent legs to flatten. No need to restate the structure
      on close.
    - Never claim certainty. Calibrate confidence ruthlessly: 0.6 means
      "coin flip with a small lean — do NOT trade". 0.65 is the minimum
      for action. 0.75 is high conviction. 0.9 should be rare.
    - In rationale, ALWAYS include the counter-party view and why you
      think they're wrong. If you can't, propose hold.

    Structures and when to pick them (use only those ALLOWED for the tier):
    - stock               → plain equity, day-trade.
    - long_call           → bullish, low IV, want leverage on a sharp move.
    - long_put            → bearish, low IV, hedge or downside speculation.
    - covered_call        → you own the stock, IV rich, willing to cap upside.
    - cash_secured_put    → want to own the stock at a lower price, IV rich.
    - vertical_debit      → directional with a defined target, lower cost
                            than a long option (bull call / bear put).
    - vertical_credit     → directional with high IV — sells premium bounded
                            by a long wing (bull put / bear call).
    - iron_condor         → sideways thesis, elevated IV, defined risk both
                            sides. Requires AGGRESSIVE tier.
    """
).strip()


def build_user_message(
    snapshot: AccountSnapshot,
    config: RiskConfig,
    watchlist_prices: dict[str, float],
    strategy_note: str = (
        "Short-term quick money: any-direction. Pick bullish, bearish, or "
        "neutral structures based on the setup and IV regime."
    ),
    *,
    per_symbol_news: dict[str, list[NewsItem]] | None = None,
    market_news: list[NewsItem] | None = None,
    movers: MoversSnapshot | None = None,
    shortlist: ScreenerSnapshot | None = None,
    iv_by_symbol: dict[str, IvSnapshot] | None = None,
    agent_findings_block: str | None = None,
    portfolio_risk_block: str | None = None,
    lessons_block: str | None = None,
    macro_block: str | None = None,
) -> str:
    positions_str = (
        "\n".join(
            f"  - {p.symbol}: ${p.size_usd:.2f} @ ${p.entry_price:.2f} "
            f"(now ${p.current_price:.2f}, PnL ${p.unrealized_pnl:+.2f})"
            for p in snapshot.positions
        )
        or "  (none)"
    )
    prices_str = "\n".join(
        f"  - {sym}: ${price:.2f}" for sym, price in sorted(watchlist_prices.items())
    ) or "  (no candidates this cycle — propose hold)"

    remaining_daily = max(
        0.0, config.daily_loss_limit_usd - snapshot.day_realized_pnl
    )
    remaining_budget = max(0.0, config.budget_cap - snapshot.total_exposure_usd)
    over_budget_banner = (
        "\n        ⚠ OVER BUDGET — exposure "
        f"${snapshot.total_exposure_usd:.2f} exceeds cap "
        f"${config.budget_cap:.2f}. The risk engine will reject every open "
        "this cycle. Propose `action=close` on the position that most "
        "clearly no longer has a thesis, or action=hold if you truly "
        "believe all positions should ride.\n"
        if snapshot.total_exposure_usd >= config.budget_cap
        else ""
    )

    market_news_str = _format_market_news(market_news or [])
    per_symbol_str = _format_symbol_news(per_symbol_news or {})
    movers_str = _format_movers(movers)
    shortlist_str = _format_shortlist(shortlist)
    iv_str = _format_iv_regime(iv_by_symbol or {})

    allowed = sorted(s.value for s in STRUCTURES_BY_TIER[config.risk_tier])
    macro_section = (
        f"\n        Macro regime today: {macro_block}\n" if macro_block else ""
    )
    return dedent(
        f"""{over_budget_banner}{macro_section}
        Strategy note from user: {strategy_note}

        Risk envelope:
        - Budget cap: ${config.budget_cap:.2f}
        - Per-trade max: ${config.per_trade_max_usd:.2f}
          ({config.max_position_pct * 100:.1f}% of budget)
        - Daily loss cap: ${-config.daily_loss_limit_usd:.2f}
        - Max drawdown: ${-config.max_drawdown_limit_usd:.2f}
        - Max concurrent positions: {config.max_concurrent_positions}
        - Max daily trades: {config.max_daily_trades}
        - Blacklist: {', '.join(config.blacklist) or '(none)'}
        - Risk tier: {config.risk_tier.value}
        - Per-spread max loss: ${config.max_option_loss_per_spread_usd:.2f}
          ({config.max_option_loss_per_spread_pct * 100:.1f}% of budget)
        - ALLOWED structures this tier: {', '.join(allowed)}

        Current state:
        - Cash available: ${snapshot.cash_balance:.2f}
        - Total deployed: ${snapshot.total_exposure_usd:.2f}
        - Remaining budget room: ${remaining_budget:.2f}
        - Day realized P&L: ${snapshot.day_realized_pnl:+.2f}
          (room before daily halt: ${remaining_daily:.2f})
        - Cumulative P&L: ${snapshot.cumulative_pnl:+.2f}
        - Trades today: {snapshot.daily_trade_count}/{config.max_daily_trades}

        Open positions:
{positions_str}
{_render_portfolio_risk_section(portfolio_risk_block)}

        Candidate symbols with latest prices (sourced from scout queue +
        movers + screener; open positions are always included so you can
        evaluate closes):
{prices_str}

        Discovery / top movers (live, liquidity-filtered):
{movers_str}

        Screener shortlist (full-universe needle-search — names moving
        with unusual volume across the entire tradable universe):
{shortlist_str}

        IV regime per candidate (near-ATM implied volatility; use to pick
        debit vs credit structures — rich IV favors selling, cheap IV favors
        buying):
{iv_str}

        Recent market news (general tape):
{market_news_str}

        Per-symbol headlines (last 48h):
{per_symbol_str}
{_render_findings_section(agent_findings_block)}{_render_lessons_section(lessons_block)}
        Decide your next action. Call propose_trade exactly once. Cite any
        specific headline in rationale when using it as a catalyst.
        """
    ).strip()


def _render_lessons_section(block: str | None) -> str:
    if not block:
        return ""
    return (
        "\n        Recent post-mortems (lessons from closed trades — weigh\n"
        "        before repeating a similar setup):\n"
        f"{block}\n"
    )


def _render_portfolio_risk_section(block: str | None) -> str:
    if not block:
        return ""
    return (
        "\n        Portfolio-risk snapshot (deterministic — use to size and\n"
        "        decide whether to add risk vs. rotate):\n"
        f"{block}\n"
    )


def _render_findings_section(block: str | None) -> str:
    if not block:
        return ""
    return (
        "\n        Per-symbol research findings (from parallel research agents\n"
        "        — use their bias + catalyst as the primary input, override\n"
        "        only if the tape clearly contradicts):\n"
        f"{block}\n"
    )


def _format_market_news(items: list[NewsItem]) -> str:
    if not items:
        return "  (none)"
    return "\n".join(
        f"  - [{n.source}] {n.headline[:200]}" for n in items if n.headline
    ) or "  (none)"


def _format_movers(movers: MoversSnapshot | None) -> str:
    if movers is None:
        return "  (discovery disabled)"

    def _fmt_pct(m) -> str:
        if m.percent_change is None:
            return ""
        return f"{m.percent_change:+.2f}%"

    def _fmt_row(m) -> str:
        price = f"${m.price:.2f}" if m.price is not None else "—"
        pct = _fmt_pct(m)
        vol = ""
        if m.volume is not None:
            vol = f" · vol {m.volume:,}"
        elif m.trade_count is not None:
            vol = f" · trades {m.trade_count:,}"
        return f"    · {m.symbol:<6} {price:<10} {pct:<9}{vol}"

    parts: list[str] = []
    if movers.gainers:
        parts.append("  Top gainers:")
        parts.extend(_fmt_row(m) for m in movers.gainers[:5])
    if movers.losers:
        parts.append("  Top losers:")
        parts.extend(_fmt_row(m) for m in movers.losers[:5])
    if movers.most_active:
        parts.append("  Most active (by volume):")
        parts.extend(_fmt_row(m) for m in movers.most_active[:5])
    return "\n".join(parts) or "  (none)"


def _format_shortlist(snap: ScreenerSnapshot | None) -> str:
    if snap is None or not snap.candidates:
        return "  (screener disabled or quiet)"
    lines: list[str] = [
        f"  scanned {snap.scored} names — top {len(snap.candidates)} by "
        "unusual volume (rank is vol_ratio only; you weigh the rest):"
    ]
    for c in snap.candidates:
        sign = "+" if c.pct_change >= 0 else ""
        gap_sign = "+" if c.gap_pct >= 0 else ""
        opt = " [opt]" if c.optionable else ""
        lines.append(
            f"    · {c.symbol:<6} ${c.price:<7.2f} "
            f"{sign}{c.pct_change * 100:>6.2f}%  "
            f"vol {c.vol_ratio:>4.1f}x  "
            f"gap {gap_sign}{c.gap_pct * 100:>5.2f}%  "
            f"rng {c.range_pct * 100:>5.2f}%{opt}"
        )
    return "\n".join(lines)


def _format_iv_regime(iv_by_symbol: dict[str, IvSnapshot]) -> str:
    if not iv_by_symbol:
        return "  (options data unavailable this cycle)"
    lines: list[str] = []
    for sym in sorted(iv_by_symbol):
        iv = iv_by_symbol[sym]
        if iv.atm_iv is None:
            lines.append(f"    · {sym:<6} iv=—      regime=unknown")
            continue
        lines.append(
            f"    · {sym:<6} iv={iv.atm_iv * 100:>5.1f}%  regime={iv.regime}"
            + (f"  exp={iv.expiry}" if iv.expiry else "")
        )
    return "\n".join(lines)


def _format_symbol_news(by_symbol: dict[str, list[NewsItem]]) -> str:
    if not by_symbol:
        return "  (none)"
    lines: list[str] = []
    for sym in sorted(by_symbol):
        items = by_symbol[sym]
        if not items:
            continue
        lines.append(f"  {sym}:")
        for n in items:
            if not n.headline:
                continue
            lines.append(f"    · {n.headline[:180]}")
    return "\n".join(lines) or "  (none)"
