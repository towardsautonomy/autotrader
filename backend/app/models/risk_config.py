from __future__ import annotations

from sqlalchemy import JSON, Boolean, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.risk import RiskConfig as RiskConfigDC
from app.risk import RiskTier

from .base import Base, TimestampMixin


class RiskConfigRow(Base, TimestampMixin):
    """Versioned risk config. Only the row with is_active=True is enforced."""

    __tablename__ = "risk_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    budget_cap: Mapped[float] = mapped_column(Float, nullable=False)
    max_position_pct: Mapped[float] = mapped_column(Float, nullable=False)
    max_concurrent_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    max_daily_trades: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_loss_cap_pct: Mapped[float] = mapped_column(Float, nullable=False)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False)
    default_stop_loss_pct: Mapped[float] = mapped_column(Float, nullable=False)
    default_take_profit_pct: Mapped[float] = mapped_column(Float, nullable=False)
    min_trade_size_usd: Mapped[float] = mapped_column(Float, nullable=False)

    # JSON list of blacklisted symbols
    blacklist: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Options — nullable with safe defaults so older rows keep working.
    risk_tier: Mapped[str | None] = mapped_column(
        String(32), default=RiskTier.MODERATE.value, nullable=True
    )
    max_option_loss_per_spread_pct: Mapped[float | None] = mapped_column(
        Float, default=0.02, nullable=True
    )
    earnings_blackout_days: Mapped[int | None] = mapped_column(
        Integer, default=2, nullable=True
    )

    # Safety v2 — added after shipping. Nullable so existing rows
    # don't need a migration; to_dataclass() fills defaults.
    max_stop_loss_pct: Mapped[float | None] = mapped_column(
        Float, default=0.10, nullable=True
    )
    paper_cost_bps: Mapped[float | None] = mapped_column(
        Float, default=5.0, nullable=True
    )

    # FINRA PDT 3-in-5. Nullable so pre-migration rows work; to_dataclass
    # falls back to the regulated default of 3.
    pdt_day_trade_count_5bd: Mapped[int | None] = mapped_column(
        Integer, default=3, nullable=True
    )

    # Quality gates — enforce a confidence floor and min reward/risk on
    # every open. Nullable for pre-migration rows; defaults mirror the
    # dataclass (0.65, 1.5).
    min_open_confidence: Mapped[float | None] = mapped_column(
        Float, default=0.65, nullable=True
    )
    min_reward_risk_ratio: Mapped[float | None] = mapped_column(
        Float, default=1.5, nullable=True
    )

    changed_by: Mapped[str | None] = mapped_column(String(128))

    def to_dataclass(self) -> RiskConfigDC:
        tier_raw = self.risk_tier or RiskTier.MODERATE.value
        try:
            tier = RiskTier(tier_raw)
        except ValueError:
            tier = RiskTier.MODERATE
        return RiskConfigDC(
            budget_cap=self.budget_cap,
            max_position_pct=self.max_position_pct,
            max_concurrent_positions=self.max_concurrent_positions,
            max_daily_trades=self.max_daily_trades,
            daily_loss_cap_pct=self.daily_loss_cap_pct,
            max_drawdown_pct=self.max_drawdown_pct,
            default_stop_loss_pct=self.default_stop_loss_pct,
            default_take_profit_pct=self.default_take_profit_pct,
            min_trade_size_usd=self.min_trade_size_usd,
            blacklist=tuple(self.blacklist or ()),
            risk_tier=tier,
            max_option_loss_per_spread_pct=(
                self.max_option_loss_per_spread_pct
                if self.max_option_loss_per_spread_pct is not None
                else 0.02
            ),
            earnings_blackout_days=(
                self.earnings_blackout_days
                if self.earnings_blackout_days is not None
                else 2
            ),
            max_stop_loss_pct=(
                self.max_stop_loss_pct
                if self.max_stop_loss_pct is not None
                else 0.10
            ),
            paper_cost_bps=(
                self.paper_cost_bps
                if self.paper_cost_bps is not None
                else 5.0
            ),
            pdt_day_trade_count_5bd=(
                self.pdt_day_trade_count_5bd
                if self.pdt_day_trade_count_5bd is not None
                else 3
            ),
            min_open_confidence=(
                self.min_open_confidence
                if self.min_open_confidence is not None
                else 0.65
            ),
            min_reward_risk_ratio=(
                self.min_reward_risk_ratio
                if self.min_reward_risk_ratio is not None
                else 1.5
            ),
        )

    @classmethod
    def from_dataclass(cls, dc: RiskConfigDC, changed_by: str | None = None) -> RiskConfigRow:
        return cls(
            budget_cap=dc.budget_cap,
            max_position_pct=dc.max_position_pct,
            max_concurrent_positions=dc.max_concurrent_positions,
            max_daily_trades=dc.max_daily_trades,
            daily_loss_cap_pct=dc.daily_loss_cap_pct,
            max_drawdown_pct=dc.max_drawdown_pct,
            default_stop_loss_pct=dc.default_stop_loss_pct,
            default_take_profit_pct=dc.default_take_profit_pct,
            min_trade_size_usd=dc.min_trade_size_usd,
            blacklist=list(dc.blacklist),
            risk_tier=dc.risk_tier.value,
            max_option_loss_per_spread_pct=dc.max_option_loss_per_spread_pct,
            earnings_blackout_days=dc.earnings_blackout_days,
            max_stop_loss_pct=dc.max_stop_loss_pct,
            paper_cost_bps=dc.paper_cost_bps,
            pdt_day_trade_count_5bd=dc.pdt_day_trade_count_5bd,
            min_open_confidence=dc.min_open_confidence,
            min_reward_risk_ratio=dc.min_reward_risk_ratio,
            changed_by=changed_by,
        )
