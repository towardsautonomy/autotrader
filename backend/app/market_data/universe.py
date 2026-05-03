"""Alpaca tradable-asset universe — cached daily.

The universe is the haystack we screen. Keep it to names the risk engine
could legally trade (active, tradable, fractionable, US equity). We also
record which names carry an `options_enabled` attribute so Phase B4 can
filter to optionable tickers without re-hitting this endpoint.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CACHE_TTL_SEC = 24 * 60 * 60  # daily refresh is plenty; asset list doesn't churn
_MAJOR_EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}


@dataclass(frozen=True, slots=True)
class UniverseAsset:
    symbol: str
    name: str
    exchange: str
    optionable: bool
    shortable: bool
    easy_to_borrow: bool
    fractionable: bool


class UniverseClient:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        base_url: str = "https://paper-api.alpaca.markets",
        timeout: float = 15.0,
    ) -> None:
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": api_secret,
        }
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._cache: tuple[float, list[UniverseAsset]] | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return all(v and "replace_me" not in v for v in self._headers.values())

    async def fetch(self) -> list[UniverseAsset]:
        if not self.enabled:
            return []
        async with self._lock:
            if self._cache and self._cache[0] > time.time():
                return self._cache[1]

        async with httpx.AsyncClient(
            timeout=self._timeout, headers=self._headers
        ) as client:
            try:
                resp = await client.get(
                    f"{self._base}/v2/assets",
                    params={"status": "active", "asset_class": "us_equity"},
                )
                resp.raise_for_status()
                rows: list[dict[str, Any]] = resp.json()
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    "alpaca universe fetch HTTP %s: %s",
                    exc.response.status_code,
                    exc.response.text[:200],
                )
                return []
            except Exception as exc:
                logger.warning("alpaca universe fetch failed: %s", exc)
                return []

        out: list[UniverseAsset] = []
        for row in rows:
            if not row.get("tradable") or row.get("status") != "active":
                continue
            if not row.get("fractionable"):
                # Fractionable-only keeps the usd-sized order path working and
                # incidentally drops most illiquid OTC-style listings.
                continue
            exchange = str(row.get("exchange") or "").upper()
            if exchange and exchange not in _MAJOR_EXCHANGES:
                continue
            attrs = row.get("attributes") or []
            optionable = "options_enabled" in attrs or bool(row.get("options_enabled"))
            out.append(
                UniverseAsset(
                    symbol=str(row.get("symbol", "")).upper(),
                    name=str(row.get("name") or ""),
                    exchange=exchange,
                    optionable=optionable,
                    shortable=bool(row.get("shortable")),
                    easy_to_borrow=bool(row.get("easy_to_borrow")),
                    fractionable=bool(row.get("fractionable")),
                )
            )

        logger.info("alpaca universe loaded: %d tradable assets", len(out))
        async with self._lock:
            self._cache = (time.time() + _CACHE_TTL_SEC, out)
        return out


__all__ = ["UniverseAsset", "UniverseClient"]
