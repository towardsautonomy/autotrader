from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class Market(StrEnum):
    STOCKS = "stocks"
    POLYMARKET = "polymarket"


class TradeAction(StrEnum):
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    CLOSE = "close"


class PositionSide(StrEnum):
    LONG = "long"
    SHORT = "short"


class RiskTier(StrEnum):
    """User-selected appetite. Gates which option structures are allowed.

    Inputs are 1-to-1 with real trader slang: conservative = "I only want
    defined-risk or income-on-shares plays", aggressive = "I know what I'm
    doing, let me run spreads with wide wings". We never expose naked
    short calls/puts at any tier — unbounded loss is off-menu by design.
    """

    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


class OptionStructure(StrEnum):
    """All supported option structures. All are *defined-risk* — naked
    short calls/puts are deliberately omitted."""

    STOCK = "stock"  # plain equity — always allowed
    LONG_CALL = "long_call"
    LONG_PUT = "long_put"
    COVERED_CALL = "covered_call"
    CASH_SECURED_PUT = "cash_secured_put"
    VERTICAL_DEBIT = "vertical_debit"  # bull call / bear put
    VERTICAL_CREDIT = "vertical_credit"  # bull put / bear call (cash-backed)
    IRON_CONDOR = "iron_condor"


STRUCTURES_BY_TIER: dict[RiskTier, frozenset[OptionStructure]] = {
    RiskTier.CONSERVATIVE: frozenset(
        {
            OptionStructure.STOCK,
            OptionStructure.COVERED_CALL,
            OptionStructure.CASH_SECURED_PUT,
        }
    ),
    RiskTier.MODERATE: frozenset(
        {
            OptionStructure.STOCK,
            OptionStructure.COVERED_CALL,
            OptionStructure.CASH_SECURED_PUT,
            OptionStructure.LONG_CALL,
            OptionStructure.LONG_PUT,
            OptionStructure.VERTICAL_DEBIT,
            OptionStructure.VERTICAL_CREDIT,
        }
    ),
    RiskTier.AGGRESSIVE: frozenset(
        {
            OptionStructure.STOCK,
            OptionStructure.COVERED_CALL,
            OptionStructure.CASH_SECURED_PUT,
            OptionStructure.LONG_CALL,
            OptionStructure.LONG_PUT,
            OptionStructure.VERTICAL_DEBIT,
            OptionStructure.VERTICAL_CREDIT,
            OptionStructure.IRON_CONDOR,
        }
    ),
}


class OptionSide(StrEnum):
    CALL = "call"
    PUT = "put"


@dataclass(frozen=True, slots=True)
class OptionLeg:
    """A single leg of an option proposal.

    Positive ratio = long, negative ratio = short. Quantity semantics live
    at the broker layer — here we only model intent.
    """

    option_symbol: str  # OCC symbol e.g. AAPL250117C00150000
    side: OptionSide
    strike: float
    expiry: str  # ISO date
    ratio: int  # +1 long / -1 short, or +2/-1 for ratios if tier allows
    mid_price: float | None = None


@dataclass(frozen=True, slots=True)
class OptionProposal:
    """Defined-risk option proposal. All fields are required and computed
    up front so the risk engine can validate max_loss/max_gain without
    re-deriving anything."""

    structure: OptionStructure
    underlying: str
    legs: tuple[OptionLeg, ...]
    net_debit_usd: float  # positive = debit paid, negative = credit received
    max_loss_usd: float  # absolute max $ at risk (always >= 0)
    max_gain_usd: float | None  # None = uncapped upside (e.g. long call)
    expiry: str  # worst-case expiry of any leg (ISO date)


@dataclass(frozen=True, slots=True)
class TradeProposal:
    market: Market
    action: TradeAction
    symbol: str
    size_usd: float
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    rationale: str = ""
    confidence: float | None = None
    # Option overlay: None for plain stock trades.
    option: OptionProposal | None = None

    @property
    def structure(self) -> OptionStructure:
        return self.option.structure if self.option else OptionStructure.STOCK

    def with_defaults(
        self, stop_loss_pct: float, take_profit_pct: float
    ) -> TradeProposal:
        return replace(
            self,
            stop_loss_pct=self.stop_loss_pct if self.stop_loss_pct is not None else stop_loss_pct,
            take_profit_pct=self.take_profit_pct
            if self.take_profit_pct is not None
            else take_profit_pct,
        )


