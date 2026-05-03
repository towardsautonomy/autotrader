"""Polymarket broker get_positions / close_position.

These exercise the CLOB-less paths: positions come from the Polymarket
data API (mocked via httpx), and close submits a SELL limit through the
CLOB client (mocked in-memory). Goal is to pin the contract so a later
upgrade to live endpoints doesn't silently change sizing or order
routing.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.brokers.base import OrderResult
from app.brokers.polymarket import PolymarketBroker


class _FakeBook:
    def __init__(self, bids: list[float]):
        self.bids = [type("Level", (), {"price": str(p)})() for p in bids]
        self.asks = []


class _FakeClobClient:
    def __init__(self, *, address: str = "0xwallet", bids: list[float] | None = None):
        self._address = address
        self._bids = bids if bids is not None else [0.60, 0.59]
        self.posted_orders: list[tuple[Any, Any]] = []

    def get_address(self) -> str:
        return self._address

    def get_order_book(self, token_id: str):
        return _FakeBook(self._bids)

    def create_order(self, args):
        return ("signed", args)

    def post_order(self, signed, order_type):
        self.posted_orders.append((signed, order_type))
        return {"success": True, "orderID": "pm-close-1"}


def _make_broker_with(fake: _FakeClobClient) -> PolymarketBroker:
    # Bypass __init__ so we don't need real creds or SDK objects.
    broker = PolymarketBroker.__new__(PolymarketBroker)
    broker._paper = True
    broker._host = "https://clob.polymarket.com"
    broker._chain_id = 137
    broker._client = fake
    return broker


@pytest.mark.asyncio
async def test_get_positions_parses_data_api(monkeypatch):
    fake = _FakeClobClient()
    broker = _make_broker_with(fake)

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("user") == "0xwallet"
        return httpx.Response(
            200,
            json=[
                {"asset": "token-1", "size": "100", "avgPrice": "0.40", "curPrice": "0.55"},
                {"asset": "token-2", "size": "50", "avgPrice": "0.70", "curPrice": "0.68"},
                # Junk row that must be skipped without crashing.
                {"asset": "token-3", "size": "0", "avgPrice": "0.50"},
            ],
        )

    transport = httpx.MockTransport(_handler)
    orig = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)

    positions = await broker.get_positions()
    assert len(positions) == 2
    p1 = positions[0]
    assert p1.symbol == "token-1"
    assert p1.entry_price == pytest.approx(0.40)
    assert p1.current_price == pytest.approx(0.55)
    # size_usd = shares * entry = 100 * 0.40
    assert p1.size_usd == pytest.approx(40.0)


@pytest.mark.asyncio
async def test_get_positions_fails_open_on_api_error(monkeypatch):
    fake = _FakeClobClient()
    broker = _make_broker_with(fake)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(_handler)
    orig = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)
    # A transient data-api failure must degrade to [] rather than raising;
    # other code paths rely on get_positions to return something iterable.
    assert await broker.get_positions() == []


@pytest.mark.asyncio
async def test_close_position_submits_sell_limit(monkeypatch):
    fake = _FakeClobClient(bids=[0.80])
    broker = _make_broker_with(fake)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"asset": "token-1", "size": "100", "avgPrice": "0.40", "curPrice": "0.80"}
            ],
        )

    transport = httpx.MockTransport(_handler)
    orig = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)

    result = await broker.close_position("token-1")
    assert result.success is True
    assert result.broker_order_id == "pm-close-1"
    # One SELL order submitted — verify shares + marketable limit were set.
    assert len(fake.posted_orders) == 1
    signed, _ = fake.posted_orders[0]
    _, args = signed
    assert args.token_id == "token-1"
    assert args.size == pytest.approx(100.0)
    # limit = bid * 0.95 = 0.80 * 0.95 = 0.76
    assert args.price == pytest.approx(0.76)


@pytest.mark.asyncio
async def test_close_position_rejects_when_no_holding(monkeypatch):
    fake = _FakeClobClient()
    broker = _make_broker_with(fake)

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(_handler)
    orig = httpx.AsyncClient

    def _client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client_factory)

    result: OrderResult = await broker.close_position("token-x")
    assert result.success is False
    assert result.error is not None and "no open" in result.error.lower()
    assert fake.posted_orders == []
