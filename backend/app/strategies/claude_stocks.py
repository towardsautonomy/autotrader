"""AI-driven stock strategy.

Asks the configured LLM (OpenRouter or LM Studio) for one action per
decision cycle using tool-use for schema compliance. Returns None (hold)
when the tool_input action is 'hold'. Enriches the prompt with recent
Finnhub news if a client is provided.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import EventSeverity, get_bus
from app.ai.llm_provider import LLMProvider
from app.ai.macro_agent import MacroAgent
from app.ai.orchestrator import Orchestrator, findings_to_prompt_block
from app.ai.prompts.stocks import SYSTEM_PROMPT, IvSnapshot, build_user_message
from app.ai.research_loop import ResearchAgent, ResearchOutcome
from app.ai.usage import log_usage
from app.brokers import BrokerAdapter
from app.market_data import (
    FinnhubClient,
    MoversClient,
    MoversSnapshot,
    NewsItem,
    OptionChain,
    OptionsClient,
    Screener,
    ScreenerSnapshot,
)
from app.risk import (
    AccountSnapshot,
    Market,
    OptionProposal,
    OptionSide,
    RiskConfig,
    TradeAction,
    TradeProposal,
    compute_portfolio_risk,
    format_portfolio_risk_block,
)
from app.scheduler.candidate_queue import CandidateQueue

from .base import Strategy, StrategyProposal
from .option_structures import (
    BuilderError,
    build_iron_condor,
    build_long_option,
    build_vertical_credit,
    build_vertical_debit,
)

logger = logging.getLogger(__name__)


class ClaudeStockStrategy(Strategy):
    def __init__(
        self,
        *,
        broker: BrokerAdapter,
        provider: LLMProvider,
        risk_config: RiskConfig,
        strategy_note: str = "Momentum day-trading on liquid US equities.",
        news_client: FinnhubClient | None = None,
        movers_client: MoversClient | None = None,
        screener: Screener | None = None,
        options_client: OptionsClient | None = None,
        session_factory: async_sessionmaker | None = None,
        research_agent: ResearchAgent | None = None,
        orchestrator: Orchestrator | None = None,
        macro_agent: MacroAgent | None = None,
        discovery_per_bucket: int = 5,
        screener_top_k: int = 10,
        iv_top_k: int = 6,
        candidate_queue: CandidateQueue | None = None,
    ) -> None:
        self._broker = broker
        self._provider = provider
        self._config = risk_config
        self._note = strategy_note
        self._news_client = news_client
        self._movers_client = movers_client
        self._screener = screener
        self._options_client = options_client
        self._session_factory = session_factory
        self._research_agent = research_agent
        self._orchestrator = orchestrator
        self._macro_agent = macro_agent
        self._discovery_per_bucket = discovery_per_bucket
        self._screener_top_k = screener_top_k
        self._iv_top_k = iv_top_k
        self._candidate_queue = candidate_queue

    @property
    def market(self) -> Market:
        return Market.STOCKS

    async def decide(self, snapshot: AccountSnapshot) -> StrategyProposal:
        bus = get_bus()

        # Discovery layer — merge live movers with the fixed watchlist so the
        # AI sees what's actually running *right now*, not just our preset list.
        movers: MoversSnapshot | None = None
        if self._movers_client and self._movers_client.enabled:
            try:
                movers = await self._movers_client.fetch()
            except Exception:
                logger.warning("movers fetch failed", exc_info=True)
            if movers:
                bus.publish(
                    "discovery.movers",
                    f"pulled {len(movers.gainers)}g/{len(movers.losers)}l/"
                    f"{len(movers.most_active)}a movers",
                    data={
                        "gainers": [m.symbol for m in movers.gainers[:5]],
                        "losers": [m.symbol for m in movers.losers[:5]],
                        "most_active": [m.symbol for m in movers.most_active[:5]],
                    },
                )

        discovery_symbols: list[str] = (
            movers.top_symbols(per_bucket=self._discovery_per_bucket)
            if movers
            else []
        )

        # Shortlist — needle-in-haystack scan across the full tradable
        # universe. Cheap signals (vol ratio, gap, range); pulls names the
        # watchlist would never surface.
        shortlist: ScreenerSnapshot | None = None
        if self._screener and self._screener.enabled:
            try:
                shortlist = await self._screener.shortlist(
                    top_k=self._screener_top_k
                )
            except Exception:
                logger.warning("screener fetch failed", exc_info=True)
            if shortlist:
                bus.publish(
                    "discovery.shortlist",
                    f"screened {shortlist.scored}/{shortlist.universe_size}"
                    f" → top {len(shortlist.candidates)}",
                    data={
                        "top": [c.symbol for c in shortlist.candidates[:10]],
                        "universe": shortlist.universe_size,
                        "scored": shortlist.scored,
                    },
                )
        shortlist_symbols: list[str] = (
            [c.symbol for c in shortlist.candidates] if shortlist else []
        )

        scout_symbols: list[str] = []
        if self._candidate_queue is not None:
            try:
                scouted = await self._candidate_queue.peek()
                scout_symbols = [c.symbol for c in scouted]
                if scout_symbols:
                    bus.publish(
                        "scout.queue_peek",
                        f"decision loop pulled {len(scout_symbols)} scout candidates",
                        data={
                            "symbols": scout_symbols[:10],
                            "queue_size": len(scouted),
                        },
                    )
            except Exception:
                logger.warning("scout queue peek failed", exc_info=True)

        # Candidate set: held positions first (always track them — the AI
        # might want to close), then discoveries (scout, movers, screener).
        # No watchlist, no fixed list. If scout/movers/screener all came up
        # empty, the AI holds this cycle — that's the correct behavior.
        discoveries: list[str] = (
            scout_symbols + discovery_symbols + shortlist_symbols
        )
        position_symbols = [
            p.symbol for p in snapshot.positions if p.market == Market.STOCKS
        ]

        ordered: list[str] = []
        seen: set[str] = set()
        for s in position_symbols + discoveries:
            if s and s not in seen:
                seen.add(s)
                ordered.append(s)

        prices: dict[str, float] = {}
        for sym in ordered:
            try:
                prices[sym] = await self._broker.get_price(sym)
            except Exception:
                logger.warning("price fetch failed for %s", sym, exc_info=True)

        per_symbol_news: dict[str, list[NewsItem]] = {}
        market_news: list[NewsItem] = []
        if self._news_client and self._news_client.enabled:
            symbols_to_fetch = ordered
            fetch_news = [
                self._news_client.company_news(s, limit=3) for s in symbols_to_fetch
            ]
            fetch_news.append(self._news_client.market_news(limit=5))
            results = await asyncio.gather(*fetch_news, return_exceptions=True)
            for sym, res in zip(symbols_to_fetch, results[:-1], strict=False):
                if isinstance(res, list):
                    per_symbol_news[sym] = res
            market_last = results[-1]
            if isinstance(market_last, list):
                market_news = market_last

            total = sum(len(v) for v in per_symbol_news.values()) + len(market_news)
            bus.publish(
                "news.fetched",
                f"fetched {total} headlines from finnhub",
                data={"symbols": list(per_symbol_news.keys()), "total": total},
            )

        iv_by_symbol: dict[str, IvSnapshot] = {}
        if self._options_client and self._options_client.enabled:
            iv_symbols = ordered[: self._iv_top_k]
            if iv_symbols:
                iv_by_symbol = await _collect_iv_snapshots(
                    self._options_client, iv_symbols, prices
                )
                if iv_by_symbol:
                    bus.publish(
                        "iv.fetched",
                        f"iv-regime snapshot for {len(iv_by_symbol)} candidates",
                        data={
                            s: {"iv": iv.atm_iv, "regime": iv.regime}
                            for s, iv in iv_by_symbol.items()
                        },
                    )

        agent_findings_block: str | None = None
        orch_findings: list = []
        if self._orchestrator is not None:
            per_symbol_context = _summarize_context_per_symbol(
                ordered, prices, per_symbol_news, iv_by_symbol, shortlist, movers
            )
            orch = await self._orchestrator.orchestrate(
                symbols=ordered,
                per_symbol_context=per_symbol_context,
            )
            orch_findings = orch.findings
            if orch_findings:
                agent_findings_block = findings_to_prompt_block(orch_findings)

        portfolio_risk = compute_portfolio_risk(snapshot, self._config)
        portfolio_risk_block = format_portfolio_risk_block(portfolio_risk)
        lessons_block = await _recent_lessons_block(self._session_factory)

        macro_block: str | None = None
        if self._macro_agent is not None:
            macro = await self._macro_agent.get(
                market_news=[n.headline for n in market_news if n.headline],
            )
            if macro is not None:
                macro_block = f"{macro.label} — {macro.color}"
        if portfolio_risk.concentration_warning:
            bus.publish(
                "portfolio.risk_warning",
                portfolio_risk.concentration_warning,
                severity=EventSeverity.WARN,
                data={
                    "utilization_pct": portfolio_risk.budget_utilization_pct,
                    "largest_symbol": portfolio_risk.largest_position_symbol,
                    "largest_pct": portfolio_risk.largest_position_pct_of_exposure,
                },
            )

        user_msg = build_user_message(
            snapshot=snapshot,
            config=self._config,
            watchlist_prices=prices,
            strategy_note=self._note,
            per_symbol_news=per_symbol_news,
            market_news=market_news,
            movers=movers,
            shortlist=shortlist,
            iv_by_symbol=iv_by_symbol,
            agent_findings_block=agent_findings_block,
            portfolio_risk_block=portfolio_risk_block,
            lessons_block=lessons_block,
            macro_block=macro_block,
        )

        bus.publish(
            "ai.request",
            f"asking {self._provider.description}",
            data={
                "provider": self._provider.provider,
                "model": self._provider.model,
                "candidates": ordered,
                "prices": prices,
            },
        )

        research_artifacts: list[dict] = []
        # Each orchestrator agent's finding becomes one "agent" entry in the
        # decision's research trail alongside any tool calls the decision
        # agent makes itself.
        for f in orch_findings:
            research_artifacts.append(
                {
                    "tool": "research_agent",
                    "arguments": {"symbol": f.symbol},
                    "result_preview": (
                        f"{f.bias} (conf {f.confidence:.2f}) — {f.summary[:200]}"
                    ),
                    "result_count": len(f.artifacts),
                    "elapsed_sec": f.elapsed_sec,
                    "error": f.error,
                }
            )

        used_research_agent = self._research_agent is not None
        try:
            if used_research_agent:
                outcome = await self._research_agent.propose(
                    system=SYSTEM_PROMPT,
                    user=user_msg,
                    agent_id="decision",
                    purpose="stock_decision",
                )
                resp = outcome.response
                research_artifacts.extend(
                    {
                        "tool": a.tool,
                        "arguments": a.arguments,
                        "result_preview": a.result_preview,
                        "result_count": a.result_count,
                    }
                    for a in outcome.artifacts
                )
            else:
                resp = await self._provider.propose(
                    system=SYSTEM_PROMPT, user=user_msg
                )
        except Exception as exc:
            bus.publish(
                "ai.error",
                f"LLM call failed: {exc}",
                severity=EventSeverity.ERROR,
                data={"provider": self._provider.provider, "model": self._provider.model},
            )
            raise

        # ResearchAgent persists one row per round; don't double-count here.
        # Single-shot provider.propose() has no per-round logger, so log it.
        if not used_research_agent and self._session_factory is not None:
            try:
                await log_usage(
                    self._session_factory,
                    resp,
                    purpose="stock_decision",
                    agent_id="decision",
                )
            except Exception:
                logger.exception("failed to persist llm usage row")
        tool = resp.tool_input
        action = tool.get("action")
        rationale = tool.get("rationale", "")

        bus.publish(
            "ai.response",
            f"model chose {action or '?'}",
            data={
                "action": action,
                "symbol": tool.get("symbol"),
                "size_usd": tool.get("size_usd"),
                "confidence": tool.get("confidence"),
                "rationale": rationale,
            },
        )

        raw_prompt = {"system": SYSTEM_PROMPT, "user": user_msg}

        if action == "hold" or not action:
            return StrategyProposal(
                market=Market.STOCKS,
                trade=None,
                rationale=rationale or "hold",
                raw_prompt=raw_prompt,
                raw_response=resp.raw_response,
                model=resp.model,
                research=research_artifacts,
            )

        trade_action = {
            "open_long": TradeAction.OPEN_LONG,
            "open_short": TradeAction.OPEN_SHORT,
            "close": TradeAction.CLOSE,
        }.get(action)

        if trade_action is None:
            return StrategyProposal(
                market=Market.STOCKS,
                trade=None,
                rationale=f"unknown action from model: {action!r}",
                raw_prompt=raw_prompt,
                raw_response=resp.raw_response,
                model=resp.model,
                research=research_artifacts,
            )

        symbol = tool.get("symbol")
        if not symbol:
            return StrategyProposal(
                market=Market.STOCKS,
                trade=None,
                rationale="model omitted symbol",
                raw_prompt=raw_prompt,
                raw_response=resp.raw_response,
                model=resp.model,
                research=research_artifacts,
            )

        size_usd = float(tool.get("size_usd") or 0.0)
        option_spec = tool.get("option") if isinstance(tool.get("option"), dict) else None
        option_proposal: OptionProposal | None = None
        if option_spec and trade_action in (
            TradeAction.OPEN_LONG,
            TradeAction.OPEN_SHORT,
        ):
            try:
                option_proposal = await self._build_option_proposal(
                    symbol.upper(), option_spec
                )
            except BuilderError as exc:
                bus.publish(
                    "option.build_failed",
                    f"{symbol}: {exc}",
                    severity=EventSeverity.WARN,
                    data={"symbol": symbol, "spec": option_spec, "error": str(exc)},
                )
                return StrategyProposal(
                    market=Market.STOCKS,
                    trade=None,
                    rationale=f"option build failed for {symbol}: {exc}",
                    raw_prompt=raw_prompt,
                    raw_response=resp.raw_response,
                    model=resp.model,
                    research=research_artifacts,
                )
            size_usd = option_proposal.max_loss_usd

        trade = TradeProposal(
            market=Market.STOCKS,
            action=trade_action,
            symbol=symbol.upper(),
            size_usd=size_usd,
            stop_loss_pct=_opt_float(tool.get("stop_loss_pct")),
            take_profit_pct=_opt_float(tool.get("take_profit_pct")),
            rationale=rationale,
            confidence=_opt_float(tool.get("confidence")),
            option=option_proposal,
        )

        return StrategyProposal(
            market=Market.STOCKS,
            trade=trade,
            rationale=rationale,
            raw_prompt=raw_prompt,
            raw_response=resp.raw_response,
            model=resp.model,
            research=research_artifacts,
        )


    async def _build_option_proposal(
        self, symbol: str, spec: dict
    ) -> OptionProposal:
        """Dispatch to the right structure builder using the option spec
        from the LLM's tool call. Raises BuilderError if unsupported or
        the chain doesn't yield viable legs."""
        if self._options_client is None or not self._options_client.enabled:
            raise BuilderError("options client not configured")
        chain = await self._options_client.chain(symbol)
        if chain is None:
            raise BuilderError(f"no option chain available for {symbol}")

        structure = str(spec.get("structure") or "").lower()
        expiry = spec.get("expiry")
        contracts = int(spec.get("contracts") or 1)

        def _sf(key: str) -> float | None:
            v = spec.get(key)
            return float(v) if v is not None else None

        if structure in ("long_call", "long_put"):
            side = OptionSide.CALL if structure == "long_call" else OptionSide.PUT
            return build_long_option(
                chain,
                side=side,
                expiry=expiry,
                strike=_sf("long_strike"),
                contracts=contracts,
            )
        if structure == "vertical_debit":
            direction = str(spec.get("direction") or "").lower()
            return build_vertical_debit(
                chain,
                direction=direction,
                expiry=expiry,
                long_strike=_sf("long_strike"),
                short_strike=_sf("short_strike"),
                contracts=contracts,
            )
        if structure == "vertical_credit":
            direction = str(spec.get("direction") or "").lower()
            return build_vertical_credit(
                chain,
                direction=direction,
                expiry=expiry,
                short_strike=_sf("short_strike"),
                long_strike=_sf("long_strike"),
                contracts=contracts,
            )
        if structure == "iron_condor":
            return build_iron_condor(
                chain,
                expiry=expiry,
                short_put_strike=_sf("short_put_strike"),
                long_put_strike=_sf("long_put_strike"),
                short_call_strike=_sf("short_call_strike"),
                long_call_strike=_sf("long_call_strike"),
                contracts=contracts,
            )
        raise BuilderError(f"unsupported structure {structure!r}")


