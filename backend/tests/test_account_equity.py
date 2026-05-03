"""Regression: total_equity must treat shorts as liabilities, not assets.

Pre-fix, ``total_equity = cash + Σ(size_usd + unrealized_pnl)`` double-
counted short proceeds: cash already reflects the short sale, so the
position's market value is a liability, and its contribution should be
``-size_usd + unrealized_pnl`` (Position.unrealized_pnl is already signed
correctly for side). The bug surfaced 2026-04-20 as ~$1,907 of phantom
equity against Alpaca's portfolio_value while the account held two
short positions summing to $953 notional (error ≈ 2 × shorts size).
"""

from __future__ import annotations

from app.risk import AccountSnapshot, Market, Position, PositionSide


def _pos(symbol: str, size: float, entry: float, current: float, side: PositionSide) -> Position:
    return Position(
        market=Market.STOCKS,
        symbol=symbol,
        size_usd=size,
        entry_price=entry,
        current_price=current,
        side=side,
    )


def test_total_equity_matches_alpaca_formula_with_shorts():
    # Mirrors the live snapshot from the 2026-04-20 incident:
    # Alpaca portfolio_value 99,967.04 when cash=100,939.13 + two underwater
    # shorts (ASTS -6 @ 78.35 → 78.66, QXO -21 @ 23.03 → 23.835).
    snap = AccountSnapshot(
        cash_balance=100_939.13,
        positions=(
            _pos("ASTS", 470.10, 78.35, 78.66, PositionSide.SHORT),
            _pos("QXO", 483.63, 23.03, 23.835, PositionSide.SHORT),
        ),
        day_realized_pnl=0.0,
        cumulative_pnl=0.0,
        daily_trade_count=0,
        trading_enabled=True,
    )
    # Alpaca: equity = cash + long_mv + short_mv = 100,939.13 + 0 + (-972.09)
    assert abs(snap.total_equity - 99_967.04) < 0.5


def test_total_equity_longs_unchanged():
    snap = AccountSnapshot(
        cash_balance=50_000.0,
        positions=(
            _pos("AAPL", 10_000.0, 100.0, 110.0, PositionSide.LONG),
        ),
        day_realized_pnl=0.0,
        cumulative_pnl=0.0,
        daily_trade_count=0,
        trading_enabled=True,
    )
    # cash + size + unrealized = 50k + 10k + 1k
    assert abs(snap.total_equity - 61_000.0) < 0.01


def test_total_equity_mixed_long_short():
    snap = AccountSnapshot(
        cash_balance=20_000.0,
        positions=(
            _pos("AAPL", 5_000.0, 100.0, 110.0, PositionSide.LONG),
            _pos("TSLA", 2_000.0, 200.0, 190.0, PositionSide.SHORT),
        ),
        day_realized_pnl=0.0,
        cumulative_pnl=0.0,
        daily_trade_count=0,
        trading_enabled=True,
    )
    # Long: +5000 + 500 = 5500
    # Short: -2000 + 100 = -1900   (short wins when price falls: +5% of 2000)
    # Equity: 20000 + 5500 - 1900 = 23600
    assert abs(snap.total_equity - 23_600.0) < 0.01
