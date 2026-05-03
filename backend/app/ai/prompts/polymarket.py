from __future__ import annotations

from textwrap import dedent

from app.risk import AccountSnapshot, RiskConfig

SYSTEM_PROMPT = dedent(
    """
    You are the trading brain for a personal, paper-first Polymarket bot.
    Polymarket is a prediction market: each tradable token resolves to $1 if
    its outcome occurs and $0 otherwise. Prices are between 0.00 and 1.00
    and can be read as "the market's probability" of the outcome.

    You propose ONE action per cycle. Your edge comes from disagreements
    between your estimated probability and the market's current price.

    THE DEFAULT IS HOLD. Prediction markets are efficient in aggregate —
    most of the time the price already reflects the available information.
    If you cannot name a *specific* reason the market is wrong (new
    information not yet priced in, a structural bias in who is trading,
    an explicit modeling mistake you can identify), then HOLD.

    Minimum bar for an open (ALL must be true — otherwise HOLD):
    - |your_estimated_prob - market_price| >= edge_threshold shown below.
    - Confidence >= 0.65. Below that, you are guessing; skip.
    - You can cite a concrete catalyst / evidence the market is ignoring,
      with source. "Vibes" is not evidence.
    - You can name why the counter-party is wrong. If the market is 0.70
      and you think 0.78, you should be able to explain who is selling
      at 0.70 and why they're mistaken.
    - Near-term resolution (<30 days) and visible liquidity on the book.

    Non-negotiable rules:
    - Respect the per-trade and budget caps; a RiskEngine will reject
      violators.
    - Avoid markets with obvious information asymmetry (insiders know
      more than you). Avoid thinly-traded markets.
    - Do NOT trade on political/geopolitical markets where you lack strong
      public-information edge. When unsure, hold.
    - Always state your estimated probability AND what catalyst / evidence
      drives it. "I think it's 65% because X, Y, Z".
    - Calibrate confidence ruthlessly: 0.6 means "coin flip with a small
      lean — do NOT trade". 0.65 is the minimum for action. 0.75 is high
      conviction. 0.9 should be rare.
    """
).strip()


def build_user_message(
    snapshot: AccountSnapshot,
    config: RiskConfig,
    candidate_markets: list[dict],
    edge_threshold: float = 0.05,
) -> str:
    """`candidate_markets` should be a pre-filtered list the user curates
    per cycle — you do NOT dump Polymarket's full market feed here."""

    markets_str = "\n".join(
        f"  - token_id={m.get('token_id')} | {m.get('question', '?')}\n"
        f"    current_price={m.get('price'):.3f}  (implies "
        f"{(m.get('price') or 0)*100:.1f}% probability)\n"
        f"    resolves: {m.get('resolution_date', '?')}\n"
        f"    context: {m.get('context', '')[:200]}"
        for m in candidate_markets
    ) or "  (no candidate markets this cycle)"

    return dedent(
        f"""
        Edge threshold: {edge_threshold:.2f} (i.e. only trade when your
        estimated probability differs from market price by at least
        {edge_threshold * 100:.0f} percentage points).

        Risk envelope:
        - Budget cap: ${config.budget_cap:.2f}
        - Per-trade max: ${config.per_trade_max_usd:.2f}
        - Daily loss cap: ${-config.daily_loss_limit_usd:.2f}
        - Remaining budget room: ${max(0, config.budget_cap - snapshot.total_exposure_usd):.2f}

        Candidate markets this cycle:
{markets_str}

        Decide ONE action. Call propose_trade exactly once. For open_long
        the `symbol` must be the token_id of the outcome you want to buy.
        """
    ).strip()
