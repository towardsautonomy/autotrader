"""Regression: alpaca-py returns status/order_type as plain Enum members
(not StrEnum), so ``str(OrderStatus.FILLED)`` is ``'OrderStatus.FILLED'``.
A naive ``.lower() == "filled"`` never matches and stuck every filled
order in PENDING forever (see incident 2026-04-20: 3 live short positions
with no local OPEN row, analytics empty). The broker now normalizes via
``_enum_str`` — this test locks that in."""

from __future__ import annotations

from enum import Enum

from app.brokers.alpaca import _enum_str


class _AlpacaLikeEnum(Enum):
    FILLED = "filled"
    CANCELED = "canceled"
    STOP_LIMIT = "stop_limit"


def test_plain_enum_member_returns_value_not_qualname():
    assert _enum_str(_AlpacaLikeEnum.FILLED) == "filled"
    assert _enum_str(_AlpacaLikeEnum.CANCELED) == "canceled"
    assert _enum_str(_AlpacaLikeEnum.STOP_LIMIT) == "stop_limit"


def test_plain_string_is_lowercased():
    assert _enum_str("FILLED") == "filled"
    assert _enum_str("filled") == "filled"


def test_none_becomes_empty_string():
    assert _enum_str(None) == ""


def test_qualname_style_string_still_tail_matches():
    """Guards against the original bug: if upstream ever hands us
    ``'OrderStatus.FILLED'`` directly (e.g. from a ``repr``), the tail
    after the last dot should still classify correctly."""
    assert _enum_str("OrderStatus.FILLED") == "filled"
