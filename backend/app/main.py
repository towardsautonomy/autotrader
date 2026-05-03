from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.activity import ActivityEvent, EventSeverity, get_bus
from app.ai import build_provider_from_settings
from app.ai.orchestrator import Orchestrator
from app.ai.macro_agent import MacroAgent
from app.ai.position_review_agent import PositionReviewAgent
from app.ai.post_mortem_agent import PostMortemAgent
from app.ai.research import UrlFetchClient, WebSearchClient
from app.ai.research_loop import ResearchAgent
from app.ai.research_toolbelt import ResearchToolbelt
from app.ai.scout_agent import ScoutAgent
from app.api.research_chat import router as research_router
from app.api.routes import router
from app.brokers import build_broker
from app.config import get_settings
from app.db import get_session_factory, init_db
from app.market_data import (
    FinnhubClient,
    MoversClient,
    OptionsClient,
    Screener,
    UniverseClient,
)
from app.models import (
    DEFAULT_RATES,
    ActivityEventRow,
    AuditLog,
    LlmRateCardRow,
    RiskConfigRow,
    SystemState,
)
from app.risk import Market, RiskConfig, RiskEngine
from app.runtime import set_candidate_queue
from app.scheduler import (
    CandidateQueue,
    BracketReconciler,
    PendingReconciler,
    PositionReviewLoop,
    PostMortemLoop,
    RuntimeMonitor,
    SafetyMonitor,
    SchedulerRunner,
    ScoutLoop,
    TradingLoop,
)
from app.strategies import ClaudeStockStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Fail-fast only in LIVE mode. Paper mode intentionally boots with
    # placeholder creds so the UI/dev server works without real keys.
    if not settings.paper_mode:
        settings.assert_secrets_configured(require_polymarket=False)
    await init_db()
    await _seed_defaults()
    await _clear_stale_circuit_breaker_pause()
    _install_activity_persistence()
    logger.info("autotrader starting in %s mode", settings.mode_label)

    bus = get_bus()
    bus.publish(
        "system.boot",
        f"autotrader online in {settings.mode_label} mode",
        severity=EventSeverity.SUCCESS,
        data={
            "mode": settings.mode_label,
            "ai_provider": settings.ai_provider,
        },
    )

    runner: SchedulerRunner | None = None
    try:
        runner = await _build_scheduler(settings)
        if runner is not None:
            runner.start()
        else:
            bus.publish(
                "scheduler.skipped",
                "scheduler disabled — fill in broker/AI credentials to enable trading",
                severity=EventSeverity.WARN,
            )

        yield
    finally:
        if runner is not None:
            runner.stop()


async def _seed_defaults() -> None:
    settings = get_settings()
    factory = get_session_factory()
    async with factory() as s:
        if await s.get(SystemState, 1) is None:
            s.add(
                SystemState(
                    id=1,
                    trading_enabled=True,
                    paper_mode=settings.paper_mode,
                )
            )
        existing = (
            await s.execute(
                select(RiskConfigRow).where(RiskConfigRow.is_active.is_(True)).limit(1)
            )
        ).scalars().first()
        if existing is None:
            row = RiskConfigRow.from_dataclass(RiskConfig(), changed_by="seed")
            row.is_active = True
            s.add(row)
        active_card = (
            await s.execute(
                select(LlmRateCardRow)
                .where(LlmRateCardRow.is_active.is_(True))
                .limit(1)
            )
        ).scalars().first()
        if active_card is None:
            s.add(
                LlmRateCardRow(
                    is_active=True,
                    rates=dict(DEFAULT_RATES),
                    changed_by="seed",
                )
            )
        await s.commit()


