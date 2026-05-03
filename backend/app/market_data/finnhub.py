"""Finnhub client — company news, general market news, and company profile.

Finnhub has a generous free tier (60 req/min). We use it to inject recent
headlines into the AI prompt so decisions are news-aware rather than
purely technical.

All calls are HTTP GETs against https://finnhub.io/api/v1. The SDK is thin
— we keep it hand-rolled with httpx so we don't drag in an opinionated
client and so we can add caching/rate-limiting in one place.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://finnhub.io/api/v1"
_NEWS_TTL_SEC = 300  # headlines rarely move faster than 5 min for our use
_QUOTE_TTL_SEC = 30  # quotes move faster; 30s keeps Finnhub well under its rate limit


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    current: float
    change: float
    change_pct: float
    open: float
    high: float
    low: float
    prev_close: float
    ts: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "current": self.current,
            "change": self.change,
            "change_pct": self.change_pct,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "prev_close": self.prev_close,
            "ts": self.ts.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class NewsItem:
    symbol: str | None
    headline: str
    summary: str
    source: str
    url: str
    datetime: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "headline": self.headline,
            "summary": self.summary,
            "source": self.source,
            "url": self.url,
            "datetime": self.datetime.isoformat(),
        }


class FinnhubClient:
    def __init__(self, api_key: str, *, timeout: float = 5.0) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._cache: dict[str, tuple[float, list[NewsItem]]] = {}
        self._quote_cache: dict[str, tuple[float, Quote]] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        if not self.enabled:
            return None
        params = {**params, "token": self._api_key}
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(f"{BASE_URL}{path}", params=params)
            # 403 = endpoint not on free tier, or symbol not covered
            # (e.g. international tickers like 3665.TW).
            # 404 = symbol unknown. Both are "no data" rather than bugs —
            # log quietly and return None so callers stay calm.
            if resp.status_code in (403, 404):
                sym = params.get("symbol") or ""
                logger.info(
                    "finnhub %s %s (%s) — no data on free tier / symbol not covered",
                    path, sym, resp.status_code,
                )
                return None
            if resp.status_code == 429:
                logger.warning(
                    "finnhub %s rate-limited (429) — backing off this cycle",
                    path,
                )
                return None
            resp.raise_for_status()
            return resp.json()

    async def company_news(
        self, symbol: str, *, lookback_days: int = 2, limit: int = 5
    ) -> list[NewsItem]:
        if not self.enabled:
            return []
        cache_key = f"company:{symbol}:{lookback_days}:{limit}"
        now = time.time()
        async with self._lock:
            entry = self._cache.get(cache_key)
            if entry and entry[0] > now:
                return entry[1]

        to_dt = datetime.now(UTC).date()
        from_dt = to_dt - timedelta(days=lookback_days)
        try:
            data = await self._get(
                "/company-news",
                {"symbol": symbol, "from": from_dt.isoformat(), "to": to_dt.isoformat()},
            )
        except Exception:
            logger.warning("finnhub company_news failed for %s", symbol, exc_info=True)
            return []

        items = self._parse_news(data or [], default_symbol=symbol)[:limit]
        async with self._lock:
            self._cache[cache_key] = (now + _NEWS_TTL_SEC, items)
        return items

    async def market_news(self, *, category: str = "general", limit: int = 5) -> list[NewsItem]:
        if not self.enabled:
            return []
        cache_key = f"market:{category}:{limit}"
        now = time.time()
        async with self._lock:
            entry = self._cache.get(cache_key)
            if entry and entry[0] > now:
                return entry[1]

        try:
            data = await self._get("/news", {"category": category})
        except Exception:
            logger.warning("finnhub market_news failed", exc_info=True)
            return []

        items = self._parse_news(data or [])[:limit]
        async with self._lock:
            self._cache[cache_key] = (now + _NEWS_TTL_SEC, items)
        return items

    async def symbol_search(
        self, query: str, *, exchange: str = "US", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Look up tickers by company name or fuzzy query.

        Hits Finnhub's `/search` endpoint. Returns entries with
        `symbol`, `description`, `displaySymbol`, `type`. Filters to
        common stock on US exchanges by default — Finnhub otherwise
        returns foreign listings, ETFs and rights/warrants that pollute
        the result list for typical "what's the ticker for X" queries.
        """
        if not self.enabled or not query.strip():
            return []
        try:
            data = await self._get(
                "/search", {"q": query.strip(), "exchange": exchange}
            )
        except Exception:
            logger.warning("finnhub search failed for %r", query, exc_info=True)
            return []
        if not isinstance(data, dict):
            return []
        raw = data.get("result") or []
        out: list[dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "").strip()
            if not sym or "." in sym:
                continue
            out.append(
                {
                    "symbol": sym,
                    "display_symbol": str(item.get("displaySymbol") or sym),
                    "description": str(item.get("description") or ""),
                    "type": str(item.get("type") or ""),
                }
            )
            if len(out) >= limit:
                break
        return out

    async def company_profile(self, symbol: str) -> dict[str, Any] | None:
        """Basic company data: name, industry, exchange, market cap, website.

        The single best tool for answering "what is ticker X?" — the
        other endpoints need this context to make sense.
        """
        if not self.enabled:
            return None
        try:
            data = await self._get("/stock/profile2", {"symbol": symbol})
        except Exception:
            logger.warning("finnhub profile2 failed for %s", symbol, exc_info=True)
            return None
        if not data or not isinstance(data, dict):
            return None
        return data

    async def peers(self, symbol: str) -> list[str]:
        """Return competitor tickers for a given symbol (may include itself)."""
        if not self.enabled:
            return []
        try:
            data = await self._get("/stock/peers", {"symbol": symbol})
        except Exception:
            logger.warning("finnhub peers failed for %s", symbol, exc_info=True)
            return []
        if not isinstance(data, list):
            return []
        return [str(t) for t in data if isinstance(t, str)]

    async def basic_financials(self, symbol: str) -> dict[str, Any] | None:
        """Ratio/metric bundle: 52w high/low, P/E, beta, margins, etc."""
        if not self.enabled:
            return None
        try:
            data = await self._get(
                "/stock/metric", {"symbol": symbol, "metric": "all"}
            )
        except Exception:
            logger.warning(
                "finnhub basic_financials failed for %s", symbol, exc_info=True
            )
            return None
        if not data or not isinstance(data, dict):
            return None
        return data

    async def recommendations(self, symbol: str) -> list[dict[str, Any]]:
        """Analyst buy/hold/sell counts by month (most recent first)."""
        if not self.enabled:
            return []
        try:
            data = await self._get("/stock/recommendation", {"symbol": symbol})
        except Exception:
            logger.warning(
                "finnhub recommendations failed for %s", symbol, exc_info=True
            )
            return []
        if not isinstance(data, list):
            return []
        return data

    async def price_target(self, symbol: str) -> dict[str, Any] | None:
        """Consensus price target — high / low / median / mean."""
        if not self.enabled:
            return None
        try:
            data = await self._get("/stock/price-target", {"symbol": symbol})
        except Exception:
            logger.warning(
                "finnhub price_target failed for %s", symbol, exc_info=True
            )
            return None
        if not isinstance(data, dict):
            return None
        return data

    async def insider_transactions(
        self, symbol: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Recent insider buys/sells from Form 4 filings."""
        if not self.enabled:
            return []
        try:
            data = await self._get(
                "/stock/insider-transactions", {"symbol": symbol}
            )
        except Exception:
            logger.warning(
                "finnhub insider_transactions failed for %s", symbol, exc_info=True
            )
            return []
        if not isinstance(data, dict):
            return []
        rows = data.get("data") or []
        if not isinstance(rows, list):
            return []
        # Most recent first, capped.
        rows.sort(key=lambda r: r.get("transactionDate") or "", reverse=True)
        return rows[:limit]

    async def ownership(
        self, symbol: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Top institutional shareholders (13F filings)."""
        if not self.enabled:
            return []
        try:
            data = await self._get(
                "/stock/ownership", {"symbol": symbol, "limit": limit}
            )
        except Exception:
            logger.warning(
                "finnhub ownership failed for %s", symbol, exc_info=True
            )
            return []
        if not isinstance(data, dict):
            return []
        rows = data.get("ownership") or []
        if not isinstance(rows, list):
            return []
        return rows[:limit]

    async def fund_ownership(
        self, symbol: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Top mutual-fund shareholders."""
        if not self.enabled:
            return []
        try:
            data = await self._get(
                "/stock/fund-ownership", {"symbol": symbol, "limit": limit}
            )
        except Exception:
            logger.warning(
                "finnhub fund_ownership failed for %s", symbol, exc_info=True
            )
            return []
        if not isinstance(data, dict):
            return []
        rows = data.get("ownership") or []
        if not isinstance(rows, list):
            return []
        return rows[:limit]

    async def earnings_calendar(
        self,
        *,
        symbol: str | None = None,
        lookback_days: int = 30,
        lookahead_days: int = 60,
    ) -> list[dict[str, Any]]:
        """Earnings dates (past + upcoming). If symbol is set, filters to it."""
        if not self.enabled:
            return []
        today = datetime.now(UTC).date()
        from_dt = today - timedelta(days=lookback_days)
        to_dt = today + timedelta(days=lookahead_days)
        params: dict[str, Any] = {
            "from": from_dt.isoformat(),
            "to": to_dt.isoformat(),
        }
        if symbol:
            params["symbol"] = symbol
        try:
            data = await self._get("/calendar/earnings", params)
        except Exception:
            logger.warning("finnhub earnings_calendar failed", exc_info=True)
            return []
        if not isinstance(data, dict):
            return []
        items = data.get("earningsCalendar") or []
        if not isinstance(items, list):
            return []
        return items

    async def earnings_surprises(self, symbol: str) -> list[dict[str, Any]]:
        """Historical actual vs estimate EPS by quarter."""
        if not self.enabled:
            return []
        try:
            data = await self._get("/stock/earnings", {"symbol": symbol})
        except Exception:
            logger.warning(
                "finnhub earnings_surprises failed for %s", symbol, exc_info=True
            )
            return []
        if not isinstance(data, list):
            return []
        return data

    async def quote(self, symbol: str) -> Quote | None:
        if not self.enabled:
            return None
        now = time.time()
        async with self._lock:
            entry = self._quote_cache.get(symbol)
            if entry and entry[0] > now:
                return entry[1]

        try:
            data = await self._get("/quote", {"symbol": symbol})
        except Exception:
            logger.warning("finnhub quote failed for %s", symbol, exc_info=True)
            return None
        if not data or not data.get("c"):
            return None

        ts = data.get("t")
        when = datetime.fromtimestamp(ts, tz=UTC) if ts else datetime.now(UTC)
        q = Quote(
            symbol=symbol,
            current=float(data.get("c") or 0.0),
            change=float(data.get("d") or 0.0),
            change_pct=float(data.get("dp") or 0.0),
            open=float(data.get("o") or 0.0),
            high=float(data.get("h") or 0.0),
            low=float(data.get("l") or 0.0),
            prev_close=float(data.get("pc") or 0.0),
            ts=when,
        )
        async with self._lock:
            self._quote_cache[symbol] = (now + _QUOTE_TTL_SEC, q)
        return q

    @staticmethod
    def _parse_news(
        data: list[dict[str, Any]], *, default_symbol: str | None = None
    ) -> list[NewsItem]:
        out: list[NewsItem] = []
        for row in data:
            ts = row.get("datetime")
            if isinstance(ts, int | float):
                when = datetime.fromtimestamp(ts, tz=UTC)
            else:
                when = datetime.now(UTC)
            out.append(
                NewsItem(
                    symbol=row.get("related") or default_symbol,
                    headline=(row.get("headline") or "").strip(),
                    summary=(row.get("summary") or "").strip(),
                    source=row.get("source") or "",
                    url=row.get("url") or "",
                    datetime=when,
                )
            )
        out.sort(key=lambda n: n.datetime, reverse=True)
        return out