@dataclass(frozen=True, slots=True)
class Position:
    market: Market
    symbol: str
    size_usd: float
    entry_price: float
    current_price: float
    side: PositionSide = PositionSide.LONG

    @property
    def unrealized_pnl(self) -> float:
        if self.entry_price == 0:
            return 0.0
        # Long profits when price rises; short profits when price falls.
        raw = self.size_usd * (self.current_price / self.entry_price - 1.0)
        return raw if self.side == PositionSide.LONG else -raw


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    cash_balance: float
    positions: tuple[Position, ...]
    day_realized_pnl: float
    cumulative_pnl: float
    daily_trade_count: int
    trading_enabled: bool
    # Sum of unrealized P&L on positions *opened today*. Drives the
    # daily_loss_halt check: a position that's 50% underwater intraday
    # must halt new trading even before it closes.
    day_unrealized_pnl: float = 0.0
    # Count of same-NY-session open+close round trips in the trailing
    # 5 business days. Drives the PDT guard so sub-$25k accounts don't
    # rack up a fourth day trade and trigger FINRA's PDT restriction.
    pdt_day_trades_window_used: int = 0

    @property
    def total_exposure_usd(self) -> float:
        return sum(p.size_usd for p in self.positions)

    @property
    def total_equity(self) -> float:
        # For a short, cash already reflects the proceeds from the sale and
        # the position is a *liability* worth its current market value.
        # Contribution to equity is therefore -size_usd + unrealized_pnl
        # (unrealized_pnl is already signed correctly by Position), not
        # +size_usd + unrealized_pnl. Matching Alpaca's portfolio_value
        # requires honouring that sign.
        equity = self.cash_balance
        for p in self.positions:
            sign = 1 if p.side == PositionSide.LONG else -1
            equity += sign * p.size_usd + p.unrealized_pnl
        return equity

    @property
    def unrealized_pnl_total(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions)

    @property
    def day_pnl_total(self) -> float:
        return self.day_realized_pnl + self.day_unrealized_pnl

    @property
    def cumulative_pnl_with_open(self) -> float:
        return self.cumulative_pnl + self.unrealized_pnl_total

    def find_position(self, market: Market, symbol: str) -> Position | None:
        for p in self.positions:
            if p.market == market and p.symbol == symbol:
                return p
        return None


@dataclass(frozen=True, slots=True)
class RiskConfig:
    budget_cap: float = 1000.0
    max_position_pct: float = 0.05
    max_concurrent_positions: int = 5
    max_daily_trades: int = 10
    daily_loss_cap_pct: float = 0.02
    max_drawdown_pct: float = 0.10
    default_stop_loss_pct: float = 0.03
    default_take_profit_pct: float = 0.06
    min_trade_size_usd: float = 1.0
    blacklist: tuple[str, ...] = field(default_factory=tuple)
    # Options — tier gates which defined-risk structures the engine allows.
    risk_tier: RiskTier = RiskTier.MODERATE
    max_option_loss_per_spread_pct: float = 0.02  # of budget_cap
    earnings_blackout_days: int = 2  # no new option opens within N days of earnings
    # Hard ceiling on stop_loss_pct — stops the LLM from proposing a 50%
    # stop that converts "bounded loss" into "almost all the money".
    max_stop_loss_pct: float = 0.10
    # Minimum LLM confidence required to open a new position. Below this
    # we treat the proposal as a coin-flip and reject — the losing-streak
    # audit found wins vs losses differ by only ~4.5pp of confidence, so
    # anything <0.65 is noise.
    min_open_confidence: float = 0.65
    # Minimum reward/risk ratio on opens (take_profit_pct / stop_loss_pct).
    # 1.5 means a win must be at least 1.5x the loss in percent terms.
    # Losers that round-trip through a wide stop are a top drain.
    min_reward_risk_ratio: float = 1.5
    # Round-trip simulated cost applied to paper-mode realized P&L so
    # analytics reflect what real fills would cost (spread + slippage).
    # 10 bps = 0.10%. Conservative default: real execution on mid-cap
    # equities averages 5–15 bps round trip; options spreads are wider.
    # Paper results under this cost that still look profitable are a
    # better signal than results under a 5 bps assumption.
    paper_cost_bps: float = 10.0
    # FINRA PDT cap: accounts under $25k equity are limited to 3 same-day
    # round trips per rolling 5 business days. A 4th triggers a 90-day
    # restriction. Default 3 is the regulated limit; set higher (e.g.
    # 99) once account equity is comfortably above $25k.
    pdt_day_trade_count_5bd: int = 3

    def __post_init__(self) -> None:
        if self.budget_cap <= 0:
            raise ValueError("budget_cap must be positive")
        if not 0 < self.max_position_pct <= 1:
            raise ValueError("max_position_pct must be in (0, 1]")
        if not 0 < self.daily_loss_cap_pct <= 1:
            raise ValueError("daily_loss_cap_pct must be in (0, 1]")
        if not 0 < self.max_drawdown_pct <= 1:
            raise ValueError("max_drawdown_pct must be in (0, 1]")
        if self.max_concurrent_positions < 1:
            raise ValueError("max_concurrent_positions must be >= 1")
        if self.max_daily_trades < 1:
            raise ValueError("max_daily_trades must be >= 1")
        if self.default_stop_loss_pct <= 0:
            raise ValueError("default_stop_loss_pct must be positive")
        if not 0 < self.max_option_loss_per_spread_pct <= 1:
            raise ValueError("max_option_loss_per_spread_pct must be in (0, 1]")
        if self.earnings_blackout_days < 0:
            raise ValueError("earnings_blackout_days must be >= 0")
        if not 0 < self.max_stop_loss_pct <= 1:
            raise ValueError("max_stop_loss_pct must be in (0, 1]")
        if self.paper_cost_bps < 0:
            raise ValueError("paper_cost_bps must be >= 0")
        if self.default_stop_loss_pct > self.max_stop_loss_pct:
            raise ValueError(
                "default_stop_loss_pct cannot exceed max_stop_loss_pct"
            )
        if self.pdt_day_trade_count_5bd < 0:
            raise ValueError("pdt_day_trade_count_5bd must be >= 0")
        if not 0 <= self.min_open_confidence <= 1:
            raise ValueError("min_open_confidence must be in [0, 1]")
        if self.min_reward_risk_ratio < 0:
            raise ValueError("min_reward_risk_ratio must be >= 0")

    @property
    def per_trade_max_usd(self) -> float:
        return self.budget_cap * self.max_position_pct

    @property
    def daily_loss_limit_usd(self) -> float:
        return -self.budget_cap * self.daily_loss_cap_pct

    @property
    def max_drawdown_limit_usd(self) -> float:
        return -self.budget_cap * self.max_drawdown_pct

    @property
    def max_option_loss_per_spread_usd(self) -> float:
        return self.budget_cap * self.max_option_loss_per_spread_pct

    def allowed_structures(self) -> frozenset[OptionStructure]:
        return STRUCTURES_BY_TIER[self.risk_tier]