def _opt_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _summarize_context_per_symbol(
    ordered: list[str],
    prices: dict[str, float],
    per_symbol_news: dict[str, list[NewsItem]],
    iv_by_symbol: dict[str, IvSnapshot],
    shortlist,
    movers: MoversSnapshot | None,
) -> dict[str, str]:
    """Produce a terse context blurb per symbol for the research agents.

    We keep this small on purpose — the research agent has its own tool
    budget to fill gaps; the point here is to give it the crumbs we
    already paid to fetch so it doesn't redundantly search."""
    shortlist_by_sym = {}
    if shortlist is not None:
        shortlist_by_sym = {c.symbol: c for c in shortlist.candidates}
    mover_by_sym: dict[str, object] = {}
    if movers is not None:
        for m in list(movers.gainers) + list(movers.losers) + list(movers.most_active):
            mover_by_sym.setdefault(m.symbol, m)

    out: dict[str, str] = {}
    for sym in ordered:
        parts: list[str] = []
        if sym in prices:
            parts.append(f"price ${prices[sym]:.2f}")
        iv = iv_by_symbol.get(sym)
        if iv and iv.atm_iv is not None:
            parts.append(f"ATM IV {iv.atm_iv * 100:.1f}% ({iv.regime})")
        c = shortlist_by_sym.get(sym)
        if c is not None:
            parts.append(
                f"screener: vol {c.vol_ratio:.1f}x, "
                f"{c.pct_change * 100:+.2f}% day, gap {c.gap_pct * 100:+.2f}%"
            )
        m = mover_by_sym.get(sym)
        if m is not None:
            pct = getattr(m, "percent_change", None)
            if pct is not None:
                parts.append(f"mover {pct:+.2f}%")
        news = per_symbol_news.get(sym) or []
        if news:
            parts.append(
                "recent: " + "; ".join(n.headline[:100] for n in news[:2] if n.headline)
            )
        out[sym] = " · ".join(parts) if parts else ""
    return out


