"""Exhaustive tests for the RiskEngine.

One failure here means real money can escape through the guardrails. Treat
any test added later as load-bearing.
"""

from __future__ import annotations

import pytest

from app.risk import (
    AccountSnapshot,
    Market,
    Position,
    PositionSide,
    RejectionCode,
    RiskConfig,
    RiskEngine,
    TradeAction,
    TradeProposal,
)

# ---------- fixtures / helpers ----------


def make_config(**overrides) -> RiskConfig:
    defaults = dict(
        budget_cap=1000.0,
        max_position_pct=0.10,  # $100 per trade max
        max_concurrent_positions=3,
        max_daily_trades=5,
        daily_loss_cap_pct=0.02,  # -$20/day
        max_drawdown_pct=0.10,  # -$100 total
        default_stop_loss_pct=0.03,
        default_take_profit_pct=0.06,
        min_trade_size_usd=1.0,
        blacklist=("SPCE", "GME"),
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


def make_snapshot(
    cash_balance: float = 1000.0,
    positions: tuple[Position, ...] = (),
    day_realized_pnl: float = 0.0,
    cumulative_pnl: float = 0.0,
    daily_trade_count: int = 0,
    trading_enabled: bool = True,
    day_unrealized_pnl: float = 0.0,
    pdt_day_trades_window_used: int = 0,
) -> AccountSnapshot:
    return AccountSnapshot(
        cash_balance=cash_balance,
        positions=positions,
        day_realized_pnl=day_realized_pnl,
        cumulative_pnl=cumulative_pnl,
        daily_trade_count=daily_trade_count,
        trading_enabled=trading_enabled,
        day_unrealized_pnl=day_unrealized_pnl,
        pdt_day_trades_window_used=pdt_day_trades_window_used,
    )


def make_position(
    symbol: str = "AAPL",
    size_usd: float = 50.0,
    entry_price: float = 100.0,
    current_price: float = 100.0,
    market: Market = Market.STOCKS,
) -> Position:
    return Position(
        market=market,
        symbol=symbol,
        size_usd=size_usd,
        entry_price=entry_price,
        current_price=current_price,
    )


def open_proposal(
    symbol: str = "AAPL",
    size_usd: float = 50.0,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    market: Market = Market.STOCKS,
) -> TradeProposal:
    return TradeProposal(
        market=market,
        action=TradeAction.OPEN_LONG,
        symbol=symbol,
        size_usd=size_usd,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        rationale="test",
    )


def close_proposal(symbol: str = "AAPL", market: Market = Market.STOCKS) -> TradeProposal:
    return TradeProposal(
        market=market,
        action=TradeAction.CLOSE,
        symbol=symbol,
        size_usd=0.0,  # ignored for close
        rationale="test close",
    )


@pytest.fixture
def engine() -> RiskEngine:
    return RiskEngine(make_config())


# ---------- RiskConfig validation ----------


class TestRiskConfigValidation:
    def test_negative_budget_rejected(self):
        with pytest.raises(ValueError, match="budget_cap"):
            RiskConfig(budget_cap=-1)

    def test_position_pct_above_one_rejected(self):
        with pytest.raises(ValueError, match="max_position_pct"):
            RiskConfig(max_position_pct=1.5)

    def test_daily_loss_pct_zero_rejected(self):
        with pytest.raises(ValueError, match="daily_loss_cap_pct"):
            RiskConfig(daily_loss_cap_pct=0)

    def test_zero_max_concurrent_rejected(self):
        with pytest.raises(ValueError, match="max_concurrent_positions"):
            RiskConfig(max_concurrent_positions=0)


# ---------- Kill switch ----------


class TestKillSwitch:
    def test_disabled_blocks_open(self, engine):
        result = engine.validate(open_proposal(), make_snapshot(trading_enabled=False))
        assert not result.approved
        assert result.code == RejectionCode.KILL_SWITCH

    def test_disabled_blocks_close(self, engine):
        snap = make_snapshot(
            trading_enabled=False, positions=(make_position("AAPL"),)
        )
        result = engine.validate(close_proposal("AAPL"), snap)
        assert not result.approved
        assert result.code == RejectionCode.KILL_SWITCH


# ---------- Close validation ----------


class TestClose:
    def test_close_rejected_when_no_position(self, engine):
        result = engine.validate(close_proposal("TSLA"), make_snapshot())
        assert not result.approved
        assert result.code == RejectionCode.NO_POSITION_TO_CLOSE

    def test_close_approved_when_position_exists(self, engine):
        snap = make_snapshot(positions=(make_position("TSLA"),))
        result = engine.validate(close_proposal("TSLA"), snap)
        assert result.approved

    def test_close_bypasses_daily_loss_halt(self, engine):
        """Crucial: if we're at the daily loss cap, we still MUST be able to
        close open positions (that's the whole point of a stop-loss exit)."""
        snap = make_snapshot(
            positions=(make_position("TSLA"),),
            day_realized_pnl=-100.0,  # way past daily loss cap
            cumulative_pnl=-500.0,  # way past drawdown
        )
        result = engine.validate(close_proposal("TSLA"), snap)
        assert result.approved

    def test_close_matches_correct_market(self, engine):
        snap = make_snapshot(
            positions=(make_position("US-ELECTION", market=Market.POLYMARKET),)
        )
        # Same symbol, wrong market — should reject
        result = engine.validate(
            close_proposal("US-ELECTION", market=Market.STOCKS), snap
        )
        assert not result.approved
        assert result.code == RejectionCode.NO_POSITION_TO_CLOSE


# ---------- Sizing guardrails ----------


class TestSizing:
    def test_zero_size_rejected(self, engine):
        result = engine.validate(open_proposal(size_usd=0), make_snapshot())
        assert not result.approved
        assert result.code == RejectionCode.SIZE_NONPOSITIVE

    def test_negative_size_rejected(self, engine):
        result = engine.validate(open_proposal(size_usd=-10), make_snapshot())
        assert not result.approved
        assert result.code == RejectionCode.SIZE_NONPOSITIVE

    def test_below_min_trade_rejected(self, engine):
        result = engine.validate(open_proposal(size_usd=0.50), make_snapshot())
        assert not result.approved
        assert result.code == RejectionCode.SIZE_BELOW_MIN

    def test_exactly_min_trade_allowed(self, engine):
        result = engine.validate(open_proposal(size_usd=1.0), make_snapshot())
        assert result.approved

    def test_per_trade_max_enforced(self, engine):
        # max_position_pct = 0.10 of $1000 budget = $100 max per trade
        result = engine.validate(open_proposal(size_usd=100.01), make_snapshot())
        assert not result.approved
        assert result.code == RejectionCode.PER_TRADE_MAX_EXCEEDED

    def test_exactly_per_trade_max_allowed(self, engine):
        result = engine.validate(open_proposal(size_usd=100.00), make_snapshot())
        assert result.approved


# ---------- Blacklist ----------


class TestBlacklist:
    def test_blacklisted_symbol_rejected(self, engine):
        result = engine.validate(open_proposal(symbol="GME"), make_snapshot())
        assert not result.approved
        assert result.code == RejectionCode.BLACKLISTED

    def test_blacklist_case_insensitive(self, engine):
        result = engine.validate(open_proposal(symbol="gme"), make_snapshot())
        assert not result.approved
        assert result.code == RejectionCode.BLACKLISTED

    def test_non_blacklisted_allowed(self, engine):
        result = engine.validate(open_proposal(symbol="AAPL"), make_snapshot())
        assert result.approved


# ---------- Budget cap ----------


class TestBudgetCap:
    def test_budget_exceeded_rejected(self):
        # Loose per-trade + concurrent caps so budget is the binding one
        engine = RiskEngine(
            make_config(max_position_pct=1.0, max_concurrent_positions=10)
        )
        snap = make_snapshot(
            cash_balance=2000.0,
            positions=(
                make_position("A", size_usd=400),
                make_position("B", size_usd=400),
                make_position("C", size_usd=150),
            ),  # 950 deployed, budget 1000
        )
        # Proposing $100 → would be $1050 total → reject
        result = engine.validate(open_proposal(size_usd=100.0), snap)
        assert not result.approved
        assert result.code == RejectionCode.BUDGET_EXCEEDED

    def test_budget_exactly_at_cap_allowed(self):
        engine = RiskEngine(make_config(max_position_pct=1.0, max_concurrent_positions=10))
        snap = make_snapshot(
            cash_balance=2000.0,
            positions=(make_position("A", size_usd=900),),
        )
        result = engine.validate(open_proposal(size_usd=100.0), snap)
        assert result.approved

    def test_insufficient_cash_rejected(self, engine):
        snap = make_snapshot(cash_balance=40.0)
        result = engine.validate(open_proposal(size_usd=50.0), snap)
        assert not result.approved
        assert result.code == RejectionCode.INSUFFICIENT_CASH

    def test_equity_drawdown_caps_effective_budget(self):
        """When equity drops below budget_cap, new opens are capped by
        equity — not the stale config number. Without this cross-check
        a drawdown would still let the engine deploy to the old budget,
        digging the hole deeper.
        """
        engine = RiskEngine(
            make_config(
                budget_cap=5000.0,
                max_position_pct=1.0,
                max_concurrent_positions=10,
            )
        )
        # Cash $1500; one position opened at $1000 now worth $900 (-10%).
        # Equity = 1500 + 1000 + (-100) = $2400, well below $5000 budget.
        snap = make_snapshot(
            cash_balance=1500.0,
            positions=(
                make_position(
                    "LOSS", size_usd=1000.0, entry_price=100.0, current_price=90.0
                ),
            ),
        )
        # Proposing $1400: exposure projected = 1000+1400 = $2400,
        # exactly at equity cap, still below budget — allowed.
        ok = engine.validate(open_proposal(size_usd=1400.0), snap)
        assert ok.approved
        # Proposing $1500: exposure projected = 1000+1500 = $2500.
        # Old check against stale $5000 budget would allow. New
        # equity-effective cap $2400 rejects BUDGET_EXCEEDED.
        bad = engine.validate(open_proposal(size_usd=1500.0), snap)
        assert not bad.approved
        assert bad.code == RejectionCode.BUDGET_EXCEEDED


# ---------- Max concurrent positions ----------


class TestMaxConcurrent:
    def test_at_limit_new_symbol_rejected(self, engine):
        # cfg.max_concurrent_positions = 3
        snap = make_snapshot(
            positions=(
                make_position("A"),
                make_position("B"),
                make_position("C"),
            )
        )
        result = engine.validate(open_proposal(symbol="D"), snap)
        assert not result.approved
        assert result.code == RejectionCode.MAX_CONCURRENT_REACHED

    def test_duplicate_symbol_rejected(self, engine):
        """Open proposals for an already-held symbol are rejected — the
        position-review agent owns existing holdings and the decision
        agent must not propose duplicates."""
        snap = make_snapshot(
            positions=(
                make_position("A"),
                make_position("B"),
                make_position("C"),
            )
        )
        result = engine.validate(open_proposal(symbol="A", size_usd=50), snap)
        assert not result.approved
        assert result.code == RejectionCode.DUPLICATE_POSITION

    def test_below_limit_new_symbol_allowed(self, engine):
        snap = make_snapshot(positions=(make_position("A"), make_position("B")))
        result = engine.validate(open_proposal(symbol="D"), snap)
        assert result.approved


# ---------- Daily trade cap ----------


class TestDailyTradeCap:
    def test_at_daily_cap_rejected(self, engine):
        # cfg.max_daily_trades = 5
        snap = make_snapshot(daily_trade_count=5)
        result = engine.validate(open_proposal(), snap)
        assert not result.approved
        assert result.code == RejectionCode.DAILY_TRADE_CAP_REACHED

    def test_one_below_cap_allowed(self, engine):
        snap = make_snapshot(daily_trade_count=4)
        result = engine.validate(open_proposal(), snap)
        assert result.approved


# ---------- Daily loss halt ----------


class TestDailyLossHalt:
    def test_at_loss_cap_halts(self, engine):
        # cfg daily_loss_cap_pct=0.02, budget=1000 → limit = -$20
        snap = make_snapshot(day_realized_pnl=-20.0)
        result = engine.validate(open_proposal(), snap)
        assert not result.approved
        assert result.code == RejectionCode.DAILY_LOSS_HALT

    def test_past_loss_cap_halts(self, engine):
        snap = make_snapshot(day_realized_pnl=-25.0)
        result = engine.validate(open_proposal(), snap)
        assert not result.approved
        assert result.code == RejectionCode.DAILY_LOSS_HALT

    def test_just_above_loss_cap_allowed(self, engine):
        snap = make_snapshot(day_realized_pnl=-19.99)
        result = engine.validate(open_proposal(), snap)
        assert result.approved


# ---------- Max drawdown halt ----------


class TestMaxDrawdownHalt:
    def test_at_drawdown_halts(self, engine):
        # cfg max_drawdown_pct=0.10, budget=1000 → limit = -$100
        snap = make_snapshot(cumulative_pnl=-100.0)
        result = engine.validate(open_proposal(), snap)
        assert not result.approved
        assert result.code == RejectionCode.MAX_DRAWDOWN_HALT

    def test_past_drawdown_halts(self, engine):
        snap = make_snapshot(cumulative_pnl=-150.0)
        result = engine.validate(open_proposal(), snap)
        assert not result.approved
        assert result.code == RejectionCode.MAX_DRAWDOWN_HALT


# ---------- Mandatory attribute injection ----------


class TestMandatoryAttributes:
    def test_stop_loss_injected_when_missing(self, engine):
        result = engine.validate(open_proposal(stop_loss_pct=None), make_snapshot())
        assert result.approved
        assert result.adjusted is not None
        assert result.adjusted.stop_loss_pct == 0.03  # default

    def test_take_profit_injected_when_missing(self, engine):
        result = engine.validate(
            open_proposal(take_profit_pct=None), make_snapshot()
        )
        assert result.approved
        assert result.adjusted.take_profit_pct == 0.06

    def test_custom_stop_loss_preserved(self, engine):
        result = engine.validate(
            open_proposal(stop_loss_pct=0.01), make_snapshot()
        )
        assert result.approved
        assert result.adjusted.stop_loss_pct == 0.01

    def test_custom_take_profit_preserved(self, engine):
        result = engine.validate(
            open_proposal(take_profit_pct=0.50), make_snapshot()
        )
        assert result.approved
        assert result.adjusted.take_profit_pct == 0.50


# ---------- End-to-end happy path ----------


class TestHappyPath:
    def test_clean_trade_passes_all_checks(self, engine):
        snap = make_snapshot(
            cash_balance=500.0,
            positions=(make_position("MSFT", size_usd=100),),
            day_realized_pnl=5.0,
            cumulative_pnl=20.0,
            daily_trade_count=2,
            trading_enabled=True,
        )
        result = engine.validate(
            open_proposal(symbol="NVDA", size_usd=75.0), snap
        )
        assert result.approved
        assert result.code is None
        assert result.adjusted.symbol == "NVDA"
        assert result.adjusted.stop_loss_pct == 0.03
        assert result.adjusted.take_profit_pct == 0.06


# ---------- Position P&L calculation ----------


class TestPositionPnl:
    def test_unrealized_pnl_positive(self):
        p = make_position(size_usd=100, entry_price=50, current_price=55)
        assert p.unrealized_pnl == pytest.approx(10.0)  # +10% on $100

    def test_unrealized_pnl_negative(self):
        p = make_position(size_usd=100, entry_price=50, current_price=45)
        assert p.unrealized_pnl == pytest.approx(-10.0)

    def test_unrealized_pnl_flat(self):
        p = make_position(size_usd=100, entry_price=50, current_price=50)
        assert p.unrealized_pnl == pytest.approx(0.0)

    def test_short_profits_when_price_falls(self):
        p = Position(
            market=Market.STOCKS,
            symbol="TSLA",
            size_usd=100,
            entry_price=50,
            current_price=45,  # price fell → short wins
            side=PositionSide.SHORT,
        )
        assert p.unrealized_pnl == pytest.approx(10.0)

    def test_short_loses_when_price_rises(self):
        p = Position(
            market=Market.STOCKS,
            symbol="TSLA",
            size_usd=100,
            entry_price=50,
            current_price=55,  # price rose → short loses
            side=PositionSide.SHORT,
        )
        assert p.unrealized_pnl == pytest.approx(-10.0)


# ---------- Halts include unrealized P&L ----------


class TestHaltsIncludeUnrealized:
    def test_daily_loss_halt_fires_on_unrealized_only(self, engine):
        # realized 0, but an open position is -$25 mark-to-market
        # (limit is -$20). Halt must fire even before the trade closes.
        snap = make_snapshot(day_unrealized_pnl=-25.0)
        result = engine.validate(open_proposal(), snap)
        assert not result.approved
        assert result.code == RejectionCode.DAILY_LOSS_HALT

    def test_daily_loss_halt_fires_on_realized_plus_unrealized(self, engine):
        # -$15 realized + -$10 unrealized = -$25 total; limit is -$20.
        snap = make_snapshot(day_realized_pnl=-15.0, day_unrealized_pnl=-10.0)
        result = engine.validate(open_proposal(), snap)
        assert not result.approved
        assert result.code == RejectionCode.DAILY_LOSS_HALT

    def test_daily_unrealized_gains_do_not_fire(self, engine):
        # Positive unrealized must not inadvertently allow huge losses.
        snap = make_snapshot(day_realized_pnl=-15.0, day_unrealized_pnl=+20.0)
        result = engine.validate(open_proposal(), snap)
        assert result.approved  # net +$5 > limit -$20

    def test_drawdown_halt_fires_on_open_unrealized(self, engine):
        # realized 0 cumulative, but a position is deep underwater.
        # limit = -$100. Position is $200 cost basis at -60% → -$120.
        underwater = make_position(
            "A", size_usd=200, entry_price=100, current_price=40
        )
        snap = make_snapshot(cumulative_pnl=0.0, positions=(underwater,))
        result = engine.validate(
            open_proposal(symbol="B", size_usd=50), snap
        )
        assert not result.approved
        assert result.code == RejectionCode.MAX_DRAWDOWN_HALT


class TestPdtGuard:
    """FINRA Pattern Day Trader guard: sub-$25k accounts get 3 same-day
    round trips per 5 business days. The engine rejects new opens once
    the count reaches the configured cap."""

    @pytest.fixture
    def engine(self) -> RiskEngine:
        return RiskEngine(make_config(pdt_day_trade_count_5bd=3))

    def test_approves_below_pdt_cap(self, engine):
        snap = make_snapshot(pdt_day_trades_window_used=2)
        result = engine.validate(open_proposal(symbol="AAPL"), snap)
        assert result.approved

    def test_rejects_at_pdt_cap(self, engine):
        snap = make_snapshot(pdt_day_trades_window_used=3)
        result = engine.validate(open_proposal(symbol="AAPL"), snap)
        assert not result.approved
        assert result.code == RejectionCode.PDT_LIMIT_REACHED

    def test_rejects_above_pdt_cap(self, engine):
        snap = make_snapshot(pdt_day_trades_window_used=4)
        result = engine.validate(open_proposal(symbol="AAPL"), snap)
        assert not result.approved
        assert result.code == RejectionCode.PDT_LIMIT_REACHED

    def test_close_allowed_even_at_pdt_cap(self, engine):
        # PDT only gates *new opens*. An exit must always go through.
        pos = make_position("AAPL")
        snap = make_snapshot(
            positions=(pos,), pdt_day_trades_window_used=3
        )
        result = engine.validate(
            TradeProposal(
                market=Market.STOCKS,
                action=TradeAction.CLOSE,
                symbol="AAPL",
                size_usd=50.0,
            ),
            snap,
        )
        assert result.approved

    def test_high_cap_disables_pdt(self):
        """Above-$25k accounts set a permissive cap (99)."""
        engine = RiskEngine(make_config(pdt_day_trade_count_5bd=99))
        snap = make_snapshot(pdt_day_trades_window_used=10)
        result = engine.validate(open_proposal(), snap)
        assert result.approved
