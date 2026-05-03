"""Aggregate the context the AI sees each decision cycle.

Exposes a single read-only view of watchlist quotes, news, positions, and
latest AI verdict per symbol — the same information surface fed into the
Claude/LM Studio prompt, rendered for humans to audit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import (
    CandidateOut,
    DiscoveryOut,
    MarketIntelOut,
    MoverOut,
    NewsItemOut,
    QuoteOut,
    SymbolDecisionOut,
    SymbolIntelOut,
)
from app.brokers import build_broker
from app.market_data.finnhub import FinnhubClient, NewsItem, Quote
from app.market_data.movers import Mover, MoversClient, MoversSnapshot
from app.market_data.screener import Screener, ScreenerSnapshot
from app.market_data.universe import UniverseClient
from app.models import Decision, utc_now
from app.risk import Market

logger = logging.getLogger(__name__)

# --- Module-level singletons --------------------------------------------------
# Each client has its own TTL cache; keeping the instance alive across requests
# is what lets those caches actually save work. Rebuilding fresh on every
# /intel poll invalidates every warm cache in the stack.

_OUTPUT_TTL_SEC = 15.0


@dataclass
class _ClientBundle:
    finnhub: FinnhubClient | None
    movers: MoversClient | None
    screener: Screener | None
    broker: Any  # BrokerAdapter (kept loose to avoid a cycle)


_client_bundle: _ClientBundle | None = None
_client_key: tuple[str, str, str] | None = None
_client_lock = asyncio.Lock()

_output_cache: tuple[float, MarketIntelOut] | None = None
_output_lock = asyncio.Lock()


async def _get_clients(settings) -> _ClientBundle:
    """Return cached clients for the current credential set.

    Keyed on the three secrets that matter so rotating a key rebuilds the
    stack rather than silently keeping a dead client."""
    global _client_bundle, _client_key
    key = (
        settings.finnhub_api_key or "",
        settings.alpaca_api_key or "",
        settings.alpaca_api_secret or "",
    )
    async with _client_lock:
        if _client_bundle is not None and _client_key == key:
            return _client_bundle

        finnhub = (
            FinnhubClient(settings.finnhub_api_key)
            if settings.finnhub_api_key
            else None
        )
        movers: MoversClient | None = None
        screener: Screener | None = None
        if "replace_me" not in settings.alpaca_api_key:
            movers = MoversClient(
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
        broker = build_broker(Market.STOCKS, settings)
        _client_bundle = _ClientBundle(
            finnhub=finnhub, movers=movers, screener=screener, broker=broker
        )
        _client_key = key
        return _client_bundle


async def collect_market_intel(settings, db: AsyncSession) -> MarketIntelOut:
    global _output_cache
    now = time.time()
    async with _output_lock:
        if _output_cache and _output_cache[0] > now:
            return _output_cache[1]

    result = await _collect_market_intel_uncached(settings, db)
    async with _output_lock:
        _output_cache = (time.time() + _OUTPUT_TTL_SEC, result)
    return result


async def _collect_market_intel_uncached(
    settings, db: AsyncSession
) -> MarketIntelOut:
    discovery_detail_cap = 12  # hydrated news/quote per candidate — all come from discovery now
    bundle = await _get_clients(settings)
    finnhub = bundle.finnhub
    movers_client = bundle.movers
    screener = bundle.screener
    broker = bundle.broker
    news_enabled = finnhub is not None and finnhub.enabled

    try:
        positions = await broker.get_positions()
    except Exception:
        logger.warning("broker.get_positions failed in intel", exc_info=True)
        positions = []

    position_by_symbol = {
        p.symbol: p for p in positions if p.market == Market.STOCKS
    }

    movers_task: asyncio.Task | None = None
    if movers_client and movers_client.enabled:
        movers_task = asyncio.create_task(movers_client.fetch())
    shortlist_task: asyncio.Task | None = None
    if screener and screener.enabled:
        shortlist_task = asyncio.create_task(screener.shortlist())

    market_news_task: asyncio.Task | None = None
    if finnhub:
        market_news_task = asyncio.create_task(finnhub.market_news(limit=10))

    # Fan out per-symbol quote + news in parallel
    async def fetch_symbol(sym: str) -> tuple[Quote | None, list[NewsItem]]:
        if not finnhub:
            return None, []
        q_task = asyncio.create_task(finnhub.quote(sym))
        n_task = asyncio.create_task(finnhub.company_news(sym, limit=5))
        q = await q_task
        n = await n_task
        return q, n

    # Seed detail symbols with positions so the UI always has context on
    # what you're holding, even when discovery is empty.
    detail_symbols: list[str] = list(position_by_symbol.keys())
    symbol_results = await asyncio.gather(
        *(fetch_symbol(sym) for sym in detail_symbols),
        return_exceptions=False,
    )

    market_news: list[NewsItem] = []
    if market_news_task:
        try:
            market_news = await market_news_task
        except Exception:
            logger.warning("finnhub market_news task failed", exc_info=True)

    movers: MoversSnapshot | None = None
    if movers_task:
        try:
            movers = await movers_task
        except Exception:
            logger.warning("alpaca movers task failed", exc_info=True)
    discovery_out = _discovery_to_schema(movers, enabled=movers_client is not None)

    shortlist: ScreenerSnapshot | None = None
    if shortlist_task:
        try:
            shortlist = await shortlist_task
        except Exception:
            logger.warning("alpaca screener task failed", exc_info=True)

    # Hydrate a bounded set of discovery + shortlist symbols with quote/news
    # so clicking a candidate card in the UI shows full context, not just the
    # thin screener row.
    extras: list[str] = []
    extra_cap = discovery_detail_cap
    if movers:
        for m in movers.top_symbols(per_bucket=3):
            if m not in detail_symbols and m not in extras and len(extras) < extra_cap:
                extras.append(m)
    if shortlist:
        for c in shortlist.candidates:
            if (
                c.symbol not in detail_symbols
                and c.symbol not in extras
                and len(extras) < extra_cap + 6
            ):
                extras.append(c.symbol)
    if extras:
        extra_results = await asyncio.gather(
            *(fetch_symbol(sym) for sym in extras),
            return_exceptions=False,
        )
        detail_symbols = detail_symbols + extras
        symbol_results = list(symbol_results) + list(extra_results)

    # Last decision per symbol (stocks market). Pull a recent window of
    # decisions once and index by proposal symbol — N SQLite roundtrips
    # collapse to one even as the watchlist grows.
    latest_by_symbol = await _latest_decisions_by_symbol(
        db,
        detail_symbols,
        position_symbols=set(position_by_symbol.keys()),
    )
    symbols_out: list[SymbolIntelOut] = []
    for sym, (quote, news) in zip(detail_symbols, symbol_results, strict=True):
        pos = position_by_symbol.get(sym)
        last = latest_by_symbol.get(sym)

        symbols_out.append(
            SymbolIntelOut(
                symbol=sym,
                quote=_quote_to_schema(quote),
                news=[_news_to_schema(n) for n in news],
                position_size_usd=pos.size_usd if pos else None,
                position_unrealized_pnl=(
                    (pos.current_price - pos.entry_price)
                    * (pos.size_usd / pos.entry_price)
                    if pos and pos.entry_price
                    else None
                ),
                last_decision=last,
            )
        )

    candidates = _build_candidates(
        positions_by_symbol=position_by_symbol,
        symbols_out=symbols_out,
        movers=movers,
        shortlist=shortlist,
    )

    return MarketIntelOut(
        candidates=candidates,
        symbols=symbols_out,
        market_news=[_news_to_schema(n) for n in market_news],
        discovery=discovery_out,
        checked_at=utc_now(),
        news_enabled=news_enabled,
    )


def _build_candidates(
    *,
    positions_by_symbol: dict,
    symbols_out: list[SymbolIntelOut],
    movers: MoversSnapshot | None,
    shortlist: ScreenerSnapshot | None,
) -> list[CandidateOut]:
    """Compose the shortlist the AI is actively thinking about this cycle.

    Priority (dedup in this order):
      1. Open positions — active thesis, must be watched
      2. Recent approved AI decisions — thesis in flight
      3. Top discovery movers — fresh candidates from the screener
      4. Full-universe screener shortlist — needles found by unusual
         volume + price action across all tradable names
    """
    seen: set[str] = set()
    out: list[CandidateOut] = []
    sym_index = {s.symbol: s for s in symbols_out}

    # 1) Positions
    for sym, pos in positions_by_symbol.items():
        if sym in seen:
            continue
        unreal = None
        if pos.entry_price:
            unreal = (pos.current_price - pos.entry_price) * (
                pos.size_usd / pos.entry_price
            )
        note = f"held ${pos.size_usd:.0f}"
        if unreal is not None:
            note += f" ({'+' if unreal >= 0 else ''}${unreal:.2f} unreal)"
        out.append(CandidateOut(symbol=sym, reason="position", note=note))
        seen.add(sym)

    # 2) Recent approved decisions — pull from symbols_out since the intel
    # collector already resolved the last-decision lookup per-symbol.
    approved: list[tuple[str, SymbolIntelOut]] = []
    for s in symbols_out:
        d = s.last_decision
        if d and (d.approved or d.executed) and s.symbol not in seen:
            approved.append((s.symbol, s))
    approved.sort(key=lambda pair: pair[1].last_decision.created_at, reverse=True)
    for sym, s in approved[:5]:
        d = s.last_decision
        action = d.action or "?"
        verb = "executed" if d.executed else "approved"
        out.append(
            CandidateOut(
                symbol=sym,
                reason="recent_approved",
                note=f"{verb} {action}",
            )
        )
        seen.add(sym)

    # 3) Top discovery movers — one from each bucket, up to 5 total.
    if movers is not None:
        buckets = [
            ("gainer", movers.gainers),
            ("loser", movers.losers),
            ("active", movers.most_active),
        ]
        for label, items in buckets:
            picked = 0
            for m in items:
                if m.symbol in seen:
                    continue
                note = label
                if m.percent_change is not None:
                    note = f"{label} {'+' if m.percent_change >= 0 else ''}{m.percent_change:.1f}%"
                elif m.volume is not None:
                    note = f"{label} vol {_compact(m.volume)}"
                out.append(
                    CandidateOut(symbol=m.symbol, reason="discovery", note=note)
                )
                seen.add(m.symbol)
                picked += 1
                if picked >= 2:
                    break

    # 4) Full-universe screener shortlist — the needle in the haystack.
    if shortlist is not None:
        for c in shortlist.candidates:
            if c.symbol in seen:
                continue
            out.append(
                CandidateOut(
                    symbol=c.symbol,
                    reason="shortlist",
                    note=c.headline_reason(),
                )
            )
            seen.add(c.symbol)

    # Rank is just insertion order.
    for i, c in enumerate(out):
        c.rank = i + 1

    return out


def _compact(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1e9:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1_000:
        return f"{n / 1e3:.0f}k"
    return str(n)


async def _latest_decisions_by_symbol(
    db: AsyncSession,
    symbols: list[str],
    *,
    position_symbols: set[str],
) -> dict[str, SymbolDecisionOut]:
    """Map each requested symbol to the AI's most recent verdict.

    Two sources are merged:
      1. Per-symbol decisions — rows whose ``proposal_json.symbol`` matches.
      2. Cycle-wide HOLD decisions — rows with ``rejection_code ==
         'strategy_no_op'``. These carry no symbol on the proposal because
         the AI chose not to act anywhere. For symbols the user is holding,
         a hold that is newer than any per-symbol decision is the most
         accurate "why am I still in this trade?" answer, so we attach it
         as a fallback.

    One query replaces what used to be a per-symbol scan.
    """
    if not symbols:
        return {}
    wanted = set(symbols)
    stmt = (
        select(Decision)
        .where(Decision.market == Market.STOCKS.value)
        .order_by(desc(Decision.id))
        .limit(400)
    )
    rows = (await db.execute(stmt)).scalars().all()

    per_symbol_row: dict[str, Decision] = {}
    latest_hold: Decision | None = None
    for row in rows:
        prop = row.proposal_json or {}
        sym = prop.get("symbol")
        if sym and sym in wanted and sym not in per_symbol_row:
            per_symbol_row[sym] = row
        elif (
            not sym
            and latest_hold is None
            and row.rejection_code == "strategy_no_op"
        ):
            latest_hold = row

    def _from_row(row: Decision, *, as_hold: bool = False) -> SymbolDecisionOut:
        prop = row.proposal_json or {}
        return SymbolDecisionOut(
            id=row.id,
            created_at=row.created_at,
            action="hold" if as_hold else prop.get("action"),
            approved=row.approved,
            executed=row.executed,
            rationale=row.rationale,
            rejection_code=None if as_hold else row.rejection_code,
        )

    out: dict[str, SymbolDecisionOut] = {}
    for sym in wanted:
        per = per_symbol_row.get(sym)
        is_position = sym in position_symbols
        if per and (not latest_hold or per.id >= latest_hold.id):
            out[sym] = _from_row(per)
        elif latest_hold and (is_position or per):
            # Position symbols always get a verdict. Non-position symbols
            # only fall back to the cycle-wide hold if they had some prior
            # per-symbol decision to override.
            out[sym] = _from_row(latest_hold, as_hold=True)
        elif per:
            out[sym] = _from_row(per)
    return out


def _quote_to_schema(q: Quote | None) -> QuoteOut | None:
    if q is None:
        return None
    return QuoteOut(
        symbol=q.symbol,
        current=q.current,
        change=q.change,
        change_pct=q.change_pct,
        open=q.open,
        high=q.high,
        low=q.low,
        prev_close=q.prev_close,
        ts=q.ts,
    )


def _discovery_to_schema(
    snap: MoversSnapshot | None, *, enabled: bool
) -> DiscoveryOut:
    if snap is None:
        return DiscoveryOut(enabled=enabled)
    return DiscoveryOut(
        enabled=enabled,
        gainers=[_mover_to_schema(m) for m in snap.gainers],
        losers=[_mover_to_schema(m) for m in snap.losers],
        most_active=[_mover_to_schema(m) for m in snap.most_active],
        last_updated=snap.last_updated,
        fetched_at=snap.fetched_at,
    )


def _mover_to_schema(m: Mover) -> MoverOut:
    return MoverOut(
        symbol=m.symbol,
        category=m.category,
        price=m.price,
        change=m.change,
        percent_change=m.percent_change,
        volume=m.volume,
        trade_count=m.trade_count,
    )


def _news_to_schema(n: NewsItem) -> NewsItemOut:
    return NewsItemOut(
        symbol=n.symbol,
        headline=n.headline,
        summary=n.summary,
        source=n.source,
        url=n.url,
        datetime=n.datetime,
    )


__all__ = ["collect_market_intel"]
