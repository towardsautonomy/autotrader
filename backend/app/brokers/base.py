from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.risk import Market, Position, TradeProposal


@dataclass(frozen=True, slots=True)
class OrderResult:
    success: bool
    broker_order_id: str | None = None
    fill_price: float | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class OrderFill:
    """Status of a previously-submitted parent order.

    Polled by the pending-order reconciler to promote PENDING Trade rows
    to OPEN (on fill) or CANCELED (on broker rejection/cancel). ``status``
    is one of ``"pending"``, ``"filled"``, ``"canceled"``, ``"rejected"``.
    ``fill_price`` is only meaningful when ``status == "filled"``.
    """

    status: str
    fill_price: float | None = None


@dataclass(frozen=True, slots=True)
class BracketFill:
    """Outcome of reconciling a broker-side bracket order.

    When Alpaca fires the stop or take-profit child leg of a bracket,
    our DB row stays OPEN until the reconciler closes it. This struct
    carries the fill price + which leg triggered so the reconciler can
    mark the Trade CLOSED with the right pnl and a meaningful event.
    """

    fill_price: float
    trigger: str  # "STOP" or "TAKE_PROFIT"
    child_order_id: str


class BrokerAdapter(ABC):
    """One adapter per market (stocks, polymarket). Paper vs live is a
    configuration detail of the adapter itself; callers never switch
    adapters when flipping modes."""

    @property
    @abstractmethod
    def market(self) -> Market: ...

    @property
    @abstractmethod
    def paper_mode(self) -> bool: ...

    @abstractmethod
    async def get_cash_balance(self) -> float: ...

    @abstractmethod
    async def get_positions(self) -> list[Position]: ...

    @abstractmethod
    async def is_market_open(self) -> bool: ...

    @abstractmethod
    async def get_price(self, symbol: str) -> float: ...

    @abstractmethod
    async def place_order(self, proposal: TradeProposal) -> OrderResult: ...

    @abstractmethod
    async def close_position(self, symbol: str) -> OrderResult: ...

    async def place_multileg_order(self, proposal: TradeProposal) -> OrderResult:
        """Submit a multi-leg option order. Default implementation refuses —
        brokers that support options override.

        Kept as a method rather than forcing subclasses to implement so
        non-options brokers (Polymarket, Null) don't need a stub."""
        return OrderResult(
            success=False,
            error=f"multi-leg order submission not implemented for {self.__class__.__name__}",
        )

    async def close_option_position(self, option_json: dict) -> OrderResult:
        """Close an open option position by submitting inverse-intent legs.

        `option_json` is the serialized OptionProposal stored on the Trade
        row at open time. Default implementation refuses — brokers that
        support options override."""
        return OrderResult(
            success=False,
            error=f"option close not implemented for {self.__class__.__name__}",
        )

    async def cancel_all_orders(self) -> int:
        """Cancel every open/resting order at the broker. Returns the count
        cancelled. Default: no-op (for brokers without a listable order
        book)."""
        return 0

    async def get_order_fill(self, order_id: str) -> OrderFill | None:
        """Return the parent-order fill status for ``order_id``.

        Used by the pending-order reconciler to promote a PENDING Trade
        row once the broker actually fills it, or to cancel it if the
        broker rejects the order. Default returns None so brokers that
        don't need the promotion path (e.g. Polymarket, which fills
        synchronously) don't need a stub.
        """
        return None

    async def get_bracket_fill(self, order_id: str) -> BracketFill | None:
        """Return fill info if a bracket child leg has filled, else None.

        Default returns None so brokers without broker-side brackets
        don't need a stub. Brokers with native brackets override."""
        return None

    async def get_option_mark(self, option_json: dict) -> float | None:
        """Current per-contract net premium (signed) for an option combo.

        Used by the runtime monitor to mark-to-market open option trades
        and fire stop/take-profit. Sign convention matches how open fills
        are stored: positive = debit (long paid), negative = credit
        (short received). Default: None — brokers without options data
        don't need a stub."""
        return None
