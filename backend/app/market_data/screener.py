"""Unusual-volume screener — find the needle in the haystack.

Scans a pre-filtered universe using Alpaca's multi-symbol snapshots
endpoint in batches, emits raw intraday signals per candidate, and
returns the top-K by unusual volume. The AI does the weighting.

Signals emitted (all derived from a single snapshot call — no historical
bars, no composite):
- `vol_ratio`   = today_daily_vol / prev_daily_vol          (unusual volume)
- `gap_pct`     = (today_open - prev_close) / prev_close    (overnight gap)
- `pct_change`  = (last - prev_close) / prev_close          (intraday move)
- `range_pct`   = (high - low) / low                        (intraday volatility)

Ranking is a single objective measure — `vol_ratio` — because "heavy
volume versus yesterday" is the one thing worth being deterministic
about (it's how we surface names that aren't otherwise visible). All
other signal weighting (is a big mover without volume real? does the
gap matter?) is the AI's job, not ours — it sees the raw values and
decides.

Liquidity floor (price ≥ $5, prev daily volume ≥ 500k) is a safety
pre-filter, not a selection signal. Cached for 60s.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from .universe import UniverseClient

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 60
_BATCH_SIZE = 100
_MAX_CONCURRENT_BATCHES = 6
_DEFAULT_MIN_PRICE = 5.0
_DEFAULT_MIN_PREV_VOL = 500_000


@dataclass(frozen=True, slots=True)
class ScreenerCandidate:
    """Raw per-symbol signals the AI uses to rank. No composite score —
    the AI weighs vol_ratio vs pct_change vs gap on its own."""

    symbol: str
    price: float
    prev_close: float
    pct_change: float
    gap_pct: float
    range_pct: float
    vol_ratio: float
    today_volume: int
    prev_volume: int
    optionable: bool

    def headline_reason(self) -> str:
        parts: list[str] = []
        if abs(self.pct_change) >= 0.005:
            sign = "+" if self.pct_change >= 0 else ""
            parts.append(f"{sign}{self.pct_change * 100:.1f}%")
        if self.vol_ratio >= 1.5:
            parts.append(f"vol {self.vol_ratio:.1f}x")
        if abs(self.gap_pct) >= 0.01:
            sign = "+" if self.gap_pct >= 0 else ""
            parts.append(f"gap {sign}{self.gap_pct * 100:.1f}%")
        return " · ".join(parts) or "screener"


@dataclass(frozen=True, slots=True)
class ScreenerSnapshot:
    candidates: list[ScreenerCandidate]
    universe_size: int
    scored: int
    fetched_at: datetime
    excluded: int = 0


class Screener:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        universe: UniverseClient,
        *,
        data_url: str = "https://data.alpaca.markets",
        timeout: float = 15.0,
        top_k: int = 20,
        min_price: float = _DEFAULT_MIN_PRICE,
        min_prev_volume: int = _DEFAULT_MIN_PREV_VOL,
    ) -> None:
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }
        self._data_url = data_url.rstrip("/")
        self._timeout = timeout
        self._universe = universe
        self._top_k = top_k
        self._min_price = min_price
        self._min_prev_volume = min_prev_volume
        self._cache: tuple[float, ScreenerSnapshot] | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return all(v and "replace_me" not in v for v in self._headers.values())

    async def shortlist(self, *, top_k: int | None = None) -> ScreenerSnapshot | None:
        if not self.enabled:
            return None
        k = top_k or self._top_k

        async with self._lock:
            if self._cache and self._cache[0] > time.time():
                snap = self._cache[1]
                return _trim(snap, k)

        assets = await self._universe.fetch()
        if not assets:
            return None

        symbols = [a.symbol for a in assets]
        optionable_by_symbol = {a.symbol: a.optionable for a in assets}

        # Batch the snapshots call. We pick modest fan-out to stay well under
        # Alpaca's per-minute request budget while still finishing a full
        # universe scan in a few seconds.
        sem = asyncio.Semaphore(_MAX_CONCURRENT_BATCHES)
        batches = [
            symbols[i : i + _BATCH_SIZE] for i in range(0, len(symbols), _BATCH_SIZE)
        ]

        async with httpx.AsyncClient(
            timeout=self._timeout, headers=self._headers
        ) as client:

            async def _fetch(batch: list[str]) -> dict[str, Any]:
                async with sem:
                    try:
                        resp = await client.get(
                            f"{self._data_url}/v2/stocks/snapshots",
                            params={"symbols": ",".join(batch)},
                        )
                        resp.raise_for_status()
                        return resp.json() or {}
                    except httpx.HTTPStatusError as exc:
                        logger.warning(
                            "alpaca snapshots batch (%d syms) HTTP %s",
                            len(batch), exc.response.status_code,
                        )
                        return {}
                    except Exception as exc:
                        logger.warning(
                            "alpaca snapshots batch (%d syms) failed: %s",
                            len(batch), exc,
                        )
                        return {}

            results = await asyncio.gather(*(_fetch(b) for b in batches))

        merged: dict[str, Any] = {}
        for chunk in results:
            merged.update(chunk)

        scored: list[ScreenerCandidate] = []
        excluded = 0
        for sym, snap in merged.items():
            if not isinstance(snap, dict):
                continue
            cand = _extract_signals(
                sym, snap, optionable_by_symbol.get(sym, False)
            )
            if cand is None:
                continue
            if cand.price < self._min_price or cand.prev_volume < self._min_prev_volume:
                excluded += 1
                continue
            scored.append(cand)

        # Single objective rank: unusual volume. AI weighs the rest.
        scored.sort(key=lambda c: c.vol_ratio, reverse=True)
        snap = ScreenerSnapshot(
            candidates=scored[: max(k, self._top_k)],
            universe_size=len(symbols),
            scored=len(scored),
            excluded=excluded,
            fetched_at=datetime.now(UTC),
        )
        async with self._lock:
            self._cache = (time.time() + _CACHE_TTL_SEC, snap)
        return _trim(snap, k)


def _trim(snap: ScreenerSnapshot, k: int) -> ScreenerSnapshot:
    if len(snap.candidates) <= k:
        return snap
    return ScreenerSnapshot(
        candidates=snap.candidates[:k],
        universe_size=snap.universe_size,
        scored=snap.scored,
        excluded=snap.excluded,
        fetched_at=snap.fetched_at,
    )


def _extract_signals(
    symbol: str, snap: dict[str, Any], optionable: bool
) -> ScreenerCandidate | None:
    """Derive per-symbol signals from a snapshot. Pure arithmetic — no
    weighting or scoring."""
    daily = snap.get("dailyBar") or {}
    prev = snap.get("prevDailyBar") or {}
    trade = snap.get("latestTrade") or {}

    prev_close = _f(prev.get("c"))
    prev_vol = _i(prev.get("v"))
    today_open = _f(daily.get("o"))
    today_high = _f(daily.get("h"))
    today_low = _f(daily.get("l"))
    today_vol = _i(daily.get("v"))
    last = _f(trade.get("p")) or _f(daily.get("c"))

    if (
        prev_close is None
        or prev_close <= 0
        or last is None
        or last <= 0
        or prev_vol is None
        or prev_vol <= 0
    ):
        return None

    pct_change = (last - prev_close) / prev_close
    gap_pct = (
        (today_open - prev_close) / prev_close
        if today_open is not None and today_open > 0
        else 0.0
    )
    range_pct = (
        (today_high - today_low) / today_low
        if today_high is not None and today_low is not None and today_low > 0
        else 0.0
    )
    vol_ratio = (today_vol or 0) / prev_vol if prev_vol else 0.0

    return ScreenerCandidate(
        symbol=symbol.upper(),
        price=float(last),
        prev_close=float(prev_close),
        pct_change=pct_change,
        gap_pct=gap_pct,
        range_pct=range_pct,
        vol_ratio=vol_ratio,
        today_volume=int(today_vol or 0),
        prev_volume=int(prev_vol),
        optionable=optionable,
    )


def _f(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


__all__ = ["Screener", "ScreenerCandidate", "ScreenerSnapshot"]
