from __future__ import annotations

import asyncio
from datetime import date as _date

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.activity import ActivityEvent, EventSeverity, get_bus
from app.api.deps import get_db, require_api_key
from app.api.schemas import (
    AccountOut,
    ActivityEventOut,
    AgentRoleOut,
    AgentRosterOut,
    AgentsOverviewOut,
    AgentSummaryOut,
    AgentTaskStep,
    BenchmarkBar,
    BenchmarkOut,
    BenchmarkSeries,
    CycleAgentOut,
    CycleOut,
    CyclesOverviewOut,
    AIModelInfo,
    AIStatusOut,
    AnalyticsOut,
    DecisionOut,
    FlipModeIn,
    GPUInfo,
    KillSwitchIn,
    LlmCallDetailOut,
    LlmRateCardIn,
    LlmRateCardOut,
    LlmRateEntry,
    LlmUsageBucket,
    LlmUsageRowOut,
    LlmUsageSummaryOut,
    MarketIntelOut,
    OptionChainOut,
    OptionContractOut,
    PauseWhenClosedIn,
    PositionOut,
    PositionVerdictOut,
    RiskConfigGenerateIn,
    RiskConfigIn,
    RiskConfigOut,
    ScoutCandidateOut,
    ScoutQueueOut,
    TradeOut,
)
from app.brokers import build_broker
from app.config import get_settings
from app.models import (
    ActivityEventRow,
    AuditLog,
    Decision,
    Halt,
    LlmRateCardRow,
    LlmUsageRow,
    RiskConfigRow,
    SystemState,
    Trade,
    TradeStatus,
    utc_now,
)
from app.risk import Market, load_active_paper_cost_bps, realized_pnl_usd
from app.runtime import get_candidate_queue
from app.scheduler.budget import today_cost_usd
from app.scheduler.locks import get_lock
from app.scheduler.snapshot import build_snapshot

router = APIRouter()


@router.get("/health")
async def health():
    # Liveness probe only — unauthenticated. Anything that could leak
    # operational state (mode, scheduler heartbeat, pending restart) lives
    # behind the API key on /system/status instead.
    return {"status": "ok"}


@router.get("/system/status", dependencies=[Depends(require_api_key)])
async def system_status(db: AsyncSession = Depends(get_db)):
    from app.scheduler import heartbeat

    s = get_settings()
    state = await db.get(SystemState, 1)
    active_mode = "PAPER" if s.paper_mode else "LIVE"
    # Scheduler heartbeat lets an external watchdog detect silent stalls
    # (APScheduler dropped ticks, a tick stuck upstream of _safe_call,
    # event loop frozen). Loops that have never run are omitted — a
    # missing label = "not booted yet or crashed on first tick".
    now = utc_now()
    sched: dict[str, dict[str, float | str]] = {}
    for label, ts in heartbeat.snapshot().items():
        sched[label] = {
            "last_tick": ts.isoformat(),
            "seconds_ago": round((now - ts).total_seconds(), 1),
        }
    payload: dict = {
        "mode": active_mode,
        "active_mode": active_mode,
        "pending_restart": False,
        "scheduler": sched,
        "status": "ok",
    }
    if state is not None:
        configured_mode = "PAPER" if state.paper_mode else "LIVE"
        payload["mode"] = configured_mode
        payload["pending_restart"] = configured_mode != active_mode
    return payload


# ---------- Account / state ----------


@router.get("/account", response_model=AccountOut, dependencies=[Depends(require_api_key)])
async def get_account(db: AsyncSession = Depends(get_db)):
    from app.api.intel import _latest_decisions_by_symbol

    settings = get_settings()
    broker = build_broker(Market.STOCKS, settings)
    snap = await build_snapshot(broker, db)
    state = await db.get(SystemState, 1)
    paused = bool(state.agents_paused) if state else False
    pause_when_closed = bool(state.pause_when_market_closed) if state else False
    configured_mode = (
        ("PAPER" if state.paper_mode else "LIVE") if state else settings.mode_label
    )
    active_mode = settings.mode_label

    stock_symbols = [p.symbol for p in snap.positions if p.market == Market.STOCKS]
    verdicts_by_symbol = await _latest_decisions_by_symbol(
        db, stock_symbols, position_symbols=set(stock_symbols)
    )

    return AccountOut(
        mode=configured_mode,
        active_mode=active_mode,
        trading_enabled=snap.trading_enabled,
        agents_paused=paused,
        pause_when_market_closed=pause_when_closed,
        cash_balance=snap.cash_balance,
        total_exposure=snap.total_exposure_usd,
        total_equity=snap.total_equity,
        day_realized_pnl=snap.day_realized_pnl,
        cumulative_pnl=snap.cumulative_pnl,
        daily_trade_count=snap.daily_trade_count,
        positions=[
            PositionOut(
                market=p.market.value,
                symbol=p.symbol,
                size_usd=p.size_usd,
                entry_price=p.entry_price,
                current_price=p.current_price,
                unrealized_pnl=p.unrealized_pnl,
                last_verdict=_verdict_for_position(verdicts_by_symbol.get(p.symbol))
                if p.market == Market.STOCKS
                else None,
            )
            for p in snap.positions
        ],
    )


def _verdict_for_position(src) -> PositionVerdictOut | None:
    if src is None:
        return None
    if src.action == "hold":
        status = "HOLD"
    elif src.executed:
        status = "EXECUTED"
    elif src.approved:
        status = "APPROVED"
    else:
        status = "REJECTED"
    return PositionVerdictOut(
        status=status,
        action=src.action,
        rationale=src.rationale,
        rejection_code=src.rejection_code,
        created_at=src.created_at,
    )


# ---------- Decisions / trades history ----------


@router.get(
    "/decisions",
    response_model=list[DecisionOut],
    dependencies=[Depends(require_api_key)],
)
async def list_decisions(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Decision)
            .order_by(desc(Decision.created_at))
            .offset(offset)
            .limit(limit)
        )
    ).scalars().all()
    return [DecisionOut.model_validate(d, from_attributes=True) for d in rows]


