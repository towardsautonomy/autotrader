"""Polymarket CLOB broker adapter.

Uses `py-clob-client`. Trading is on-chain via Polygon, so every order
requires a wallet private key and (after one-time derivation) CLOB API
credentials.

In Polymarket, a "symbol" is a `token_id` — a specific outcome
(YES or NO) of a specific market condition. The AI layer is responsible
for turning human-readable events into token_ids via the
`CLOBClient.get_markets()` feed before proposing a trade.

**This adapter is labeled experimental.** The interface is stable; the
implementation has only been smoke-tested against Amoy testnet. Verify
end-to-end in paper mode before any mainnet use.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from app.risk import Market, Position, TradeAction, TradeProposal

from .base import BrokerAdapter, OrderResult

if TYPE_CHECKING:
    from py_clob_client.client import ClobClient

logger = logging.getLogger(__name__)


class PolymarketBroker(BrokerAdapter):
    def __init__(
        self,
        *,
        private_key: str,
        clob_api_key: str,
        clob_secret: str,
        clob_passphrase: str,
        host: str,
        chain_id: int,
        paper: bool,
    ) -> None:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        self._paper = paper
        self._host = host
        self._chain_id = chain_id

        self._client: ClobClient = ClobClient(
            host=host, chain_id=chain_id, key=private_key
        )

        if clob_api_key and clob_secret and clob_passphrase:
            self._client.set_api_creds(
                ApiCreds(
                    api_key=clob_api_key,
                    api_secret=clob_secret,
                    api_passphrase=clob_passphrase,
                )
            )
        else:
            logger.warning(
                "Polymarket CLOB creds missing — derive via "
                "client.create_or_derive_api_creds() and paste into .env"
            )

    @property
    def market(self) -> Market:
        return Market.POLYMARKET

    @property
    def paper_mode(self) -> bool:
        return self._paper

    async def get_cash_balance(self) -> float:
        """USDC balance available on the wallet, as reported by the CLOB."""

        def _fetch():
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self._client.get_balance_allowance(params)
            # Balances are returned as stringified 10^6 units (USDC has 6 decimals)
            return float(result.get("balance", 0)) / 1_000_000

        try:
            return await asyncio.to_thread(_fetch)
        except Exception:
            logger.exception("polymarket get_cash_balance failed")
            return 0.0

    async def get_positions(self) -> list[Position]:
        """Current outcome-token holdings.

        The CLOB client doesn't expose a positions endpoint, but Polymarket
        publishes one on ``data-api.polymarket.com/positions?user=<address>``.
        Each row reports ``size`` (shares), ``avgPrice`` (entry), and
        ``curPrice`` (mark) — enough to construct risk-engine Positions.

        Degrades silently to an empty list on any failure: the monitor
        and scheduler already treat positions as "what we think we have
        via DB" for reconciliation, so a transient data-api outage just
        means no broker-side cross-check this tick.
        """
        import httpx

        def _address() -> str | None:
            try:
                return self._client.get_address()
            except Exception:
                return None

        address = await asyncio.to_thread(_address)
        if not address:
            logger.warning("polymarket get_positions: no wallet address")
            return []

        url = "https://data-api.polymarket.com/positions"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url, params={"user": address})
                resp.raise_for_status()
                rows = resp.json() or []
        except Exception:
            logger.warning(
                "polymarket get_positions fetch failed", exc_info=True
            )
            return []

        positions: list[Position] = []
        for row in rows or []:
            token_id = row.get("asset") or row.get("tokenId")
            size = row.get("size")
            avg_price = row.get("avgPrice") or row.get("averagePrice")
            cur_price = row.get("curPrice") or row.get("currentPrice") or avg_price
            if not token_id or size is None or avg_price is None:
                continue
            try:
                shares = float(size)
                entry = float(avg_price)
                current = float(cur_price) if cur_price is not None else entry
            except (TypeError, ValueError):
                continue
            if shares <= 0 or entry <= 0:
                continue
            positions.append(
                Position(
                    market=Market.POLYMARKET,
                    symbol=str(token_id),
                    size_usd=shares * entry,
                    entry_price=entry,
                    current_price=current,
                )
            )
        return positions

    async def is_market_open(self) -> bool:
        return True

    async def get_price(self, symbol: str) -> float:
        """`symbol` here is a CLOB token_id. Returns the current mid-market
        price in USDC (a value between 0 and 1)."""

        def _fetch():
            book = self._client.get_order_book(symbol)
            # book has .bids / .asks; best bid/ask are first entries
            bids = getattr(book, "bids", []) or []
            asks = getattr(book, "asks", []) or []
            if bids and asks:
                best_bid = float(bids[0].price)
                best_ask = float(asks[0].price)
                return (best_bid + best_ask) / 2
            if bids:
                return float(bids[0].price)
            if asks:
                return float(asks[0].price)
            return 0.0

        try:
            return await asyncio.to_thread(_fetch)
        except Exception:
            logger.exception("polymarket get_price failed for %s", symbol)
            return 0.0

    async def place_order(self, proposal: TradeProposal) -> OrderResult:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        if proposal.action not in (TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT):
            return OrderResult(success=False, error=f"unsupported action {proposal.action}")

        side = BUY if proposal.action == TradeAction.OPEN_LONG else SELL

        def _submit():
            args = MarketOrderArgs(
                token_id=proposal.symbol,
                amount=proposal.size_usd,
                side=side,
            )
            signed = self._client.create_market_order(args)
            resp = self._client.post_order(signed, OrderType.FOK)
            return resp

        try:
            resp = await asyncio.to_thread(_submit)
            order_id = (
                resp.get("orderID")
                or resp.get("order_id")
                or resp.get("id")
                or ""
            )
            # Polymarket FOK responses usually don't include a fill price; the
            # monitor will fetch current price when calculating PnL.
            return OrderResult(
                success=bool(resp.get("success", order_id != "")),
                broker_order_id=str(order_id) if order_id else None,
                error=resp.get("errorMsg") or resp.get("error"),
            )
        except Exception as exc:
            logger.exception("polymarket place_order failed")
            return OrderResult(success=False, error=str(exc))

    async def close_position(self, symbol: str) -> OrderResult:
        """Sell the full holding of ``symbol`` (a CLOB token_id).

        Looks up current shares via ``get_positions``, grabs the best bid
        from the order book, and submits a SELL limit at ``bid * 0.95``
        via GTC. A marketable limit (vs. MKT) avoids thin-book slippage
        blowouts — outcome tokens can have sparse order books.
        """
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import SELL

        positions = await self.get_positions()
        match = next((p for p in positions if p.symbol == symbol), None)
        if match is None:
            return OrderResult(
                success=False,
                error=f"no open Polymarket position for token {symbol}",
            )
        shares = match.size_usd / match.entry_price if match.entry_price > 0 else 0.0
        if shares <= 0:
            return OrderResult(
                success=False, error=f"zero shares held for token {symbol}"
            )

        def _best_bid() -> float | None:
            try:
                book = self._client.get_order_book(symbol)
                bids = getattr(book, "bids", []) or []
                if bids:
                    return float(bids[0].price)
            except Exception:
                logger.exception("polymarket order book fetch failed")
            return None

        bid = await asyncio.to_thread(_best_bid)
        if bid is None or bid <= 0:
            return OrderResult(
                success=False,
                error=f"no bid available to close token {symbol}",
            )
        # Pad 5% below the best bid so a marketable limit actually fills
        # without handing free money to market makers on a thin book.
        limit_price = round(bid * 0.95, 4)

        def _submit():
            args = OrderArgs(
                token_id=symbol,
                price=limit_price,
                size=shares,
                side=SELL,
            )
            signed = self._client.create_order(args)
            return self._client.post_order(signed, OrderType.GTC)

        try:
            resp = await asyncio.to_thread(_submit)
            order_id = (
                resp.get("orderID")
                or resp.get("order_id")
                or resp.get("id")
                or ""
            )
            return OrderResult(
                success=bool(resp.get("success", order_id != "")),
                broker_order_id=str(order_id) if order_id else None,
                error=resp.get("errorMsg") or resp.get("error"),
            )
        except Exception as exc:
            logger.exception("polymarket close_position failed")
            return OrderResult(success=False, error=str(exc))
