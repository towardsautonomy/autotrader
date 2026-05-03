"""Aggregate time-series analytics for the dashboard graphs.

Computes equity curve, daily P&L, win rate, and decision-throughput series
on-demand from ``trades`` and ``decisions``. We don't persist equity
snapshots separately — walking the closed-trade log is cheap at the scales
this app runs at, and it guarantees the curve matches the ledger.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from datetime import UTC

from app.clock import NYSE, pacific_session_date  # noqa: F401
from app.api.schemas import (
    AiQualityStats,
    AnalyticsOut,
    DailyEquityPoint,
    DailyPnlPoint,
    DecisionBucketPoint,
    DecisionStats,
    DrawdownPoint,
    EquityPoint,
    HoldTimeBucket,
    HourBucket,
    LlmCostVsPnlPoint,
    PerformanceStats,
    PnlBySymbolBar,
    RollingWinRatePoint,
    TradeOutcomePoint,
    WinRateStats,
)
from app.models import Decision, LlmUsageRow, RiskConfigRow, Trade, TradeStatus, utc_now


async def collect_analytics(db: AsyncSession) -> AnalyticsOut:
    trades = (
        await db.execute(
            select(Trade).order_by(Trade.closed_at.asc().nullslast(), Trade.id.asc())
        )
    ).scalars().all()

    closed = [t for t in trades if t.status == TradeStatus.CLOSED and t.closed_at]

    # Equity curve: running cumulative realized P&L at each close event.
    equity_curve: list[EquityPoint] = []
    running = 0.0
    for t in closed:
        running += float(t.realized_pnl_usd or 0.0)
        equity_curve.append(
            EquityPoint(ts=t.closed_at, cumulative_pnl=round(running, 2))
        )

    # Daily P&L: bucket by closed_at date, also count trades.
    by_day: dict[date, dict[str, float]] = defaultdict(
        lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0}
    )
    for t in closed:
        d = pacific_session_date(t.closed_at)
        by_day[d]["pnl"] += float(t.realized_pnl_usd or 0.0)
        by_day[d]["trades"] += 1
        pnl = float(t.realized_pnl_usd or 0.0)
        if pnl > 0:
            by_day[d]["wins"] += 1
        elif pnl < 0:
            by_day[d]["losses"] += 1

    daily_pnl: list[DailyPnlPoint] = []
    for d in sorted(by_day.keys()):
        b = by_day[d]
        daily_pnl.append(
            DailyPnlPoint(
                day=d,
                realized_pnl=round(b["pnl"], 2),
                trade_count=int(b["trades"]),
                wins=int(b["wins"]),
                losses=int(b["losses"]),
            )
        )

    # Win rate: overall tallies across all closed trades.
    wins = sum(1 for t in closed if float(t.realized_pnl_usd or 0.0) > 0)
    losses = sum(1 for t in closed if float(t.realized_pnl_usd or 0.0) < 0)
    breakeven = len(closed) - wins - losses
    win_rate_pct = (wins / len(closed) * 100.0) if closed else 0.0
    avg_win = (
        sum(float(t.realized_pnl_usd) for t in closed if float(t.realized_pnl_usd) > 0)
        / wins
        if wins
        else 0.0
    )
    avg_loss = (
        sum(float(t.realized_pnl_usd) for t in closed if float(t.realized_pnl_usd) < 0)
        / losses
        if losses
        else 0.0
    )
    win_rate = WinRateStats(
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        total=len(closed),
        win_rate_pct=round(win_rate_pct, 2),
        avg_win_usd=round(avg_win, 2),
        avg_loss_usd=round(avg_loss, 2),
    )

    # Trade outcomes: pass through the last ~200 closed trades so the UI can
    # render a dot-per-trade P&L scatter without hitting /trades twice.
    outcomes: list[TradeOutcomePoint] = [
        TradeOutcomePoint(
            id=t.id,
            symbol=t.symbol,
            action=t.action,
            closed_at=t.closed_at,
            realized_pnl_usd=round(float(t.realized_pnl_usd or 0.0), 2),
            size_usd=round(float(t.size_usd or 0.0), 2),
        )
        for t in closed[-200:]
    ]

    # Decision stats + per-day bucket.
    decisions = (
        await db.execute(select(Decision).order_by(Decision.created_at.asc()))
    ).scalars().all()

    total = len(decisions)
    approved = sum(1 for d in decisions if d.approved)
    executed = sum(1 for d in decisions if d.executed)
    rejected = total - approved
    decision_stats = DecisionStats(
        total=total,
        approved=approved,
        rejected=rejected,
        executed=executed,
    )

    by_dec_day: dict[date, dict[str, int]] = defaultdict(
        lambda: {"approved": 0, "rejected": 0, "executed": 0}
    )
    for d in decisions:
        day = pacific_session_date(d.created_at)
        if d.approved:
            by_dec_day[day]["approved"] += 1
        else:
            by_dec_day[day]["rejected"] += 1
        if d.executed:
            by_dec_day[day]["executed"] += 1

    # Pad the decision timeline across the observed window so the chart has
    # zero-bars on idle days rather than collapsing to a ragged series.
    decision_timeline: list[DecisionBucketPoint] = []
    if by_dec_day:
        start = min(by_dec_day.keys())
        end = max(by_dec_day.keys())
        cur = start
        while cur <= end:
            b = by_dec_day.get(cur, {"approved": 0, "rejected": 0, "executed": 0})
            decision_timeline.append(
                DecisionBucketPoint(
                    day=cur,
                    approved=b["approved"],
                    rejected=b["rejected"],
                    executed=b["executed"],
                )
            )
            cur = cur + timedelta(days=1)

    rolling_win_rate = _rolling_win_rate(closed, window=20)
    hold_time_distribution = _hold_time_distribution(closed)
    pnl_by_symbol = _pnl_by_symbol(closed)
    llm_cost_vs_pnl = await _llm_cost_vs_pnl(db, by_day)

    active_cfg = (
        await db.execute(
            select(RiskConfigRow).where(RiskConfigRow.is_active.is_(True))
        )
    ).scalar_one_or_none()
    budget_cap_usd = float(active_cfg.budget_cap) if active_cfg else 0.0

    drawdown_curve = _drawdown_curve(equity_curve)
    performance = _performance_stats(closed, drawdown_curve, budget_cap_usd)
    equity_curve_daily = _daily_equity_curve(closed)
    hour_of_day = _hour_of_day_distribution(closed)
    ai_quality = await _ai_quality_stats(db, decisions, closed)

    return AnalyticsOut(
        equity_curve=equity_curve,
        equity_curve_daily=equity_curve_daily,
        drawdown_curve=drawdown_curve,
        daily_pnl=daily_pnl,
        decision_stats=decision_stats,
        decision_timeline=decision_timeline,
        win_rate=win_rate,
        performance=performance,
        ai_quality=ai_quality,
        trade_outcomes=outcomes,
        rolling_win_rate=rolling_win_rate,
        llm_cost_vs_pnl=llm_cost_vs_pnl,
        hold_time_distribution=hold_time_distribution,
        hour_of_day_distribution=hour_of_day,
        pnl_by_symbol=pnl_by_symbol,
        budget_cap_usd=budget_cap_usd,
        generated_at=utc_now(),
    )


def _rolling_win_rate(
    closed: list[Trade], *, window: int = 20
) -> list[RollingWinRatePoint]:
    """Rolling win rate over the last `window` closed trades at each close."""
    points: list[RollingWinRatePoint] = []
    wins_flags: list[int] = []
    for idx, t in enumerate(closed, start=1):
        pnl = float(t.realized_pnl_usd or 0.0)
        wins_flags.append(1 if pnl > 0 else 0)
        bucket = wins_flags[-window:]
        rate = sum(bucket) / len(bucket) * 100.0 if bucket else 0.0
        points.append(
            RollingWinRatePoint(
                trade_index=idx,
                closed_at=t.closed_at,
                window_size=len(bucket),
                win_rate_pct=round(rate, 2),
            )
        )
    return points


_HOLD_BUCKETS: list[tuple[str, float]] = [
    ("<5m", 5.0),
    ("5-15m", 15.0),
    ("15-60m", 60.0),
    ("1-4h", 240.0),
    ("4h+", float("inf")),
]


def _hold_time_distribution(closed: list[Trade]) -> list[HoldTimeBucket]:
    counts = {label: {"wins": 0, "losses": 0, "count": 0} for label, _ in _HOLD_BUCKETS}
    for t in closed:
        if t.opened_at is None or t.closed_at is None:
            continue
        minutes = (t.closed_at - t.opened_at).total_seconds() / 60.0
        label = next(lbl for lbl, ceiling in _HOLD_BUCKETS if minutes < ceiling)
        pnl = float(t.realized_pnl_usd or 0.0)
        counts[label]["count"] += 1
        if pnl > 0:
            counts[label]["wins"] += 1
        elif pnl < 0:
            counts[label]["losses"] += 1
    return [
        HoldTimeBucket(
            bucket=label,
            wins=counts[label]["wins"],
            losses=counts[label]["losses"],
            count=counts[label]["count"],
        )
        for label, _ in _HOLD_BUCKETS
    ]


def _pnl_by_symbol(closed: list[Trade], *, top_k: int = 15) -> list[PnlBySymbolBar]:
    """Aggregate realized P&L by underlying symbol.

    Returns the top-k by absolute P&L so both big winners and big losers
    surface, instead of only the biggest winners dominating."""
    agg: dict[str, dict[str, float]] = defaultdict(
        lambda: {"pnl": 0.0, "count": 0, "wins": 0, "losses": 0}
    )
    for t in closed:
        pnl = float(t.realized_pnl_usd or 0.0)
        bucket = agg[t.symbol]
        bucket["pnl"] += pnl
        bucket["count"] += 1
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
    bars = [
        PnlBySymbolBar(
            symbol=sym,
            realized_pnl_usd=round(b["pnl"], 2),
            trade_count=int(b["count"]),
            wins=int(b["wins"]),
            losses=int(b["losses"]),
        )
        for sym, b in agg.items()
    ]
    bars.sort(key=lambda b: abs(b.realized_pnl_usd), reverse=True)
    return bars[:top_k]


async def _llm_cost_vs_pnl(
    db: AsyncSession, by_day: dict[date, dict[str, float]]
) -> list[LlmCostVsPnlPoint]:
    cost_rows = (
        await db.execute(select(LlmUsageRow))
    ).scalars().all()
    by_day_cost: dict[date, float] = defaultdict(float)
    for row in cost_rows:
        if row.created_at is None:
            continue
        cost = float(row.cost_usd or 0.0)
        if cost <= 0:
            continue
        by_day_cost[pacific_session_date(row.created_at)] += cost
    days = set(by_day_cost.keys()) | set(by_day.keys())
    out: list[LlmCostVsPnlPoint] = []
    for d in sorted(days):
        out.append(
            LlmCostVsPnlPoint(
                day=d,
                llm_cost_usd=round(by_day_cost.get(d, 0.0), 4),
                realized_pnl_usd=round(
                    by_day.get(d, {}).get("pnl", 0.0), 2
                ),
            )
        )
    return out


def _drawdown_curve(equity_curve: list[EquityPoint]) -> list[DrawdownPoint]:
    """Running peak-to-trough drawdown, one point per close event.

    At each equity point, drawdown is ``min(0, equity - running_peak)``.
    Series is always ≤ 0; 0 means we're at a new high-water mark.
    """
    points: list[DrawdownPoint] = []
    peak = 0.0
    for p in equity_curve:
        peak = max(peak, p.cumulative_pnl)
        dd = p.cumulative_pnl - peak  # ≤ 0
        points.append(DrawdownPoint(ts=p.ts, drawdown_usd=round(dd, 2)))
    return points


def _performance_stats(
    closed: list[Trade],
    drawdown_curve: list[DrawdownPoint],
    budget_cap_usd: float,
) -> PerformanceStats:
    if not closed:
        return PerformanceStats()

    pnls = [float(t.realized_pnl_usd or 0.0) for t in closed]
    wins_sum = sum(p for p in pnls if p > 0)
    losses_sum = sum(p for p in pnls if p < 0)
    profit_factor = (wins_sum / abs(losses_sum)) if losses_sum < 0 else None
    expectancy = sum(pnls) / len(pnls)

    # Current + longest streaks from the signed P&L sequence. A
    # breakeven trade resets the streak.
    cur = 0
    longest_win = 0
    longest_loss = 0
    for p in pnls:
        if p > 0:
            cur = cur + 1 if cur > 0 else 1
            longest_win = max(longest_win, cur)
        elif p < 0:
            cur = cur - 1 if cur < 0 else -1
            longest_loss = max(longest_loss, -cur)
        else:
            cur = 0

    max_dd_usd = min((pt.drawdown_usd for pt in drawdown_curve), default=0.0)
    max_dd_pct = (
        abs(max_dd_usd) / budget_cap_usd * 100.0 if budget_cap_usd > 0 else 0.0
    )

    return PerformanceStats(
        max_drawdown_usd=round(max_dd_usd, 2),
        max_drawdown_pct=round(max_dd_pct, 2),
        profit_factor=(
            round(profit_factor, 2) if profit_factor is not None else None
        ),
        expectancy_usd=round(expectancy, 2),
        current_streak=cur,
        longest_win_streak=longest_win,
        longest_loss_streak=longest_loss,
    )


def _daily_equity_curve(closed: list[Trade]) -> list[DailyEquityPoint]:
    """End-of-day cumulative realized P&L, one point per Pacific trading day.

    Unlike ``equity_curve`` (one point per trade close, at sub-second
    precision), this series is aligned to calendar days so daily
    benchmark bars (SPY etc.) can overlay cleanly. Days with no closed
    trades are padded — the running cumulative stays flat.
    """
    if not closed:
        return []
    by_day: dict[date, float] = defaultdict(float)
    for t in closed:
        by_day[pacific_session_date(t.closed_at)] += float(t.realized_pnl_usd or 0.0)
    start = min(by_day.keys())
    end = max(by_day.keys())
    out: list[DailyEquityPoint] = []
    running = 0.0
    cur = start
    while cur <= end:
        running += by_day.get(cur, 0.0)
        out.append(DailyEquityPoint(day=cur, cumulative_pnl=round(running, 2)))
        cur = cur + timedelta(days=1)
    return out


def _hour_of_day_distribution(closed: list[Trade]) -> list[HourBucket]:
    """Trade outcomes bucketed by NY-market-local close hour (0-23).

    Only market-hour hours (9-16 ET) will generally populate, but we
    return all 24 so the chart draws cleanly with zero bars on silent
    hours — easier to read than a ragged axis.
    """
    buckets: dict[int, dict[str, float]] = {
        h: {"wins": 0, "losses": 0, "pnl": 0.0} for h in range(24)
    }
    for t in closed:
        if t.closed_at is None:
            continue
        ts = t.closed_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        h = ts.astimezone(NYSE).hour
        pnl = float(t.realized_pnl_usd or 0.0)
        buckets[h]["pnl"] += pnl
        if pnl > 0:
            buckets[h]["wins"] += 1
        elif pnl < 0:
            buckets[h]["losses"] += 1
    return [
        HourBucket(
            hour=h,
            wins=int(buckets[h]["wins"]),
            losses=int(buckets[h]["losses"]),
            realized_pnl_usd=round(buckets[h]["pnl"], 2),
        )
        for h in range(24)
    ]


async def _ai_quality_stats(
    db: AsyncSession,
    decisions: list[Decision],
    closed: list[Trade],
) -> AiQualityStats:
    """Derive AI-quality signal from the decision + trade + llm-usage logs.

    Confidence gap (wins − losses) is the best single signal that the
    model's confidence calibration is useful. Cost-per-dollar-of-P&L
    tells us whether the LLM spend pays for itself.
    """
    # Build a (symbol, created_at) index of executed decisions so we can
    # reunite each closed trade with the decision that opened it. The
    # Trade.decision_id FK exists but is not populated by the trading
    # loop, so we match by symbol + time-proximity (decision created_at
    # is within ~60s before trade opened_at). In observed data the two
    # timestamps are identical to millisecond precision.
    dec_by_id = {d.id: d for d in decisions}  # noqa: F841 (future linkage)
    exec_decisions = [d for d in decisions if d.executed]

    def _match_decision_for_trade(t: Trade) -> Decision | None:
        if t.opened_at is None:
            return None
        opened = t.opened_at
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=UTC)
        best: Decision | None = None
        best_delta = 120.0  # seconds
        for d in exec_decisions:
            prop = d.proposal_json or {}
            if str(prop.get("symbol") or "").upper() != t.symbol.upper():
                continue
            created = d.created_at
            if created is None:
                continue
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            delta = abs((opened - created).total_seconds())
            if delta < best_delta:
                best_delta = delta
                best = d
        return best

    conf_wins: list[float] = []
    conf_losses: list[float] = []
    conf_rejected: list[float] = []
    latencies: list[float] = []
    for t in closed:
        d = _match_decision_for_trade(t)
        if d is None:
            continue
        prop = d.proposal_json or {}
        conf = prop.get("confidence")
        if isinstance(conf, (int, float)):
            pnl = float(t.realized_pnl_usd or 0.0)
            if pnl > 0:
                conf_wins.append(float(conf))
            elif pnl < 0:
                conf_losses.append(float(conf))
        if t.opened_at and d.created_at:
            opened = t.opened_at
            created = d.created_at
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            delta = (opened - created).total_seconds()
            if delta >= 0:
                latencies.append(delta)
    for d in decisions:
        if d.approved:
            continue
        prop = d.proposal_json or {}
        conf = prop.get("confidence")
        if isinstance(conf, (int, float)):
            conf_rejected.append(float(conf))

    cost_rows = (await db.execute(select(LlmUsageRow))).scalars().all()
    total_spend = sum(float(r.cost_usd or 0.0) for r in cost_rows)
    executed_count = sum(1 for d in decisions if d.executed)
    net_pnl = sum(float(t.realized_pnl_usd or 0.0) for t in closed)

    def _avg(xs: list[float]) -> float | None:
        return round(sum(xs) / len(xs), 3) if xs else None

    def _median(xs: list[float]) -> float | None:
        if not xs:
            return None
        s = sorted(xs)
        mid = len(s) // 2
        if len(s) % 2:
            return round(s[mid], 2)
        return round((s[mid - 1] + s[mid]) / 2.0, 2)

    return AiQualityStats(
        executed_decisions=executed_count,
        avg_confidence_wins=_avg(conf_wins),
        avg_confidence_losses=_avg(conf_losses),
        avg_confidence_rejected=_avg(conf_rejected),
        median_exec_latency_sec=_median(latencies),
        total_llm_spend_usd=round(total_spend, 4),
        cost_per_executed_decision_usd=(
            round(total_spend / executed_count, 4) if executed_count else None
        ),
        cost_per_dollar_pnl=(
            round(total_spend / abs(net_pnl), 4) if net_pnl else None
        ),
    )


__all__ = ["collect_analytics"]