@router.get(
    "/trades", response_model=list[TradeOut], dependencies=[Depends(require_api_key)]
)
async def list_trades(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    market: str | None = None,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Trade).order_by(desc(Trade.created_at))
    if market:
        stmt = stmt.where(Trade.market == market)
    if status:
        stmt = stmt.where(Trade.status == status)
    rows = (await db.execute(stmt.offset(offset).limit(limit))).scalars().all()
    return [TradeOut.model_validate(t, from_attributes=True) for t in rows]


# ---------- Risk config ----------


@router.get(
    "/risk-config",
    response_model=RiskConfigOut,
    dependencies=[Depends(require_api_key)],
)
async def get_active_risk_config(db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(
            select(RiskConfigRow)
            .where(RiskConfigRow.is_active.is_(True))
            .order_by(desc(RiskConfigRow.id))
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(404, "no active risk config — seed one first")
    return RiskConfigOut.model_validate(row, from_attributes=True)


@router.put(
    "/risk-config",
    response_model=RiskConfigOut,
    dependencies=[Depends(require_api_key)],
)
async def update_risk_config(
    payload: RiskConfigIn, db: AsyncSession = Depends(get_db)
):
    # Deactivate previous, insert new, mark new active
    await db.execute(update(RiskConfigRow).values(is_active=False))
    new_row = RiskConfigRow(
        is_active=True,
        changed_by="api",
        **payload.model_dump(),
    )
    db.add(new_row)
    db.add(
        AuditLog(
            event_type="risk_config_changed",
            message="risk config updated via API",
            payload=payload.model_dump(),
        )
    )
    await db.commit()
    return RiskConfigOut.model_validate(new_row, from_attributes=True)


# ---------- Safety constraints (UI warnings) ----------


@router.get(
    "/risk-config/warnings",
    dependencies=[Depends(require_api_key)],
)
async def get_risk_config_warnings(db: AsyncSession = Depends(get_db)):
    """Evaluate the active risk config against the safety-constraint
    registry and return any violations. The frontend renders these as
    red/yellow banners on /risk-config so the user sees gotchas (PDT
    rule, spread-vs-notional, options-contract-vs-budget) before
    committing a too-tight setup.
    """
    from app.safety import evaluate_constraints, list_constraints

    row = (
        await db.execute(
            select(RiskConfigRow)
            .where(RiskConfigRow.is_active.is_(True))
            .order_by(desc(RiskConfigRow.id))
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        return {"definitions": list_constraints(), "violations": []}
    cfg = row.to_dataclass()
    violations = evaluate_constraints(cfg)
    return {
        "definitions": list_constraints(),
        "violations": [
            {
                "key": v.key,
                "severity": v.severity,
                "title": v.title,
                "description": v.description,
                "remedy": v.remedy,
            }
            for v in violations
        ],
    }


@router.post(
    "/risk-config/evaluate",
    dependencies=[Depends(require_api_key)],
)
async def evaluate_risk_config(payload: RiskConfigIn):
    """Preview constraint violations for an unsaved config. Lets the
    UI warn live as the user types, without committing."""
    from app.safety import evaluate_constraints

    from app.risk import RiskConfig, RiskTier

    try:
        cfg = RiskConfig(
            budget_cap=payload.budget_cap,
            max_position_pct=payload.max_position_pct,
            max_concurrent_positions=payload.max_concurrent_positions,
            max_daily_trades=payload.max_daily_trades,
            daily_loss_cap_pct=payload.daily_loss_cap_pct,
            max_drawdown_pct=payload.max_drawdown_pct,
            default_stop_loss_pct=payload.default_stop_loss_pct,
            default_take_profit_pct=payload.default_take_profit_pct,
            min_trade_size_usd=payload.min_trade_size_usd,
            blacklist=tuple(payload.blacklist),
            risk_tier=RiskTier(payload.risk_tier),
            max_option_loss_per_spread_pct=payload.max_option_loss_per_spread_pct,
            earnings_blackout_days=payload.earnings_blackout_days,
            max_stop_loss_pct=payload.max_stop_loss_pct,
            paper_cost_bps=payload.paper_cost_bps,
            pdt_day_trade_count_5bd=payload.pdt_day_trade_count_5bd,
            min_open_confidence=payload.min_open_confidence,
            min_reward_risk_ratio=payload.min_reward_risk_ratio,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    violations = evaluate_constraints(cfg)
    return {
        "violations": [
            {
                "key": v.key,
                "severity": v.severity,
                "title": v.title,
                "description": v.description,
                "remedy": v.remedy,
            }
            for v in violations
        ],
    }


@router.post(
    "/risk-config/generate",
    dependencies=[Depends(require_api_key)],
)
async def generate_risk_config_ai(payload: RiskConfigGenerateIn):
    """Ask the LLM for a full risk-config tailored to the user's budget.

    Returns a preview payload (same shape as RiskConfigIn) + rationale.
    Does NOT persist anything — the UI decides whether to apply.
    """
    from app.ai.llm_provider import build_provider_from_settings
    from app.ai.risk_config_generator import generate_risk_config
    from app.db import get_session_factory

    settings = get_settings()
    try:
        provider = build_provider_from_settings(settings)
    except Exception:
        provider = None

    result = await generate_risk_config(
        provider=provider,
        session_factory=get_session_factory(),
        budget_cap=payload.budget_cap,
        preference=payload.preference,
    )
    return result


# ---------- Mode flip (paper <-> live) ----------


_LIVE_CONFIRM = "I UNDERSTAND I CAN LOSE REAL MONEY"


@router.post("/mode", dependencies=[Depends(require_api_key)])
async def set_mode(payload: FlipModeIn, db: AsyncSession = Depends(get_db)):
    """Queue a paper<->live flip. Stored on SystemState. The live broker
    instance is built at boot, so the flip takes effect after the backend
    restarts — UI surfaces the pending state until then."""
    target = payload.target_mode.strip().upper()
    if target not in ("PAPER", "LIVE"):
        raise HTTPException(400, "target_mode must be 'PAPER' or 'LIVE'")
    if target == "LIVE" and payload.confirm_phrase != _LIVE_CONFIRM:
        raise HTTPException(
            400,
            f"confirm_phrase must equal '{_LIVE_CONFIRM}' when flipping to LIVE",
        )

    new_paper = target == "PAPER"
    settings = get_settings()
    active_mode = settings.mode_label

    state = await db.get(SystemState, 1)
    if state is None:
        state = SystemState(
            id=1,
            trading_enabled=True,
            agents_paused=False,
            pause_when_market_closed=False,
            paper_mode=new_paper,
        )
        db.add(state)
    else:
        state.paper_mode = new_paper

    db.add(
        AuditLog(
            event_type="mode_flipped",
            message=f"queued target_mode={target} (active={active_mode})",
        )
    )
    await db.commit()
    get_bus().publish(
        "mode.flipped",
        f"mode flip queued: {target} (restart backend to apply)",
        severity=EventSeverity.WARN if target == "LIVE" else EventSeverity.INFO,
    )
    return {
        "mode": target,
        "active_mode": active_mode,
        "pending_restart": target != active_mode,
    }


# ---------- Kill switch / unpause ----------


@router.post("/kill-switch", dependencies=[Depends(require_api_key)])
async def kill_switch(payload: KillSwitchIn, db: AsyncSession = Depends(get_db)):
    if payload.confirm != "KILL":
        raise HTTPException(400, "confirm must equal 'KILL'")

    state = await db.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1, trading_enabled=False)
        db.add(state)
    else:
        state.trading_enabled = False

    db.add(
        Halt(
            reason_code="kill_switch",
            reason=payload.reason or "user pressed kill switch",
            started_at=utc_now(),
        )
    )
    db.add(
        AuditLog(
            event_type="kill_switch_pressed",
            message=payload.reason or "user pressed kill switch",
        )
    )
    await db.commit()
    get_bus().publish(
        "halt.engaged",
        f"kill switch: {payload.reason or 'user pressed kill switch'}",
        severity=EventSeverity.ERROR,
    )
    return {"trading_enabled": False}


@router.post("/unpause", dependencies=[Depends(require_api_key)])
async def unpause(db: AsyncSession = Depends(get_db)):
    state = await db.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1, trading_enabled=True)
        db.add(state)
    else:
        state.trading_enabled = True

    # Close most recent open halt
    open_halt = (
        await db.execute(
            select(Halt).where(Halt.ended_at.is_(None)).order_by(desc(Halt.id)).limit(1)
        )
    ).scalars().first()
    if open_halt is not None:
        open_halt.ended_at = utc_now()
        open_halt.unpaused_by = "api"

    db.add(AuditLog(event_type="unpaused", message="trading re-enabled"))
    await db.commit()
    get_bus().publish(
        "halt.cleared",
        "trading re-enabled by user",
        severity=EventSeverity.SUCCESS,
    )
    return {"trading_enabled": True}


# ---------- Agents pause / resume ----------


@router.post("/agents/pause", dependencies=[Depends(require_api_key)])
async def pause_agents(db: AsyncSession = Depends(get_db)):
    """Pause every scheduled loop (decision, scout, monitor). Order flow
    is already blocked by the kill switch; this stops the AI from doing
    any work at all — research included."""
    state = await db.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1, trading_enabled=True, agents_paused=True)
        db.add(state)
    else:
        state.agents_paused = True

    db.add(AuditLog(event_type="agents_paused", message="all agents paused"))
    await db.commit()
    get_bus().publish(
        "agents.paused",
        "all agents paused by user",
        severity=EventSeverity.WARN,
    )
    return {"agents_paused": True}


@router.post("/agents/resume", dependencies=[Depends(require_api_key)])
async def resume_agents(db: AsyncSession = Depends(get_db)):
    state = await db.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1, trading_enabled=True, agents_paused=False)
        db.add(state)
    else:
        state.agents_paused = False

    db.add(AuditLog(event_type="agents_resumed", message="agents resumed"))
    await db.commit()
    get_bus().publish(
        "agents.resumed",
        "all agents resumed",
        severity=EventSeverity.SUCCESS,
    )
    return {"agents_paused": False}


@router.post(
    "/agents/pause-when-closed", dependencies=[Depends(require_api_key)]
)
async def set_pause_when_closed(
    payload: PauseWhenClosedIn, db: AsyncSession = Depends(get_db)
):
    """Toggle auto-idle during market close. Decision + scout loops skip
    their ticks whenever the US equity session is closed; monitor keeps
    running. Flips off by setting enabled=false."""
    state = await db.get(SystemState, 1)
    if state is None:
        state = SystemState(
            id=1,
            trading_enabled=True,
            agents_paused=False,
            pause_when_market_closed=payload.enabled,
        )
        db.add(state)
    else:
        state.pause_when_market_closed = payload.enabled

    db.add(
        AuditLog(
            event_type="agents_pause_when_closed",
            message=f"pause_when_market_closed={payload.enabled}",
        )
    )
    await db.commit()
    get_bus().publish(
        "agents.pause_when_closed",
        f"auto-idle on market close: {'on' if payload.enabled else 'off'}",
        severity=EventSeverity.INFO,
    )
    return {"pause_when_market_closed": payload.enabled}


# ---------- Manual close ----------


async def _close_trade_row(trade: Trade, db: AsyncSession) -> dict:
    """Route a close through the right broker path, mark the row CLOSED."""
    settings = get_settings()
    broker = build_broker(Market(trade.market), settings)
    if trade.option_json:
        result = await broker.close_option_position(trade.option_json)
    else:
        result = await broker.close_position(trade.symbol)

    if not result.success:
        return {
            "trade_id": trade.id,
            "symbol": trade.symbol,
            "success": False,
            "error": result.error,
        }

    trade.status = TradeStatus.CLOSED
    trade.closed_at = utc_now()
    trade.exit_price = result.fill_price
    trade.broker_close_order_id = result.broker_order_id
    if (
        result.fill_price is not None
        and trade.entry_price
        and not trade.option_json
    ):
        bps = await load_active_paper_cost_bps(db)
        trade.realized_pnl_usd = realized_pnl_usd(
            action=trade.action,
            size_usd=trade.size_usd,
            entry_price=trade.entry_price,
            exit_price=result.fill_price,
            paper_mode=broker.paper_mode,
            paper_cost_bps=bps,
        )
    db.add(trade)
    return {
        "trade_id": trade.id,
        "symbol": trade.symbol,
        "success": True,
    }


@router.post(
    "/trades/{trade_id}/close",
    dependencies=[Depends(require_api_key)],
)
async def close_trade(trade_id: int, db: AsyncSession = Depends(get_db)):
    trade = await db.get(Trade, trade_id)
    if trade is None:
        raise HTTPException(404, f"trade {trade_id} not found")
    if trade.status != TradeStatus.OPEN:
        raise HTTPException(400, f"trade {trade_id} is not open (status={trade.status})")

    # Serialize with TradingLoop + monitor + position-review on this market.
    # Commit stays inside the lock so other sessions don't see half state.
    async with get_lock(trade.market):
        result = await _close_trade_row(trade, db)
        db.add(
            AuditLog(
                event_type="manual_close",
                message=f"user closed trade {trade_id} ({trade.symbol})",
                payload=result,
            )
        )
        await db.commit()

    bus = get_bus()
    if result["success"]:
        bus.publish(
            "trade.closed_manual",
            f"user closed {trade.symbol} (trade #{trade_id})",
            severity=EventSeverity.SUCCESS,
        )
    else:
        bus.publish(
            "trade.close_failed",
            f"manual close failed for {trade.symbol}: {result.get('error')}",
            severity=EventSeverity.ERROR,
        )
        raise HTTPException(502, result.get("error") or "broker close failed")
    return result


@router.post(
    "/positions/close-all",
    dependencies=[Depends(require_api_key)],
)
async def close_all_positions(db: AsyncSession = Depends(get_db)):
    open_trades = (
        await db.execute(
            select(Trade)
            .where(Trade.status == TradeStatus.OPEN)
            .order_by(Trade.id)
        )
    ).scalars().all()

    # Group by market so each market's lock is held only for its own
    # trades — avoids one broker's slow close blocking the other.
    by_market: dict[str, list[Trade]] = {}
    for t in open_trades:
        by_market.setdefault(t.market, []).append(t)

    results: list[dict] = []
    for market, trades in by_market.items():
        # Commit inside the lock so a loop tick for this market can't
        # read half-closed state between the close and the commit.
        async with get_lock(market):
            for t in trades:
                res = await _close_trade_row(t, db)
                results.append(res)
            await db.commit()

    db.add(
        AuditLog(
            event_type="close_all",
            message=f"user closed {len(results)} positions",
            payload={"results": results},
        )
    )
    await db.commit()

    closed = sum(1 for r in results if r["success"])
    get_bus().publish(
        "trade.close_all",
        f"close_all: {closed}/{len(results)} succeeded",
        severity=EventSeverity.WARN if closed < len(results) else EventSeverity.SUCCESS,
    )
    return {"attempted": len(results), "closed": closed, "results": results}


@router.post(
    "/orders/cancel-all",
    dependencies=[Depends(require_api_key)],
)
async def cancel_all_orders(db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    broker = build_broker(Market.STOCKS, settings)
    cancelled = await broker.cancel_all_orders()

    # Reconcile local DB: Trade rows that are PENDING, or that were
    # optimistically marked OPEN but never got an entry_price (off-hours
    # accept → never filled), are terminated as CANCELED. Without this
    # they linger forever in the UI as phantom "open" trades.
    stale = (
        await db.execute(
            select(Trade).where(
                Trade.market == Market.STOCKS.value,
                Trade.status.in_(
                    [TradeStatus.PENDING.value, TradeStatus.OPEN.value]
                ),
                Trade.entry_price.is_(None),
            )
        )
    ).scalars().all()
    for t in stale:
        t.status = TradeStatus.CANCELED
        t.closed_at = utc_now()
        db.add(t)
    await db.commit()

    get_bus().publish(
        "orders.cancel_all",
        f"cancel_all_orders: {cancelled} broker / {len(stale)} local rows cleaned",
        severity=EventSeverity.WARN,
        data={"broker_cancelled": cancelled, "local_reconciled": len(stale)},
    )
    return {"cancelled": cancelled, "local_reconciled": len(stale)}


# ---------- AI provider / GPU status ----------


@router.get(
    "/ai/status",
    response_model=AIStatusOut,
    dependencies=[Depends(require_api_key)],
)
async def ai_status():
    from app.ai.status import collect_ai_status

    return await collect_ai_status(get_settings())


# ---------- Market intel (what the AI sees) ----------


@router.get(
    "/intel",
    response_model=MarketIntelOut,
    dependencies=[Depends(require_api_key)],
)
async def market_intel(db: AsyncSession = Depends(get_db)):
    from app.api.intel import collect_market_intel

    return await collect_market_intel(get_settings(), db)


# ---------- Analytics (graphs) ----------


@router.get(
    "/analytics",
    response_model=AnalyticsOut,
    dependencies=[Depends(require_api_key)],
)
async def analytics(db: AsyncSession = Depends(get_db)):
    from app.api.analytics import collect_analytics

    return await collect_analytics(db)


@router.get(
    "/analytics/benchmark",
    response_model=BenchmarkOut,
    dependencies=[Depends(require_api_key)],
)
async def analytics_benchmark(
    symbols: str = Query("SPY", description="comma-separated tickers"),
    start: str = Query(..., description="YYYY-MM-DD"),
    end: str = Query(..., description="YYYY-MM-DD"),
):
    """Daily closes for one or more tickers over a window.

    Used by the analytics page to overlay benchmark lines (SPY by default)
    on the portfolio equity curve. Bars are returned in ascending date
    order; the frontend normalizes to a % change from the first bar.
    """
    try:
        start_d = _date.fromisoformat(start)
        end_d = _date.fromisoformat(end)
    except ValueError as exc:
        raise HTTPException(400, f"invalid date: {exc}") from exc
    if end_d < start_d:
        raise HTTPException(400, "end must be >= start")

    settings = get_settings()
    if "replace_me" in settings.alpaca_api_key:
        raise HTTPException(503, "alpaca credentials not configured")

    tickers = [
        s.strip().upper() for s in symbols.split(",") if s.strip()
    ][:8]
    if not tickers:
        raise HTTPException(400, "at least one symbol required")

    headers = {
        "APCA-API-KEY-ID": settings.alpaca_api_key,
        "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
    }
    out: list[BenchmarkSeries] = []
    async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:

        async def fetch(sym: str) -> BenchmarkSeries:
            url = f"{settings.alpaca_data_url}/v2/stocks/{sym}/bars"
            params = {
                "timeframe": "1Day",
                "start": start_d.isoformat(),
                "end": end_d.isoformat(),
                "limit": 10000,
                "adjustment": "split",
                "feed": "iex",
            }
            try:
                r = await client.get(url, params=params)
                r.raise_for_status()
                data = r.json() or {}
            except Exception as exc:
                return BenchmarkSeries(
                    symbol=sym, bars=[], error=f"{type(exc).__name__}: {exc}"
                )
            bars: list[BenchmarkBar] = []
            for b in data.get("bars") or []:
                t = b.get("t") or ""
                try:
                    d = _date.fromisoformat(t[:10])
                except Exception:
                    continue
                close = b.get("c")
                if close is None:
                    continue
                bars.append(BenchmarkBar(day=d, close=float(close)))
            return BenchmarkSeries(symbol=sym, bars=bars)

        results = await asyncio.gather(*(fetch(t) for t in tickers))
        out = list(results)

    return BenchmarkOut(start=start_d, end=end_d, series=out)


# ---------- Options chain ----------


@router.get(
    "/options/{symbol}",
    response_model=OptionChainOut,
    dependencies=[Depends(require_api_key)],
)
async def options_chain(symbol: str):
    from app.market_data import OptionsClient

    settings = get_settings()
    if "replace_me" in settings.alpaca_api_key:
        raise HTTPException(503, "alpaca credentials not configured")

    client = OptionsClient(
        settings.alpaca_api_key,
        settings.alpaca_api_secret,
        base_url=settings.alpaca_base_url,
        data_url=settings.alpaca_data_url,
    )
    chain = await client.chain(symbol.upper())
    if chain is None:
        raise HTTPException(502, f"failed to fetch options chain for {symbol}")

    return OptionChainOut(
        underlying=chain.underlying,
        expiries=chain.expiries(),
        contracts=[
            OptionContractOut(
                symbol=c.symbol,
                side=c.side.value,
                strike=c.strike,
                expiry=c.expiry,
                bid=c.bid,
                ask=c.ask,
                mid=c.mid,
                last=c.last,
                implied_volatility=c.implied_volatility,
                delta=c.delta,
                gamma=c.gamma,
                theta=c.theta,
                vega=c.vega,
                open_interest=c.open_interest,
                volume=c.volume,
            )
            for c in chain.contracts
        ],
        fetched_at=chain.fetched_at,
    )


# ---------- LLM rate card + usage ----------


@router.get(
    "/llm/rate-card",
    response_model=LlmRateCardOut,
    dependencies=[Depends(require_api_key)],
)
async def get_rate_card(db: AsyncSession = Depends(get_db)):
    row = (
        await db.execute(
            select(LlmRateCardRow)
            .where(LlmRateCardRow.is_active.is_(True))
            .order_by(desc(LlmRateCardRow.id))
            .limit(1)
        )
    ).scalars().first()
    if row is None:
        raise HTTPException(404, "no active rate card — seed one first")
    return LlmRateCardOut(
        id=row.id,
        rates={k: LlmRateEntry(**v) for k, v in (row.rates or {}).items()},
        updated_at=row.created_at,
    )


@router.put(
    "/llm/rate-card",
    response_model=LlmRateCardOut,
    dependencies=[Depends(require_api_key)],
)
async def update_rate_card(
    payload: LlmRateCardIn, db: AsyncSession = Depends(get_db)
):
    await db.execute(update(LlmRateCardRow).values(is_active=False))
    row = LlmRateCardRow(
        is_active=True,
        rates={k: v.model_dump() for k, v in payload.rates.items()},
        changed_by="api",
    )
    db.add(row)
    db.add(
        AuditLog(
            event_type="llm_rate_card_changed",
            message="rate card updated via API",
            payload={k: v.model_dump() for k, v in payload.rates.items()},
        )
    )
    await db.commit()
    await db.refresh(row)
    return LlmRateCardOut(
        id=row.id,
        rates={k: LlmRateEntry(**v) for k, v in (row.rates or {}).items()},
        updated_at=row.created_at,
    )


@router.get(
    "/llm/usage",
    response_model=LlmUsageSummaryOut,
    dependencies=[Depends(require_api_key)],
)
async def llm_usage_summary(
    hours: int = Query(24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
):
    from datetime import timedelta

    cutoff = utc_now() - timedelta(hours=hours)
    rows = (
        await db.execute(
            select(LlmUsageRow)
            .where(LlmUsageRow.created_at >= cutoff)
            .order_by(desc(LlmUsageRow.id))
        )
    ).scalars().all()

    by_key: dict[str, dict] = {}
    for r in rows:
        key = f"{r.provider}::{r.model}"
        bucket = by_key.setdefault(
            key,
            {
                "provider": r.provider,
                "model": r.model,
                "calls": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
            },
        )
        bucket["calls"] += 1
        bucket["total_tokens"] += r.total_tokens
        bucket["cost_usd"] += r.cost_usd

    recent = [LlmUsageRowOut.model_validate(r, from_attributes=True) for r in rows[:50]]
    return LlmUsageSummaryOut(
        window_hours=hours,
        total_calls=len(rows),
        total_tokens=sum(r.total_tokens for r in rows),
        total_cost_usd=sum(r.cost_usd for r in rows),
        by_model=[LlmUsageBucket(key=k, **v) for k, v in by_key.items()],
        recent=recent,
        generated_at=utc_now(),
    )


@router.get(
    "/agents",
    response_model=AgentsOverviewOut,
    dependencies=[Depends(require_api_key)],
)
async def agents_overview(
    hours: int = Query(24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
):
    """Aggregate LLM calls by agent_id over the last N hours.

    One row per agent; `recent_*` fields let the UI anchor on the most
    recent activity without a second query."""
    from datetime import timedelta

    cutoff = utc_now() - timedelta(hours=hours)
    rows = (
        await db.execute(
            select(LlmUsageRow)
            .where(LlmUsageRow.created_at >= cutoff)
            .order_by(desc(LlmUsageRow.id))
        )
    ).scalars().all()

    buckets: dict[str, dict] = {}
    for r in rows:
        key = r.agent_id or "(unlabelled)"
        b = buckets.get(key)
        if b is None:
            b = {
                "agent_id": key,
                "role": r.purpose,
                "calls": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "first_call_at": r.created_at,
                "last_call_at": r.created_at,
                "last_model": r.model,
                "last_purpose": r.purpose,
            }
            buckets[key] = b
        b["calls"] += 1
        b["total_tokens"] += r.total_tokens
        b["cost_usd"] += r.cost_usd
        if r.created_at > b["last_call_at"]:
            b["last_call_at"] = r.created_at
            b["last_model"] = r.model
            b["last_purpose"] = r.purpose
        if r.created_at < b["first_call_at"]:
            b["first_call_at"] = r.created_at

    agents = sorted(
        (AgentSummaryOut(**b) for b in buckets.values()),
        key=lambda a: a.last_call_at or utc_now(),
        reverse=True,
    )
    return AgentsOverviewOut(
        window_hours=hours,
        total_agents=len(agents),
        total_calls=len(rows),
        total_cost_usd=sum(r.cost_usd for r in rows),
        agents=agents,
        generated_at=utc_now(),
    )


@router.get(
    "/agents/roster",
    response_model=AgentRosterOut,
    dependencies=[Depends(require_api_key)],
)
async def agents_roster(db: AsyncSession = Depends(get_db)):
    """Static-ish list of agent TYPES wired into the system.

    Answers "what kinds of agents do I have running?" — not the dynamic
    usage overview the /agents endpoint gives. Pulls enabled-flags from
    settings and last-run timestamps from llm_usage_rows. The UI renders
    this as a tidy roster on the /agents page."""
    from datetime import timedelta

    settings = get_settings()
    cutoff_24h = utc_now() - timedelta(hours=24)

    roles_cfg: list[dict[str, object]] = [
        {
            "role": "scout",
            "label": "scout",
            "description": (
                "Fast-cadence discovery. Pulls movers + screener picks "
                "and optionally runs an LLM filter to keep the "
                "candidate queue focused."
            ),
            "enabled": bool(settings.scout_enabled),
            "cadence": f"every {settings.scout_interval_min}m",
            "purpose_match": ["scout"],
        },
        {
            "role": "research",
            "label": "research (per-symbol)",
            "description": (
                "Per-symbol specialist. Uses web_search + fetch_url to "
                "nail the catalyst, then emits propose_structure or "
                "report_finding. Fanout controlled by multi_agent."
            ),
            "enabled": bool(
                settings.research_enabled and settings.multi_agent_enabled
            ),
            "cadence": "on decision tick",
            "purpose_match": ["research_agent"],
        },
        {
            "role": "decision",
            "label": "decision",
            "description": (
                "Final trading brain. Consumes findings + queue + "
                "positions, emits propose_trade. All trades pass the "
                "risk engine before execution."
            ),
            "enabled": True,
            "cadence": f"every {settings.stock_decision_interval_min}m",
            "purpose_match": ["stock_decision"],
        },
        {
            "role": "position-review",
            "label": "position-review",
            "description": (
                "Fast-cadence exit scanner. Reviews every open position "
                "with fresh news; emits hold / close / tighten_stop in "
                "parallel tool calls."
            ),
            "enabled": bool(settings.position_review_enabled),
            "cadence": f"every {settings.position_review_interval_sec}s",
            "purpose_match": ["position_review"],
        },
        {
            "role": "post-mortem",
            "label": "post-mortem",
            "description": (
                "Writes a terse verdict + lesson for every closed trade. "
                "Recent lessons are surfaced back into the decision prompt."
            ),
            "enabled": bool(settings.post_mortem_enabled),
            "cadence": "after each close",
            "purpose_match": ["post_mortem"],
        },
        {
            "role": "macro",
            "label": "macro / regime",
            "description": (
                "Session-opening tape read. Labels today risk_on / "
                "risk_off / ranging / volatile + one-sentence color — "
                "cached for the session so decisions are consistent."
            ),
            "enabled": bool(settings.macro_regime_enabled),
            "cadence": "once per session",
            "purpose_match": ["macro_regime"],
        },
    ]

    all_purposes = {p for cfg in roles_cfg for p in cfg["purpose_match"]}
    rows = (
        await db.execute(
            select(LlmUsageRow).where(
                LlmUsageRow.created_at >= cutoff_24h,
                LlmUsageRow.purpose.in_(all_purposes),
            )
        )
    ).scalars().all()

    by_purpose_last: dict[str, datetime] = {}
    by_purpose_count: dict[str, int] = {}
    for r in rows:
        if r.purpose is None:
            continue
        prev = by_purpose_last.get(r.purpose)
        if prev is None or r.created_at > prev:
            by_purpose_last[r.purpose] = r.created_at
        by_purpose_count[r.purpose] = by_purpose_count.get(r.purpose, 0) + 1

    out: list[AgentRoleOut] = []
    for cfg in roles_cfg:
        purposes = cfg["purpose_match"]
        last = None
        count = 0
        for p in purposes:
            ts = by_purpose_last.get(p)
            if ts is not None and (last is None or ts > last):
                last = ts
            count += by_purpose_count.get(p, 0)
        out.append(
            AgentRoleOut(
                role=str(cfg["role"]),
                label=str(cfg["label"]),
                description=str(cfg["description"]),
                enabled=bool(cfg["enabled"]),
                cadence=str(cfg["cadence"]),
                last_run_at=last,
                calls_24h=count,
            )
        )

    return AgentRosterOut(generated_at=utc_now(), roles=out)


_TERMINAL_TOOLS = {
    "report_finding",
    "propose_structure",
    "propose_trade",
    "emit_candidates",
}


def _summarize_tool_call(name: str, args: dict) -> str:
    """One-line summary of what this tool call did. Kept short — this
    renders in a table row and a pill title. Falls back to just the tool
    name when arguments are missing."""
    if not isinstance(args, dict):
        return ""
    if name == "web_search":
        q = args.get("query") or args.get("q") or ""
        return str(q)[:80]
    if name == "fetch_url":
        url = args.get("url") or ""
        # Trim scheme + path, keep host + first path segment.
        s = str(url).replace("https://", "").replace("http://", "")
        return s[:80]
    if name == "report_finding":
        bias = args.get("bias") or "?"
        conf = args.get("confidence")
        conf_str = f" {float(conf):.2f}" if isinstance(conf, (int, float)) else ""
        sym = args.get("symbol") or ""
        return f"{sym} {bias}{conf_str}".strip()
    if name == "propose_structure":
        s = args.get("structure") or ""
        u = args.get("underlying") or ""
        return f"{s} {u}".strip()
    if name == "propose_trade":
        action = args.get("action") or ""
        sym = args.get("symbol") or ""
        size = args.get("size_usd")
        size_str = f" ${float(size):.0f}" if isinstance(size, (int, float)) else ""
        return f"{action} {sym}{size_str}".strip()
    if name == "emit_candidates":
        picks = args.get("picks") or args.get("candidates") or []
        if isinstance(picks, list):
            syms = []
            for p in picks[:5]:
                if not p:
                    continue
                if isinstance(p, dict):
                    syms.append(str(p.get("symbol") or p))
                else:
                    syms.append(str(p))
            more = "" if len(picks) <= 5 else f" +{len(picks) - 5}"
            return ", ".join(syms) + more
        return ""
    return ""


def _extract_task_trail(rows: list[LlmUsageRow]) -> list[AgentTaskStep]:
    """Walk an agent's calls in chronological order and flatten every
    tool call into a single trail. Truncated response bodies are skipped
    gracefully."""
    import json as _json

    ordered = sorted(rows, key=lambda r: r.created_at)
    trail: list[AgentTaskStep] = []
    for r in ordered:
        body = r.response_body
        if not isinstance(body, dict) or body.get("_truncated"):
            continue
        choices = body.get("choices") or []
        if not choices:
            continue
        msg = (choices[0] or {}).get("message") or {}
        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            fn = (tc or {}).get("function") or {}
            name = fn.get("name") or ""
            args_raw = fn.get("arguments") or "{}"
            if isinstance(args_raw, str):
                try:
                    args = _json.loads(args_raw)
                except Exception:
                    args = {}
            elif isinstance(args_raw, dict):
                args = args_raw
            else:
                args = {}
            trail.append(
                AgentTaskStep(
                    tool=name or "unknown",
                    summary=_summarize_tool_call(name, args),
                    terminal=name in _TERMINAL_TOOLS,
                    ts=r.created_at,
                )
            )
    return trail


def _task_summary(agent_id: str, trail: list[AgentTaskStep]) -> str | None:
    """Build the one-line status shown on the cycle card.

    Prefer the terminal tool (what they committed to). Otherwise show the
    last tool + a "still researching" marker.
    """
    if not trail:
        return "waiting" if agent_id != "scout" else None
    terminal = next((t for t in reversed(trail) if t.terminal), None)
    if terminal is not None:
        suffix = f": {terminal.summary}" if terminal.summary else ""
        return f"{terminal.tool}{suffix}"
    # No commit yet — show tool progress + "in-flight"
    last = trail[-1]
    counts: dict[str, int] = {}
    for t in trail:
        counts[t.tool] = counts.get(t.tool, 0) + 1
    parts = [f"{v}× {k}" for k, v in counts.items()]
    tail = f" → {last.summary}" if last.summary else ""
    return "in-flight: " + ", ".join(parts) + tail


@router.get(
    "/cycles",
    response_model=CyclesOverviewOut,
    dependencies=[Depends(require_api_key)],
)
async def cycles_overview(
    hours: int = Query(24, ge=1, le=24 * 30),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    """Group LLM calls by cycle_id → one entry per TradingLoop tick.

    Each cycle surfaces the full swarm hierarchy: scout → research-{sym} →
    decision, with per-agent timings/tokens/cost and the decision outcome.
    Rows older than the column's introduction have ``cycle_id = NULL`` and
    are bucketed under a single "legacy" cycle per UTC hour so they don't
    disappear from the UI."""
    from collections import defaultdict
    from datetime import timedelta

    cutoff = utc_now() - timedelta(hours=hours)
    rows = (
        await db.execute(
            select(LlmUsageRow)
            .where(LlmUsageRow.created_at >= cutoff)
            .order_by(desc(LlmUsageRow.id))
        )
    ).scalars().all()

    decisions = (
        await db.execute(
            select(Decision).where(Decision.cycle_id.is_not(None))
        )
    ).scalars().all()
    decision_by_cycle: dict[str, Decision] = {
        d.cycle_id: d for d in decisions if d.cycle_id is not None
    }

    buckets: dict[str, list[LlmUsageRow]] = defaultdict(list)
    for r in rows:
        if r.cycle_id:
            key = r.cycle_id
        else:
            # Bucket unlabelled rows by UTC hour so the UI isn't flooded.
            hour = r.created_at.replace(minute=0, second=0, microsecond=0)
            key = f"legacy-{hour.isoformat()}"
        buckets[key].append(r)

    cycles: list[CycleOut] = []
    for cycle_id, cycle_rows in buckets.items():
        if not cycle_rows:
            continue
        started = min(r.created_at for r in cycle_rows)
        ended = max(r.created_at for r in cycle_rows)

        # Fold rows into per-agent summaries within this cycle.
        agent_buckets: dict[str, dict] = {}
        agent_rows: dict[str, list[LlmUsageRow]] = {}
        for r in cycle_rows:
            a_key = r.agent_id or "(unlabelled)"
            ab = agent_buckets.get(a_key)
            if ab is None:
                focus: str | None = None
                if r.agent_id and r.agent_id.startswith("research-"):
                    focus = r.agent_id[len("research-") :].upper() or None
                ab = {
                    "agent_id": a_key,
                    "role": r.purpose,
                    "calls": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "first_call_at": r.created_at,
                    "last_call_at": r.created_at,
                    "focus": focus,
                }
                agent_buckets[a_key] = ab
                agent_rows[a_key] = []
            agent_rows[a_key].append(r)
            ab["calls"] += 1
            ab["total_tokens"] += r.total_tokens
            ab["cost_usd"] += r.cost_usd
            if r.created_at < ab["first_call_at"]:
                ab["first_call_at"] = r.created_at
            if r.created_at > ab["last_call_at"]:
                ab["last_call_at"] = r.created_at

        # Roles are ordered as they appear in the swarm: scout →
        # research-* → decision. Sort so the UI renders hierarchy naturally.
        def _role_order(a: dict) -> tuple[int, str]:
            aid = a["agent_id"]
            if aid == "scout":
                return (0, aid)
            if aid.startswith("research-"):
                return (1, aid)
            if aid == "decision":
                return (2, aid)
            return (3, aid)

        agents = []
        for b in sorted(agent_buckets.values(), key=_role_order):
            trail = _extract_task_trail(agent_rows[b["agent_id"]])
            agents.append(
                CycleAgentOut(
                    agent_id=b["agent_id"],
                    role=b["role"],
                    calls=b["calls"],
                    total_tokens=b["total_tokens"],
                    cost_usd=b["cost_usd"],
                    first_call_at=b["first_call_at"],
                    last_call_at=b["last_call_at"],
                    focus=b["focus"],
                    task_summary=_task_summary(b["agent_id"], trail),
                    task_trail=trail,
                )
            )

        if cycle_id.startswith("legacy-"):
            kind = "legacy"
        elif cycle_id.startswith("scout-"):
            kind = "scout"
        else:
            kind = "decision"

        d = decision_by_cycle.get(cycle_id)
        outcome: str | None = None
        symbol: str | None = None
        rationale: str | None = None
        market: str | None = None
        decision_id: int | None = None
        if d is not None:
            decision_id = d.id
            market = d.market
            rationale = d.rationale
            if d.executed:
                outcome = "executed"
            elif d.rejection_code == "market_closed":
                outcome = "market_closed"
            elif d.rejection_code == "strategy_no_op":
                outcome = "hold"
            elif d.approved:
                outcome = "approved_not_executed"
            else:
                outcome = "rejected"
            if d.proposal_json:
                sym = d.proposal_json.get("symbol") if isinstance(d.proposal_json, dict) else None
                if isinstance(sym, str):
                    symbol = sym

        cycles.append(
            CycleOut(
                cycle_id=cycle_id,
                kind=kind,
                started_at=started,
                ended_at=ended,
                elapsed_sec=max(0.0, (ended - started).total_seconds()),
                total_calls=len(cycle_rows),
                total_tokens=sum(r.total_tokens for r in cycle_rows),
                total_cost_usd=sum(r.cost_usd for r in cycle_rows),
                decision_id=decision_id,
                decision_market=market,
                decision_outcome=outcome,
                decision_symbol=symbol,
                decision_rationale=rationale,
                agents=agents,
            )
        )

    cycles.sort(key=lambda c: c.started_at, reverse=True)
    cycles = cycles[:limit]

    return CyclesOverviewOut(
        window_hours=hours,
        total_cycles=len(cycles),
        total_calls=sum(c.total_calls for c in cycles),
        total_cost_usd=sum(c.total_cost_usd for c in cycles),
        cycles=cycles,
        generated_at=utc_now(),
    )


@router.get(
    "/llm/calls",
    response_model=list[LlmUsageRowOut],
    dependencies=[Depends(require_api_key)],
)
async def list_llm_calls(
    agent_id: str | None = None,
    purpose: str | None = None,
    decision_id: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    """List LLM calls with filters. Rows carry metadata only — fetch
    /api/llm/calls/{id} for the full prompt/response blob."""
    stmt = select(LlmUsageRow).order_by(desc(LlmUsageRow.id))
    if agent_id:
        stmt = stmt.where(LlmUsageRow.agent_id == agent_id)
    if purpose:
        stmt = stmt.where(LlmUsageRow.purpose == purpose)
    if decision_id is not None:
        stmt = stmt.where(LlmUsageRow.decision_id == decision_id)
    rows = (await db.execute(stmt.limit(limit))).scalars().all()
    return [LlmUsageRowOut.model_validate(r, from_attributes=True) for r in rows]


@router.get(
    "/llm/calls/{call_id}",
    response_model=LlmCallDetailOut,
    dependencies=[Depends(require_api_key)],
)
async def get_llm_call(call_id: int, db: AsyncSession = Depends(get_db)):
    row = await db.get(LlmUsageRow, call_id)
    if row is None:
        raise HTTPException(404, f"llm call {call_id} not found")
    return LlmCallDetailOut.model_validate(row, from_attributes=True)


# ---------- Scout queue ----------


@router.get(
    "/scout/queue",
    response_model=ScoutQueueOut,
    dependencies=[Depends(require_api_key)],
)
async def scout_queue():
    from datetime import UTC, datetime

    settings = get_settings()
    queue = get_candidate_queue()

    spent = 0.0
    if settings.daily_llm_budget_usd > 0:
        from app.db import get_session_factory

        try:
            spent = await today_cost_usd(get_session_factory())
        except Exception:
            spent = 0.0

    if queue is None:
        return ScoutQueueOut(
            enabled=False,
            queue_size=0,
            ttl_sec=settings.scout_queue_ttl_sec,
            daily_llm_budget_usd=settings.daily_llm_budget_usd,
            daily_llm_spent_usd=spent,
            candidates=[],
        )

    items = await queue.peek()
    return ScoutQueueOut(
        enabled=True,
        queue_size=len(items),
        ttl_sec=settings.scout_queue_ttl_sec,
        daily_llm_budget_usd=settings.daily_llm_budget_usd,
        daily_llm_spent_usd=spent,
        candidates=[
            ScoutCandidateOut(
                symbol=c.symbol,
                source=c.source,
                note=c.note,
                score=c.score,
                added_at=datetime.fromtimestamp(c.added_at, tz=UTC),
                age_sec=c.age_sec(),
            )
            for c in items
        ],
    )


# ---------- Live activity ----------


@router.get(
    "/activity",
    response_model=list[ActivityEventOut],
    dependencies=[Depends(require_api_key)],
)
async def list_activity(
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(ActivityEventRow)
            .order_by(desc(ActivityEventRow.id))
            .limit(limit)
        )
    ).scalars().all()
    return [ActivityEventOut.model_validate(r, from_attributes=True) for r in rows]


@router.get("/events")
async def events_stream(request: Request, api_key: str | None = Query(default=None)):
    """Server-Sent Events stream of live activity.

    EventSource cannot set custom headers, so auth comes via ?api_key= on the
    querystring rather than the X-API-Key header used by the rest of the API.
    """
    expected = get_settings().jwt_secret
    if not api_key or api_key != expected:
        raise HTTPException(status_code=401, detail="invalid api key")

    bus = get_bus()
    queue = bus.subscribe()

    async def generator():
        try:
            hello = ActivityEvent(
                id=0,
                ts=utc_now().isoformat(),
                type="stream.connected",
                severity=EventSeverity.INFO,
                message="activity stream online",
            )
            yield hello.to_sse()

            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield event.to_sse()
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