async def _collect_iv_snapshots(
    client: OptionsClient,
    symbols: list[str],
    prices: dict[str, float],
) -> dict[str, IvSnapshot]:
    """Pull ATM implied-volatility per symbol in parallel.

    One chain call per symbol — the client already caches so repeated
    cycles hit memory. Sets a timeout to avoid holding up a decision
    cycle if the options endpoint is slow."""

    async def _one(sym: str) -> tuple[str, IvSnapshot]:
        try:
            chain = await asyncio.wait_for(client.chain(sym), timeout=5.0)
        except (TimeoutError, Exception):
            logger.warning("iv fetch failed for %s", sym, exc_info=True)
            return sym, IvSnapshot(symbol=sym, atm_iv=None, expiry=None)
        if chain is None:
            return sym, IvSnapshot(symbol=sym, atm_iv=None, expiry=None)
        spot = prices.get(sym)
        return sym, _atm_iv_snapshot(chain, spot)

    results = await asyncio.gather(*(_one(s) for s in symbols))
    return {sym: iv for sym, iv in results}


def _atm_iv_snapshot(chain: OptionChain, spot: float | None) -> IvSnapshot:
    """Pick the nearest usable expiry and report ATM IV for that strip.

    'Usable' = first expiry at least 7 days out if one exists, else the
    nearest. ATM IV = average of nearest-strike call + put IV, ignoring
    None values."""

    expiries = chain.expiries()
    if not expiries:
        return IvSnapshot(symbol=chain.underlying, atm_iv=None, expiry=None)

    from datetime import date

    from app.clock import ny_today

    today = ny_today()
    chosen: str | None = None
    for e in expiries:
        try:
            dte = (date.fromisoformat(e) - today).days
        except ValueError:
            continue
        if dte >= 7:
            chosen = e
            break
    if chosen is None:
        chosen = expiries[0]

    contracts = chain.for_expiry(chosen)
    if not contracts or spot is None:
        return IvSnapshot(symbol=chain.underlying, atm_iv=None, expiry=chosen)

    nearest = min(contracts, key=lambda c: abs(c.strike - spot))
    near_strike = nearest.strike
    ivs = [
        c.implied_volatility
        for c in contracts
        if abs(c.strike - near_strike) < 1e-6 and c.implied_volatility is not None
    ]
    atm_iv = sum(ivs) / len(ivs) if ivs else None
    return IvSnapshot(
        symbol=chain.underlying,
        atm_iv=atm_iv,
        expiry=chosen,
        regime=_iv_regime_label(atm_iv),
    )


def _iv_regime_label(atm_iv: float | None) -> str:
    """Deterministic bucket label used for prompt readability.

    This is a *display* categorisation, not a selection rule — the AI
    still decides what to do. Keeps the three human-readable labels the
    prompt references stable."""
    if atm_iv is None:
        return "unknown"
    if atm_iv >= 0.50:
        return "rich"
    if atm_iv <= 0.20:
        return "cheap"
    return "normal"


async def _recent_lessons_block(
    session_factory: async_sessionmaker | None, *, limit: int = 15
) -> str | None:
    """Pull the most recent trade post-mortems for the decision prompt.

    Returns None when the table is empty or session_factory is unset so the
    prompt just omits the section.
    """
    if session_factory is None:
        return None
    from sqlalchemy import desc, select

    from app.models import TradePostMortem

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(TradePostMortem)
                .order_by(desc(TradePostMortem.id))
                .limit(limit)
            )
        ).scalars().all()
    if not rows:
        return None
    lines: list[str] = []
    for row in rows:
        lines.append(
            f"  · {row.symbol} [{row.verdict}] "
            f"pnl=${row.realized_pnl_usd:+.2f} — {row.lesson[:220]}"
        )
    return "\n".join(lines)