async def _clear_stale_circuit_breaker_pause() -> None:
    """If agents_paused is True but the circuit breaker is now disabled and
    the most recent pause/resume event came from ``safety.circuit_breaker``,
    clear the pause automatically.

    Without this, disabling the circuit breaker only takes effect after the
    user clicks resume once — any stale trip from a previous boot keeps the
    agents frozen even though no new auto-pause path can fire.
    """
    settings = get_settings()
    if settings.circuit_breaker_consecutive_losses > 0:
        return
    factory = get_session_factory()
    async with factory() as s:
        state = await s.get(SystemState, 1)
        if state is None or not state.agents_paused:
            return
        latest = (
            await s.execute(
                select(ActivityEventRow)
                .where(
                    ActivityEventRow.type.in_(
                        (
                            "safety.circuit_breaker",
                            "agents.paused",
                            "agents.resumed",
                        )
                    )
                )
                .order_by(ActivityEventRow.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if latest is None or latest.type != "safety.circuit_breaker":
            return
        state.agents_paused = False
        s.add(state)
        s.add(
            AuditLog(
                event_type="agents_resumed",
                message=(
                    "boot: cleared stale circuit-breaker pause "
                    "(circuit_breaker_consecutive_losses=0)"
                ),
            )
        )
        await s.commit()
    logger.info("cleared stale circuit-breaker pause on boot")
    get_bus().publish(
        "agents.resumed",
        "boot: stale circuit-breaker pause cleared",
        severity=EventSeverity.INFO,
        data={"source": "boot_stale_clear"},
    )


async def _active_risk_config() -> RiskConfig:
    factory = get_session_factory()
    async with factory() as s:
        row = (
            await s.execute(
                select(RiskConfigRow)
                .where(RiskConfigRow.is_active.is_(True))
                .order_by(RiskConfigRow.id.desc())
                .limit(1)
            )
        ).scalars().first()
        if row is None:
            return RiskConfig()
        return row.to_dataclass()


async def _build_scheduler(settings) -> SchedulerRunner | None:
    """Compose broker + strategy + risk engine + loops and return a runner.

    Returns None if required credentials aren't configured — the API still
    boots, the UI renders, but no trading loops run until the user fixes
    backend/.env.
    """
    broker = build_broker(Market.STOCKS, settings)
    if broker.__class__.__name__ == "NullBroker":
        logger.warning("stocks broker is NullBroker — scheduler will not start")
        return None

    try:
        provider = build_provider_from_settings(settings)
    except Exception:
        logger.exception("failed to build AI provider — scheduler will not start")
        return None

    news_client: FinnhubClient | None = None
    if settings.finnhub_api_key:
        news_client = FinnhubClient(settings.finnhub_api_key)

    movers_client: MoversClient | None = None
    universe_client: UniverseClient | None = None
    screener: Screener | None = None
    options_client: OptionsClient | None = None
    if "replace_me" not in settings.alpaca_api_key:
        movers_client = MoversClient(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            data_url=settings.alpaca_data_url,
        )
        universe_client = UniverseClient(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            base_url=settings.alpaca_base_url,
        )
        screener = Screener(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            universe_client,
            data_url=settings.alpaca_data_url,
            top_k=settings.screener_top_k,
        )
        options_client = OptionsClient(
            settings.alpaca_api_key,
            settings.alpaca_api_secret,
            base_url=settings.alpaca_base_url,
            data_url=settings.alpaca_data_url,
        )

    risk_config = await _active_risk_config()
    risk_engine = RiskEngine(risk_config)
    factory = get_session_factory()

    shared_search = WebSearchClient(
        tavily_api_key=settings.tavily_api_key,
        brave_api_key=settings.brave_search_api_key,
        serper_api_key=settings.serper_api_key,
    )

    # Single shared research toolbelt — every agent (decision loop,
    # per-symbol specialists, researcher chat) pulls from the same
    # tool set so adding a tool in one place is visible everywhere.
    shared_toolbelt = ResearchToolbelt(
        finnhub=news_client,
        search=shared_search,
        fetch=UrlFetchClient(),
        alpaca_api_key=(
            settings.alpaca_api_key
            if "replace_me" not in settings.alpaca_api_key
            else None
        ),
        alpaca_api_secret=(
            settings.alpaca_api_secret
            if "replace_me" not in settings.alpaca_api_secret
            else None
        ),
        alpaca_data_url=settings.alpaca_data_url,
        session_factory=factory,
    )

    # Tools the trading agents can opt into. The researcher chat always
    # exposes the full belt; the decision / per-symbol agents get the
    # data-heavy tools but not deep_dive (to keep the prompt surface
    # tight for a directional call).
    _decision_tool_names = [
        "deep_dive",
        "get_company_profile",
        "get_company_news",
        "get_quote",
        "get_price_history",
        "get_technicals",
        "get_analyst_ratings",
        "get_earnings",
        "get_insider_transactions",
        "get_ownership",
        "get_basic_financials",
        "get_peers",
        "get_sec_filings",
        "read_filing",
        "get_market_context",
    ]

    research_agent: ResearchAgent | None = None
    orchestrator: Orchestrator | None = None
    if settings.research_enabled:
        research_agent = ResearchAgent(
            provider=provider,
            search_client=shared_search,
            fetch_client=UrlFetchClient(),
            max_tool_calls=settings.research_max_tool_calls,
            max_rounds=settings.research_max_rounds,
            session_factory=factory,
            toolbelt=shared_toolbelt,
            extra_tool_names=_decision_tool_names,
        )
        if settings.multi_agent_enabled:
            orchestrator = Orchestrator(
                provider=provider,
                research_agent=research_agent,
                focus_count=settings.multi_agent_focus_count,
                per_agent_max_tool_calls=settings.multi_agent_per_agent_tool_calls,
                per_agent_max_rounds=settings.multi_agent_per_agent_rounds,
                session_factory=factory,
                toolbelt=shared_toolbelt,
                extra_tool_names=_decision_tool_names,
            )

    macro_agent: MacroAgent | None = None
    if settings.macro_regime_enabled:
        macro_agent = MacroAgent(
            provider=provider,
            session_factory=factory,
        )

    candidate_queue: CandidateQueue | None = None
    if settings.scout_enabled:
        candidate_queue = CandidateQueue(
            ttl_sec=float(settings.scout_queue_ttl_sec),
            max_size=settings.scout_queue_max_size,
        )
    set_candidate_queue(candidate_queue)

    strategy = ClaudeStockStrategy(
        broker=broker,
        provider=provider,
        risk_config=risk_config,
        strategy_note=settings.stock_strategy_note,
        news_client=news_client,
        movers_client=movers_client,
        screener=screener,
        options_client=options_client,
        session_factory=factory,
        research_agent=research_agent,
        orchestrator=orchestrator,
        macro_agent=macro_agent,
        screener_top_k=settings.screener_top_k,
        candidate_queue=candidate_queue,
    )

    loop = TradingLoop(
        broker=broker,
        strategy=strategy,
        risk_engine=risk_engine,
        session_factory=factory,
        daily_llm_budget_usd=settings.daily_llm_budget_usd,
        respect_market_hours=settings.respect_market_hours,
    )
    monitor = RuntimeMonitor(broker=broker, session_factory=factory)
    reconciler = BracketReconciler(broker=broker, session_factory=factory)
    pending_reconciler = PendingReconciler(
        broker=broker, session_factory=factory
    )

    scout_loops: list[ScoutLoop] = []
    if candidate_queue is not None and (
        movers_client is not None or screener is not None
    ):
        scout_llm: ScoutAgent | None = None
        if settings.scout_llm_enabled:
            scout_llm = ScoutAgent(provider=provider, session_factory=factory)
        scout_loops.append(
            ScoutLoop(
                queue=candidate_queue,
                movers_client=movers_client,
                screener=screener,
                per_bucket=settings.scout_per_bucket,
                screener_top_k=settings.screener_top_k,
                session_factory=factory,
                daily_llm_budget_usd=settings.daily_llm_budget_usd,
                market_label=Market.STOCKS.value,
                llm_agent=scout_llm,
                # Scout runs 24/7 — research doesn't need the market open.
                respect_market_hours=False,
            )
        )

    position_review_loops: list[PositionReviewLoop] = []
    if settings.position_review_enabled:
        review_agent = PositionReviewAgent(
            provider=provider,
            session_factory=factory,
        )
        position_review_loops.append(
            PositionReviewLoop(
                broker=broker,
                agent=review_agent,
                session_factory=factory,
                news_client=news_client,
                daily_llm_budget_usd=settings.daily_llm_budget_usd,
                respect_market_hours=settings.respect_market_hours,
                market_label=Market.STOCKS.value,
            )
        )

    post_mortem_loops: list[PostMortemLoop] = []
    if settings.post_mortem_enabled:
        post_mortem_agent = PostMortemAgent(
            provider=provider,
            session_factory=factory,
        )
        post_mortem_loops.append(
            PostMortemLoop(
                agent=post_mortem_agent,
                session_factory=factory,
                daily_llm_budget_usd=settings.daily_llm_budget_usd,
                market_label=Market.STOCKS.value,
            )
        )

    safety_monitors: list[SafetyMonitor] = []
    if (
        settings.circuit_breaker_consecutive_losses > 0
        or settings.option_dte_watchdog_days > 0
    ):
        safety_monitors.append(
            SafetyMonitor(
                broker=broker,
                session_factory=factory,
                consecutive_loss_limit=(
                    settings.circuit_breaker_consecutive_losses
                ),
                option_dte_watchdog_days=settings.option_dte_watchdog_days,
            )
        )

    return SchedulerRunner(
        trading_loops=[loop],
        monitors=[monitor],
        decision_interval_min=settings.stock_decision_interval_min,
        monitor_interval_sec=settings.runtime_monitor_interval_sec,
        scout_loops=scout_loops,
        scout_interval_min=settings.scout_interval_min,
        position_review_loops=position_review_loops,
        position_review_interval_sec=settings.position_review_interval_sec,
        safety_monitors=safety_monitors,
        post_mortem_loops=post_mortem_loops,
        reconcilers=[reconciler],
        pending_reconcilers=[pending_reconciler],
    )


def _install_activity_persistence() -> None:
    """Wire the ActivityBus to persist every event to activity_events.

    Uses a background task queue so publishers never block on I/O.
    """
    factory = get_session_factory()
    loop = asyncio.get_running_loop()

    async def _write(event: ActivityEvent) -> None:
        try:
            async with factory() as s:
                s.add(
                    ActivityEventRow(
                        type=event.type,
                        severity=event.severity.value,
                        message=event.message,
                        data=event.data or None,
                    )
                )
                await s.commit()
        except Exception:
            logger.exception("failed to persist activity event %s", event.type)

    def hook(event: ActivityEvent) -> None:
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_write(event)))

    get_bus().set_persist_hook(hook)


def create_app() -> FastAPI:
    app = FastAPI(title="autotrader", version="0.1.0", lifespan=lifespan)
    settings = get_settings()
    origins = [
        o.strip() for o in settings.cors_origins.split(",") if o.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")
    app.include_router(research_router, prefix="/api")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
