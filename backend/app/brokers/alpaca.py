"""Alpaca broker adapter for US stocks/ETFs.

Uses the official `alpaca-py` SDK. Paper vs live is controlled by the
`paper` flag on TradingClient (which also picks the correct base URL).

alpaca-py is synchronous; we wrap blocking calls in `asyncio.to_thread`
so the scheduler's event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.risk import (
    Market,
    OptionProposal,
    Position,
    PositionSide,
    TradeAction,
    TradeProposal,
)

from .base import BracketFill, BrokerAdapter, OrderFill, OrderResult

if TYPE_CHECKING:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient

logger = logging.getLogger(__name__)


def _enum_str(val: Any) -> str:
    """Lowercase the value of an Alpaca SDK enum (or any value).

    alpaca-py returns fields like `order.status` as `Enum` members
    (not `StrEnum`), so `str(OrderStatus.FILLED)` is
    ``'OrderStatus.FILLED'`` — a naive `.lower()` never matches
    ``'filled'``. Prefer `.value` when present, falling back to the
    tail after the last dot.
    """
    if val is None:
        return ""
    raw = getattr(val, "value", None)
    if raw is None:
        raw = str(val)
    return str(raw).rsplit(".", 1)[-1].lower()


class AlpacaBroker(BrokerAdapter):
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        *,
        paper: bool = True,
        data_url: str = "https://data.alpaca.markets",
    ) -> None:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._paper = paper
        self._api_key = api_key
        self._api_secret = api_secret
        self._data_url = data_url.rstrip("/")
        self._trading: TradingClient = TradingClient(
            api_key=api_key, secret_key=api_secret, paper=paper
        )
        self._data: StockHistoricalDataClient = StockHistoricalDataClient(
            api_key=api_key, secret_key=api_secret
        )

    @property
    def market(self) -> Market:
        return Market.STOCKS

    @property
    def paper_mode(self) -> bool:
        return self._paper

    async def get_cash_balance(self) -> float:
        account = await asyncio.to_thread(self._trading.get_account)
        return float(account.cash)

    async def get_positions(self) -> list[Position]:
        raw = await asyncio.to_thread(self._trading.get_all_positions)
        out: list[Position] = []
        for p in raw:
            qty = float(p.qty)
            avg_entry = float(p.avg_entry_price)
            current_price = float(p.current_price) if p.current_price else avg_entry
            out.append(
                Position(
                    market=Market.STOCKS,
                    symbol=p.symbol,
                    size_usd=abs(qty * avg_entry),
                    entry_price=avg_entry,
                    current_price=current_price,
                    side=PositionSide.SHORT if qty < 0 else PositionSide.LONG,
                )
            )
        return out

    async def is_market_open(self) -> bool:
        clock = await asyncio.to_thread(self._trading.get_clock)
        return bool(clock.is_open)

    async def get_price(self, symbol: str) -> float:
        from alpaca.data.requests import StockLatestTradeRequest

        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        resp = await asyncio.to_thread(self._data.get_stock_latest_trade, req)
        return float(resp[symbol].price)

    async def place_order(self, proposal: TradeProposal) -> OrderResult:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import (
            MarketOrderRequest,
            StopLossRequest,
            TakeProfitRequest,
        )

        if proposal.action not in (TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT):
            return OrderResult(success=False, error=f"unsupported action {proposal.action}")

        is_long = proposal.action == TradeAction.OPEN_LONG
        side = OrderSide.BUY if is_long else OrderSide.SELL

        # Prefer broker-side BRACKET (entry + stop + target in one atomic
        # order) so a sudden gap fires the stop at Alpaca even if our 30s
        # monitor is behind. Requirements: whole-share qty (Alpaca rejects
        # notional brackets) AND both stop+target percentages on the
        # proposal. The risk engine injects defaults via with_defaults,
        # so both are almost always set by the time we get here. If any
        # condition fails we fall back to a notional market order — the
        # monitor still covers the stop, just with worse worst-case
        # latency on a fast gap.
        stop_pct = proposal.stop_loss_pct
        tp_pct = proposal.take_profit_pct
        if stop_pct and stop_pct > 0 and tp_pct and tp_pct > 0:
            try:
                price = await self.get_price(proposal.symbol)
                qty = int(proposal.size_usd / price) if price > 0 else 0
                if qty >= 1:
                    stop_sign = -1 if is_long else 1
                    tp_sign = 1 if is_long else -1
                    stop_price = round(price * (1 + stop_sign * stop_pct), 2)
                    target_price = round(price * (1 + tp_sign * tp_pct), 2)
                    bracket_req = MarketOrderRequest(
                        symbol=proposal.symbol,
                        qty=qty,
                        side=side,
                        time_in_force=TimeInForce.DAY,
                        order_class=OrderClass.BRACKET,
                        take_profit=TakeProfitRequest(limit_price=target_price),
                        stop_loss=StopLossRequest(stop_price=stop_price),
                    )
                    order = await asyncio.to_thread(
                        self._trading.submit_order, bracket_req
                    )
                    fill_price = (
                        float(order.filled_avg_price)
                        if order.filled_avg_price
                        else None
                    )
                    return OrderResult(
                        success=True,
                        broker_order_id=str(order.id),
                        fill_price=fill_price,
                    )
            except Exception:
                logger.exception(
                    "alpaca bracket submit failed, falling back to notional"
                )

        try:
            req = MarketOrderRequest(
                symbol=proposal.symbol,
                notional=proposal.size_usd,
                side=side,
                time_in_force=TimeInForce.DAY,
            )
            order = await asyncio.to_thread(self._trading.submit_order, req)
            fill_price = float(order.filled_avg_price) if order.filled_avg_price else None
            return OrderResult(
                success=True,
                broker_order_id=str(order.id),
                fill_price=fill_price,
            )
        except Exception as exc:
            logger.exception("alpaca place_order failed")
            return OrderResult(success=False, error=str(exc))

    async def place_multileg_order(self, proposal: TradeProposal) -> OrderResult:
        """Submit a defined-risk option structure.

        One leg       → simple options order (buy_to_open / sell_to_open).
        Two or more   → OrderClass.MLEG with per-leg PositionIntent.

        Uses a marketable limit price derived from the net debit/credit
        reported by the builder — MLEG on Alpaca requires LIMIT orders,
        market MLEG is not accepted. Single-leg longs can go market.
        """
        if proposal.option is None:
            return OrderResult(success=False, error="place_multileg_order requires proposal.option")
        opt = proposal.option

        from alpaca.trading.enums import (
            OrderClass,
            OrderSide,
            OrderType,
            PositionIntent,
            TimeInForce,
        )
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            OptionLegRequest,
        )

        try:
            if len(opt.legs) == 1:
                leg = opt.legs[0]
                is_long = leg.ratio > 0
                side = OrderSide.BUY if is_long else OrderSide.SELL
                intent = (
                    PositionIntent.BUY_TO_OPEN if is_long else PositionIntent.SELL_TO_OPEN
                )
                qty = abs(leg.ratio)
                # Long single-leg: market is fine. Short single-leg is off-
                # menu per tier rules, but guard anyway by using limit.
                if is_long:
                    req: Any = MarketOrderRequest(
                        symbol=leg.option_symbol,
                        qty=qty,
                        side=side,
                        type=OrderType.MARKET,
                        time_in_force=TimeInForce.DAY,
                        position_intent=intent,
                    )
                else:
                    if leg.mid_price is None:
                        return OrderResult(
                            success=False,
                            error=f"no mid price for {leg.option_symbol}; cannot set limit",
                        )
                    req = LimitOrderRequest(
                        symbol=leg.option_symbol,
                        qty=qty,
                        side=side,
                        type=OrderType.LIMIT,
                        time_in_force=TimeInForce.DAY,
                        position_intent=intent,
                        limit_price=round(leg.mid_price * 0.95, 2),
                    )
            else:
                legs_req = [
                    OptionLegRequest(
                        symbol=l.option_symbol,
                        ratio_qty=abs(l.ratio),
                        side=OrderSide.BUY if l.ratio > 0 else OrderSide.SELL,
                        position_intent=(
                            PositionIntent.BUY_TO_OPEN
                            if l.ratio > 0
                            else PositionIntent.SELL_TO_OPEN
                        ),
                    )
                    for l in opt.legs
                ]
                limit_price = _combo_limit_price(opt, closing=False)
                if limit_price is None:
                    return OrderResult(
                        success=False,
                        error="cannot derive combo limit price (missing mid on one or more legs)",
                    )
                req = LimitOrderRequest(
                    qty=1,
                    order_class=OrderClass.MLEG,
                    type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    legs=legs_req,
                    limit_price=limit_price,
                )

            order = await asyncio.to_thread(self._trading.submit_order, req)
            fill_price = float(order.filled_avg_price) if order.filled_avg_price else None
            return OrderResult(
                success=True,
                broker_order_id=str(order.id),
                fill_price=fill_price,
            )
        except Exception as exc:
            logger.exception("alpaca place_multileg_order failed")
            return OrderResult(success=False, error=str(exc))

    async def close_option_position(self, option_json: dict) -> OrderResult:
        """Submit inverse-intent legs to flatten a stored option structure.

        Long legs from the open → sell_to_close on close; shorts → buy_to_close.
        Single leg uses a simple option order; 2+ legs use MLEG.
        """
        legs_spec = option_json.get("legs") or []
        if not legs_spec:
            return OrderResult(success=False, error="option_json has no legs")

        from alpaca.trading.enums import (
            OrderClass,
            OrderSide,
            OrderType,
            PositionIntent,
            TimeInForce,
        )
        from alpaca.trading.requests import (
            LimitOrderRequest,
            MarketOrderRequest,
            OptionLegRequest,
        )

        try:
            if len(legs_spec) == 1:
                leg = legs_spec[0]
                ratio = int(leg.get("ratio") or 0)
                was_long = ratio > 0
                # Invert intent: long → sell_to_close, short → buy_to_close.
                side = OrderSide.SELL if was_long else OrderSide.BUY
                intent = (
                    PositionIntent.SELL_TO_CLOSE
                    if was_long
                    else PositionIntent.BUY_TO_CLOSE
                )
                qty = abs(ratio)
                if was_long:
                    req: Any = MarketOrderRequest(
                        symbol=leg["option_symbol"],
                        qty=qty,
                        side=side,
                        type=OrderType.MARKET,
                        time_in_force=TimeInForce.DAY,
                        position_intent=intent,
                    )
                else:
                    mid = leg.get("mid_price")
                    if mid is None:
                        return OrderResult(
                            success=False,
                            error=f"no mid price stored for close of {leg['option_symbol']}",
                        )
                    req = LimitOrderRequest(
                        symbol=leg["option_symbol"],
                        qty=qty,
                        side=side,
                        type=OrderType.LIMIT,
                        time_in_force=TimeInForce.DAY,
                        position_intent=intent,
                        limit_price=round(float(mid) * 1.05, 2),
                    )
            else:
                legs_req = []
                for leg in legs_spec:
                    ratio = int(leg.get("ratio") or 0)
                    was_long = ratio > 0
                    legs_req.append(
                        OptionLegRequest(
                            symbol=leg["option_symbol"],
                            ratio_qty=abs(ratio),
                            side=OrderSide.SELL if was_long else OrderSide.BUY,
                            position_intent=(
                                PositionIntent.SELL_TO_CLOSE
                                if was_long
                                else PositionIntent.BUY_TO_CLOSE
                            ),
                        )
                    )
                limit_price = _combo_limit_price_from_json(option_json, closing=True)
                if limit_price is None:
                    return OrderResult(
                        success=False,
                        error="cannot derive combo close price (missing mid on one or more legs)",
                    )
                req = LimitOrderRequest(
                    qty=1,
                    order_class=OrderClass.MLEG,
                    type=OrderType.LIMIT,
                    time_in_force=TimeInForce.DAY,
                    legs=legs_req,
                    limit_price=limit_price,
                )

            order = await asyncio.to_thread(self._trading.submit_order, req)
            fill_price = float(order.filled_avg_price) if order.filled_avg_price else None
            return OrderResult(
                success=True,
                broker_order_id=str(order.id),
                fill_price=fill_price,
            )
        except Exception as exc:
            logger.exception("alpaca close_option_position failed")
            return OrderResult(success=False, error=str(exc))

    async def close_position(self, symbol: str) -> OrderResult:
        # Bracket orders pin the position's shares as `held_for_orders` via
        # their child stop + take-profit legs; close_position then rejects
        # with "insufficient qty available". Cancel any open orders on the
        # symbol first so the shares are free to close.
        await self._cancel_open_orders_for_symbol(symbol)
        try:
            order = await asyncio.to_thread(
                self._trading.close_position, symbol_or_asset_id=symbol
            )
            fill_price = float(order.filled_avg_price) if order.filled_avg_price else None
            return OrderResult(
                success=True,
                broker_order_id=str(order.id),
                fill_price=fill_price,
            )
        except Exception as exc:
            logger.exception("alpaca close_position failed")
            return OrderResult(success=False, error=str(exc))

    async def _cancel_open_orders_for_symbol(self, symbol: str) -> None:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        try:
            req = GetOrdersRequest(
                status=QueryOrderStatus.OPEN, symbols=[symbol], nested=True
            )
            orders = await asyncio.to_thread(self._trading.get_orders, filter=req)
        except Exception:
            logger.exception("alpaca get_orders failed for %s", symbol)
            return

        for o in orders or []:
            oid = getattr(o, "id", None)
            if not oid:
                continue
            try:
                await asyncio.to_thread(self._trading.cancel_order_by_id, oid)
            except Exception:
                logger.warning(
                    "alpaca cancel_order_by_id failed for %s (symbol %s)",
                    oid,
                    symbol,
                    exc_info=True,
                )

    async def cancel_all_orders(self) -> int:
        try:
            cancelled = await asyncio.to_thread(self._trading.cancel_orders)
            return len(cancelled) if cancelled is not None else 0
        except Exception:
            logger.exception("alpaca cancel_all_orders failed")
            return 0

    async def get_order_fill(self, order_id: str) -> OrderFill | None:
        """Map an Alpaca parent order to the reconciler's status contract.

        ``status`` coerces Alpaca's statuses into {pending, filled, canceled,
        rejected} — anything still in-flight is "pending" so the reconciler
        keeps polling. A terminal non-fill ("expired", "done_for_day",
        "canceled", "rejected") becomes CANCELED locally so the row doesn't
        linger.
        """
        try:
            order = await asyncio.to_thread(
                self._trading.get_order_by_id, order_id
            )
        except Exception:
            logger.exception("alpaca get_order_by_id failed for %s", order_id)
            return None

        raw_status = _enum_str(getattr(order, "status", None))
        fill_price = getattr(order, "filled_avg_price", None)
        if raw_status == "filled" and fill_price is not None:
            return OrderFill(status="filled", fill_price=float(fill_price))
        if raw_status in {"canceled", "cancelled", "expired", "done_for_day"}:
            return OrderFill(status="canceled")
        if raw_status == "rejected":
            return OrderFill(status="rejected")
        return OrderFill(status="pending")

    async def get_bracket_fill(self, order_id: str) -> BracketFill | None:
        """Inspect a bracket parent's child legs for stop/take-profit fills.

        Alpaca brackets attach the stop and take-profit as child orders
        under the parent. When one fires, the sibling is canceled and
        the filled child carries `status='filled'` + `filled_avg_price`.
        We classify the trigger by `order_type`: STOP* = stop-loss,
        LIMIT* = take-profit. Returns None if no child has filled yet.
        """
        try:
            order = await asyncio.to_thread(
                self._trading.get_order_by_id, order_id
            )
        except Exception:
            logger.exception("alpaca get_order_by_id failed for %s", order_id)
            return None

        legs = getattr(order, "legs", None) or []
        for leg in legs:
            status = _enum_str(getattr(leg, "status", None))
            if status != "filled":
                continue
            fill_price = getattr(leg, "filled_avg_price", None)
            if fill_price is None:
                continue
            order_type = _enum_str(getattr(leg, "order_type", None))
            order_type_alt = _enum_str(getattr(leg, "type", None))
            is_stop = "stop" in order_type or "stop" in order_type_alt
            trigger = "STOP" if is_stop else "TAKE_PROFIT"
            return BracketFill(
                fill_price=float(fill_price),
                trigger=trigger,
                child_order_id=str(getattr(leg, "id", "") or ""),
            )
        return None

    async def get_option_mark(self, option_json: dict) -> float | None:
        """Fetch current per-contract net premium for a stored combo.

        Hits Alpaca's options snapshots endpoint for every OCC symbol in
        the combo and sums `sign(ratio) * mid * abs(ratio)`. Returns None
        if any leg's mid is unavailable, so the monitor leaves the trade
        alone rather than tripping on a degraded quote.
        """
        legs = option_json.get("legs") or []
        if not legs:
            return None
        symbols = [leg.get("option_symbol") for leg in legs if leg.get("option_symbol")]
        if not symbols:
            return None

        import httpx

        headers = {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
        }
        url = f"{self._data_url}/v1beta1/options/snapshots"
        params = {"symbols": ",".join(symbols), "feed": "indicative"}
        try:
            async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                snaps = (resp.json() or {}).get("snapshots") or {}
        except Exception:
            logger.warning(
                "options snapshot fetch failed for %s", symbols, exc_info=True
            )
            return None

        signed_sum = 0.0
        for leg in legs:
            sym = leg.get("option_symbol")
            ratio = int(leg.get("ratio") or 0)
            if not sym or ratio == 0:
                return None
            snap = snaps.get(sym) or {}
            quote = snap.get("latestQuote") or {}
            bid = quote.get("bp")
            ask = quote.get("ap")
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                return None
            mid = (float(bid) + float(ask)) / 2.0
            sign = 1.0 if ratio > 0 else -1.0
            signed_sum += sign * mid * abs(ratio)
        return signed_sum


def _combo_limit_price(opt: OptionProposal, *, closing: bool) -> float | None:
    """Compute a per-contract limit price for the whole combo.

    Alpaca's MLEG limit is a *per-contract net* quote — positive = debit
    paid, negative = credit received. We derive it from the leg mids:
    `sum(mid * sign(ratio))` over all legs. A small buffer is applied so
    marketable orders actually fill:
      open debit  → +3% padding    open credit  → −3% padding
      close of a debit open → credit side (negative), we pad toward 0
    """
    signed_sum = 0.0
    for leg in opt.legs:
        if leg.mid_price is None:
            return None
        # Per-contract net: sign of ratio, magnitude scaled by |ratio|/1.
        sign = 1.0 if leg.ratio > 0 else -1.0
        signed_sum += sign * float(leg.mid_price) * abs(leg.ratio)

    if closing:
        signed_sum = -signed_sum
    # Pad 5% in the unfavorable direction to make it marketable.
    pad = 0.05
    if signed_sum >= 0:
        limit = signed_sum * (1.0 + pad)
    else:
        limit = signed_sum * (1.0 - pad)
    return round(limit, 2)


def _combo_limit_price_from_json(option_json: dict, *, closing: bool) -> float | None:
    legs = option_json.get("legs") or []
    signed_sum = 0.0
    for leg in legs:
        mid = leg.get("mid_price")
        if mid is None:
            return None
        ratio = int(leg.get("ratio") or 0)
        sign = 1.0 if ratio > 0 else -1.0
        signed_sum += sign * float(mid) * abs(ratio)
    if closing:
        signed_sum = -signed_sum
    pad = 0.05
    if signed_sum >= 0:
        limit = signed_sum * (1.0 + pad)
    else:
        limit = signed_sum * (1.0 - pad)
    return round(limit, 2)
