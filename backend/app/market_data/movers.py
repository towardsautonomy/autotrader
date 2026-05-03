"""Alpaca screener — top gainers, losers, and most-active names.

Surfaces the *whole tape*, not just the fixed watchlist, so the AI can act
on opportunities that appear intraday. Enforces a liquidity floor so we
don't hand it pump-and-dump micro-caps that our risk engine would reject
downstream anyway.

Uses the v1beta1 screener endpoints:
- GET /v1beta1/screener/stocks/movers?top=N
- GET /v1beta1/screener/stocks/most-actives?top=N&by=volume
Both are included in Alpaca's free/basic data tier.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_MIN_PRICE = 5.0
_DEFAULT_MIN_TRADE_COUNT = 10_000
_CACHE_TTL_SEC = 60


@dataclass(frozen=True, slots=True)
class Mover:
    symbol: str
    category: str  # "gainer" | "loser" | "most_active"
    price: float | None = None
    change: float | None = None
    percent_change: float | None = None
    volume: int | None = None
    trade_count: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "category": self.category,
            "price": self.price,
            "change": self.change,
            "percent_change": self.percent_change,
            "volume": self.volume,
            "trade_count": self.trade_count,
        }


@dataclass(frozen=True, slots=True)
class MoversSnapshot:
    gainers: list[Mover]
    losers: list[Mover]
    most_active: list[Mover]
    fetched_at: datetime
    last_updated: datetime | None = None

    def top_symbols(self, *, per_bucket: int = 5) -> list[str]:
        """Deduped, ranked symbols from each bucket, prioritising movers."""
        seen: set[str] = set()
        ordered: list[str] = []
        for bucket in (self.gainers, self.losers, self.most_active):
            for m in bucket[:per_bucket]:
                if m.symbol not in seen:
                    seen.add(m.symbol)
                    ordered.append(m.symbol)
        return ordered


class MoversClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        data_url: str = "https://data.alpaca.markets",
        timeout: float = 5.0,
        min_price: float = _DEFAULT_MIN_PRICE,
        min_trade_count: int = _DEFAULT_MIN_TRADE_COUNT,
    ) -> None:
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }
        self._base = f"{data_url.rstrip('/')}/v1beta1/screener/stocks"
        self._timeout = timeout
        self._min_price = min_price
        self._min_trade_count = min_trade_count
        self._cache: tuple[float, MoversSnapshot] | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return all(v and "replace_me" not in v for v in self._headers.values())

    async def fetch(self, *, top: int = 20) -> MoversSnapshot | None:
        if not self.enabled:
            return None

        async with self._lock:
            if self._cache and self._cache[0] > time.time():
                return self._cache[1]

        async with httpx.AsyncClient(timeout=self._timeout, headers=self._headers) as client:
            try:
                movers_task = client.get(f"{self._base}/movers", params={"top": top})
                actives_task = client.get(
                    f"{self._base}/most-actives",
                    params={"top": top, "by": "volume"},
                )
                movers_resp, actives_resp = await asyncio.gather(
                    movers_task, actives_task
                )
                movers_resp.raise_for_status()
                actives_resp.raise_for_status()
                movers_json = movers_resp.json()
                actives_json = actives_resp.json()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "alpaca movers fetch HTTP %s: %s",
                    exc.response.status_code,
                    exc.response.text[:200],
                )
                return None
            except Exception as exc:
                logger.warning("alpaca movers fetch failed: %s", exc)
                return None

        # Price/liquidity lookups for most-actives (screener doesn't return price
        # there) are skipped for cost; we preserve volume + trade_count instead.
        gainers = self._parse_movers(movers_json.get("gainers", []), "gainer")
        losers = self._parse_movers(movers_json.get("losers", []), "loser")
        most_active = self._parse_actives(actives_json.get("most_actives", []))

        last_updated = _parse_iso(movers_json.get("last_updated"))

        snap = MoversSnapshot(
            gainers=gainers,
            losers=losers,
            most_active=most_active,
            fetched_at=datetime.now(UTC),
            last_updated=last_updated,
        )
        async with self._lock:
            self._cache = (time.time() + _CACHE_TTL_SEC, snap)
        return snap

    def _parse_movers(self, rows: list[dict[str, Any]], category: str) -> list[Mover]:
        out: list[Mover] = []
        for row in rows:
            price = _to_float(row.get("price"))
            if price is None or price < self._min_price:
                continue
            out.append(
                Mover(
                    symbol=str(row.get("symbol", "")).upper(),
                    category=category,
                    price=price,
                    change=_to_float(row.get("change")),
                    percent_change=_to_float(row.get("percent_change")),
                )
            )
        return out

    def _parse_actives(self, rows: list[dict[str, Any]]) -> list[Mover]:
        out: list[Mover] = []
        for row in rows:
            tc = row.get("trade_count")
            if isinstance(tc, int | float) and tc < self._min_trade_count:
                continue
            out.append(
                Mover(
                    symbol=str(row.get("symbol", "")).upper(),
                    category="most_active",
                    volume=int(row["volume"]) if row.get("volume") is not None else None,
                    trade_count=int(tc) if tc is not None else None,
                )
            )
        return out


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(v: Any) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        s = v.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except ValueError:
        return None


__all__ = ["Mover", "MoversClient", "MoversSnapshot"]