class RejectionCode(StrEnum):
    KILL_SWITCH = "kill_switch"
    MARKET_CLOSED = "market_closed"
    NO_POSITION_TO_CLOSE = "no_position_to_close"
    SIZE_BELOW_MIN = "size_below_min"
    SIZE_NONPOSITIVE = "size_nonpositive"
    BLACKLISTED = "blacklisted"
    BUDGET_EXCEEDED = "budget_exceeded"
    OVER_BUDGET_DELEVERAGE = "over_budget_deleverage"
    PER_TRADE_MAX_EXCEEDED = "per_trade_max_exceeded"
    MAX_CONCURRENT_REACHED = "max_concurrent_reached"
    DAILY_TRADE_CAP_REACHED = "daily_trade_cap_reached"
    DAILY_LOSS_HALT = "daily_loss_halt"
    MAX_DRAWDOWN_HALT = "max_drawdown_halt"
    INSUFFICIENT_CASH = "insufficient_cash"
    # Options-specific rejections
    STRUCTURE_NOT_ALLOWED = "structure_not_allowed"
    UNDEFINED_RISK = "undefined_risk"
    OPTION_MAX_LOSS_EXCEEDED = "option_max_loss_exceeded"
    EXPIRY_TOO_CLOSE = "expiry_too_close"
    EARNINGS_BLACKOUT = "earnings_blackout"
    DUPLICATE_POSITION = "duplicate_position"
    STOP_LOSS_TOO_WIDE = "stop_loss_too_wide"
    PDT_LIMIT_REACHED = "pdt_limit_reached"
    CONFIDENCE_TOO_LOW = "confidence_too_low"
    REWARD_RISK_TOO_LOW = "reward_risk_too_low"


@dataclass(frozen=True, slots=True)
class ValidationResult:
    approved: bool
    reason: str
    code: RejectionCode | None = None
    adjusted: TradeProposal | None = None

    @classmethod
    def approve(cls, adjusted: TradeProposal) -> ValidationResult:
        return cls(approved=True, reason="ok", adjusted=adjusted)

    @classmethod
    def reject(cls, code: RejectionCode, reason: str) -> ValidationResult:
        return cls(approved=False, reason=reason, code=code)
