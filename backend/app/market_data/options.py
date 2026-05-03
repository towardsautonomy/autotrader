"""Alpaca options market data client.

Fetches option contracts + snapshot greeks/IV for a given underlying. The
chain is returned as a list of `OptionContract` dataclasses keyed by
expiry + strike + side, so the strategy picker can shop for legs by
moneyness / delta without re-parsing OCC symbols.

Endpoints used (all under the v1beta1 options namespace):
- `GET /v1beta1/options/contracts?underlying_symbols=AAPL&limit=...`
- `GET /v1beta1/options/snapshots/{underlying}?feed=indicative`

Greeks come from the snapshots endpoint. If the snapshot is missing
(common for deep-OTM strikes with no recent trade) we still return the
contract with `None` greeks so callers can fall back to mid-price
heuristics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.risk import OptionSide

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 30
_CONTRACTS_PAGE = 1000


@dataclass(frozen=True, slots=True)
class OptionContract:
    symbol: str  # OCC symbol e.g. AAPL250117C00150000
    underlying: str
    side: OptionSide
    strike: float
    expiry: str  # ISO date
    bid: float | None
    ask: float | None
    mid: float | None
    last: float | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    open_interest: int | None
    volume: int | None

    @property
    def mid_or_last(self) -> float | None:
        return self.mid if self.mid is not None else self.last


@dataclass(frozen=True, slots=True)
class OptionChain:
    underlying: str
    contracts: tuple[OptionContract, ...]
    fetched_at: datetime

    def expiries(self) -> list[str]:
        seen = sorted({c.expiry for c in self.contracts})
        return seen

    def for_expiry(self, expiry: str) -> list[OptionContract]:
        return [c for c in self.contracts if c.expiry == expiry]

    def call_at(self, expiry: str, strike: float) -> OptionContract | None:
        return self._find(expiry, strike, OptionSide.CALL)

    def put_at(self, expiry: str, strike: float) -> OptionContract | None:
        return self._find(expiry, strike, OptionSide.PUT)

    def _find(
        self, expiry: str, strike: float, side: OptionSide
    ) -> OptionContract | None:
        for c in self.contracts:
            if (
                c.expiry == expiry
                and c.side == side
                and abs(c.strike - strike) < 1e-6
            ):
                return c
        return None


class OptionsClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = "https://paper-api.alpaca.markets",
        data_url: str = "https://data.alpaca.markets",
        timeout: float = 10.0,
    ) -> None:
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }
        self._base = base_url.rstrip("/")
        self._data = data_url.rstrip("/")
        self._timeout = timeout
        self._cache: dict[str, tuple[float, OptionChain]] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return all(v and "replace_me" not in v for v in self._headers.values())

    async def chain(
        self, underlying: str, *, max_contracts: int = 200
    ) -> OptionChain | None:
        if not self.enabled:
            return None
        key = underlying.upper()

        async with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.time():
                return cached[1]

        async with httpx.AsyncClient(
            timeout=self._timeout, headers=self._headers
        ) as client:
            try:
                contracts_task = client.get(
                    f"{self._base}/v2/options/contracts",
                    params={
                        "underlying_symbols": key,
                        "limit": _CONTRACTS_PAGE,
                        "status": "active",
                    },
                )
                snapshots_task = client.get(
                    f"{self._data}/v1beta1/options/snapshots/{key}",
                    params={"feed": "indicative", "limit": 1000},
                )
                contracts_resp, snapshots_resp = await asyncio.gather(
                    contracts_task, snapshots_task, return_exceptions=True
                )
            except Exception:
                logger.warning(
                    "options chain fetch failed for %s", key, exc_info=True
                )
                return None

        contracts_raw = _ok_json(contracts_resp, "contracts")
        snapshots_raw = _ok_json(snapshots_resp, "snapshots")

        if not contracts_raw:
            return None

        snaps = snapshots_raw.get("snapshots") if snapshots_raw else {}
        rows = contracts_raw.get("option_contracts") or contracts_raw.get(
            "contracts", []
        )

        contracts: list[OptionContract] = []
        for row in rows[:max_contracts * 8]:  # pre-cap before greek merge
            c = _build_contract(key, row, snaps or {})
            if c is not None:
                contracts.append(c)
        contracts.sort(key=lambda x: (x.expiry, x.side.value, x.strike))
        contracts = contracts[:max_contracts * 4]  # 4x buffer for both sides / expiries

        chain = OptionChain(
            underlying=key,
            contracts=tuple(contracts),
            fetched_at=datetime.now(UTC),
        )
        async with self._lock:
            self._cache[key] = (time.time() + _CACHE_TTL_SEC, chain)
        return chain


def _ok_json(resp: Any, label: str) -> dict[str, Any] | None:
    if isinstance(resp, Exception):
        logger.warning("options %s request raised: %s", label, resp)
        return None
    try:
        resp.raise_for_status()
        return resp.json() or {}
    except Exception as exc:
        logger.warning("options %s response failed: %s", label, exc)
        return None


def _build_contract(
    underlying: str, row: dict[str, Any], snaps: dict[str, Any]
) -> OptionContract | None:
    symbol = row.get("symbol")
    side_raw = (row.get("type") or row.get("option_type") or "").lower()
    if not symbol or side_raw not in ("call", "put"):
        return None
    strike = _f(row.get("strike_price") or row.get("strike"))
    expiry = row.get("expiration_date") or row.get("expiration")
    if strike is None or not expiry:
        return None

    snap = snaps.get(symbol) or {}
    quote = snap.get("latestQuote") or {}
    trade = snap.get("latestTrade") or {}
    greeks = snap.get("greeks") or {}
    iv = _f(snap.get("impliedVolatility"))
    bid = _f(quote.get("bp"))
    ask = _f(quote.get("ap"))
    mid = (
        (bid + ask) / 2.0
        if bid is not None and ask is not None and bid > 0 and ask > 0
        else None
    )

    return OptionContract(
        symbol=str(symbol).upper(),
        underlying=underlying,
        side=OptionSide.CALL if side_raw == "call" else OptionSide.PUT,
        strike=float(strike),
        expiry=str(expiry),
        bid=bid,
        ask=ask,
        mid=mid,
        last=_f(trade.get("p")),
        implied_volatility=iv,
        delta=_f(greeks.get("delta")),
        gamma=_f(greeks.get("gamma")),
        theta=_f(greeks.get("theta")),
        vega=_f(greeks.get("vega")),
        open_interest=_i(row.get("open_interest")),
        volume=_i(row.get("close_volume") or row.get("volume")),
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


__all__ = ["OptionChain", "OptionContract", "OptionsClient"]
