from __future__ import annotations

from datetime import UTC, date
from datetime import datetime as _dt
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer


def _utc_iso(dt: _dt) -> str:
    """Emit ISO-8601 with UTC offset so JS `new Date(s)` parses it correctly.

    SQLite stores naive datetimes in TEXT; loads come back tz-naive even
    though the source is UTC. Treat naive as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


datetime = Annotated[_dt, PlainSerializer(_utc_iso, return_type=str, when_used="json")]


class PositionVerdictOut(BaseModel):
    """Most recent AI verdict for a position.

    ``status`` is a coarse label the UI renders as a badge:
    HOLD / APPROVED / EXECUTED / REJECTED. For HOLD the ``action`` is
    "hold" and ``rejection_code`` is omitted even though the underlying
    Decision row is a ``strategy_no_op`` — the user reads it as the AI
    affirmatively choosing to keep the position.
    """

    status: str
    action: str | None
    rationale: str | None
    rejection_code: str | None
    created_at: datetime


class PositionOut(BaseModel):
    market: str
    symbol: str
    size_usd: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    last_verdict: PositionVerdictOut | None = None


class AccountOut(BaseModel):
    mode: str  # "PAPER" or "LIVE" — what the user wants (SystemState)
    active_mode: str  # "PAPER" or "LIVE" — what the broker is actually running
    trading_enabled: bool
    agents_paused: bool
    pause_when_market_closed: bool
    cash_balance: float
    total_exposure: float
    total_equity: float
    day_realized_pnl: float
    cumulative_pnl: float
    daily_trade_count: int
    positions: list[PositionOut]


class DecisionOut(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    created_at: datetime
    market: str
    model: str
    approved: bool
    executed: bool
    rejection_code: str | None
    rejection_reason: str | None
    rationale: str | None
    proposal_json: dict | None
    execution_error: str | None
    research: list[dict] | None = Field(default=None, alias="research_json")


class TradeOut(BaseModel):
    id: int
    created_at: datetime
    market: str
    symbol: str
    action: str
    size_usd: float
    entry_price: float | None
    exit_price: float | None
    realized_pnl_usd: float
    status: str
    paper_mode: bool
    opened_at: datetime | None
    closed_at: datetime | None


class RiskConfigIn(BaseModel):
    budget_cap: float = Field(gt=0)
    max_position_pct: float = Field(gt=0, le=1)
    max_concurrent_positions: int = Field(ge=1)
    max_daily_trades: int = Field(ge=1)
    daily_loss_cap_pct: float = Field(gt=0, le=1)
    max_drawdown_pct: float = Field(gt=0, le=1)
    default_stop_loss_pct: float = Field(gt=0)
    default_take_profit_pct: float = Field(gt=0)
    min_trade_size_usd: float = Field(ge=0)
    blacklist: list[str] = Field(default_factory=list)
    # Options
    risk_tier: str = "moderate"  # "conservative" | "moderate" | "aggressive"
    max_option_loss_per_spread_pct: float = Field(default=0.02, gt=0, le=1)
    earnings_blackout_days: int = Field(default=2, ge=0)
    max_stop_loss_pct: float = Field(default=0.10, gt=0, le=1)
    paper_cost_bps: float = Field(default=5.0, ge=0)
    pdt_day_trade_count_5bd: int = Field(default=3, ge=0)
    min_open_confidence: float = Field(default=0.65, ge=0, le=1)
    min_reward_risk_ratio: float = Field(default=1.5, ge=0)


class RiskConfigOut(RiskConfigIn):
    id: int
    is_active: bool
    created_at: datetime
    changed_by: str | None = None


class RiskConfigGenerateIn(BaseModel):
    budget_cap: float = Field(gt=0)
    preference: str | None = None


class KillSwitchIn(BaseModel):
    confirm: str
    reason: str = ""


class PauseWhenClosedIn(BaseModel):
    enabled: bool


class FlipModeIn(BaseModel):
    target_mode: str  # "PAPER" | "LIVE"
    confirm_phrase: str = ""  # required when target_mode="LIVE"


class ActivityEventOut(BaseModel):
    id: int
    created_at: datetime
    type: str
    severity: str
    message: str
    data: dict | None


class AIModelInfo(BaseModel):
    id: str
    state: str | None = None
    arch: str | None = None
    quantization: str | None = None
    max_context_length: int | None = None
    loaded_context_length: int | None = None
    capabilities: list[str] = Field(default_factory=list)


class GPUInfo(BaseModel):
    index: int
    name: str
    total_mb: int
    used_mb: int
    free_mb: int
    utilization_pct: int | None = None


class NewsItemOut(BaseModel):
    symbol: str | None = None
    headline: str
    summary: str = ""
    source: str = ""
    url: str = ""
    datetime: datetime


class QuoteOut(BaseModel):
    symbol: str
    current: float
    change: float
    change_pct: float
    open: float
    high: float
    low: float
    prev_close: float
    ts: datetime


class SymbolDecisionOut(BaseModel):
    id: int
    created_at: datetime
    action: str | None
    approved: bool
    executed: bool
    rationale: str | None
    rejection_code: str | None


class SymbolIntelOut(BaseModel):
    symbol: str
    quote: QuoteOut | None
    news: list[NewsItemOut] = Field(default_factory=list)
    position_size_usd: float | None = None
    position_unrealized_pnl: float | None = None
    last_decision: SymbolDecisionOut | None = None


class MoverOut(BaseModel):
    symbol: str
    category: str
    price: float | None = None
    change: float | None = None
    percent_change: float | None = None
    volume: int | None = None
    trade_count: int | None = None


class DiscoveryOut(BaseModel):
    enabled: bool
    gainers: list[MoverOut] = Field(default_factory=list)
    losers: list[MoverOut] = Field(default_factory=list)
    most_active: list[MoverOut] = Field(default_factory=list)
    last_updated: datetime | None = None
    fetched_at: datetime | None = None


class CandidateOut(BaseModel):
    """A symbol under active consideration right now — what the AI is
    actually thinking about this cycle."""

    symbol: str
    # One of: "position" | "recent_approved" | "discovery" | "shortlist"
    reason: str
    # Human-readable one-liner: "+5.2% gainer", "held $1200 (+$43)",
    # "approved LONG 12m ago"
    note: str = ""
    rank: int = 0


class MarketIntelOut(BaseModel):
    candidates: list[CandidateOut] = Field(default_factory=list)
    symbols: list[SymbolIntelOut]
    market_news: list[NewsItemOut] = Field(default_factory=list)
    discovery: DiscoveryOut
    checked_at: datetime
    news_enabled: bool


class EquityPoint(BaseModel):
    ts: datetime
    cumulative_pnl: float


class DailyPnlPoint(BaseModel):
    day: date
    realized_pnl: float
    trade_count: int
    wins: int
    losses: int


class DecisionStats(BaseModel):
    total: int
    approved: int
    rejected: int
    executed: int


class DecisionBucketPoint(BaseModel):
    day: date
    approved: int
    rejected: int
    executed: int


class WinRateStats(BaseModel):
    wins: int
    losses: int
    breakeven: int
    total: int
    win_rate_pct: float
    avg_win_usd: float
    avg_loss_usd: float


class TradeOutcomePoint(BaseModel):
    id: int
    symbol: str
    action: str
    closed_at: datetime
    realized_pnl_usd: float
    size_usd: float


class ScoutCandidateOut(BaseModel):
    symbol: str
    source: str
    note: str = ""
    score: float | None = None
    added_at: datetime
    age_sec: float


class ScoutQueueOut(BaseModel):
    enabled: bool
    queue_size: int
    ttl_sec: int
    daily_llm_budget_usd: float
    daily_llm_spent_usd: float
    candidates: list[ScoutCandidateOut] = Field(default_factory=list)


class RollingWinRatePoint(BaseModel):
    """Rolling-window win rate at the close of each trade — window sized
    in trade count, so the series is dense and comparable across sparse
    activity periods."""

    trade_index: int
    closed_at: datetime
    window_size: int
    win_rate_pct: float


class LlmCostVsPnlPoint(BaseModel):
    day: date
    llm_cost_usd: float
    realized_pnl_usd: float


class HoldTimeBucket(BaseModel):
    bucket: str  # e.g. "<5m", "5-15m", "15-60m", "1-4h", "4h+"
    wins: int
    losses: int
    count: int


class PnlBySymbolBar(BaseModel):
    symbol: str
    realized_pnl_usd: float
    trade_count: int
    wins: int
    losses: int


class DrawdownPoint(BaseModel):
    ts: datetime
    drawdown_usd: float  # negative distance below running peak (0 at peaks)


class PerformanceStats(BaseModel):
    """Headline strategy metrics — the numbers a trader judges a run by.

    Derived from closed trades only: expectancy is the mean realized P&L
    per closed trade, profit factor is Σ(wins)/|Σ(losses)|, and max
    drawdown is the deepest peak-to-trough on the closed-trade equity
    curve. ``max_drawdown_pct`` is expressed against the configured
    budget cap, not account equity, so it stays comparable across runs.
    """

    max_drawdown_usd: float = 0.0
    max_drawdown_pct: float = 0.0  # vs budget_cap_usd; 0 when cap missing
    profit_factor: float | None = None  # None when no losses yet
    expectancy_usd: float = 0.0
    current_streak: int = 0  # +N after N wins, -N after N losses
    longest_win_streak: int = 0
    longest_loss_streak: int = 0


class DailyEquityPoint(BaseModel):
    """One point per Pacific trading day: end-of-day cumulative realized P&L.

    Aligns cleanly with daily OHLC benchmark bars so SPY (etc.) can overlay
    the portfolio without sub-second timestamp mismatches.
    """

    day: date
    cumulative_pnl: float


class HourBucket(BaseModel):
    """Trade outcomes bucketed by close-hour (market-local 0-23)."""

    hour: int
    wins: int = 0
    losses: int = 0
    realized_pnl_usd: float = 0.0


class AiQualityStats(BaseModel):
    """How good is the AI, not just what P&L did it produce.

    ``avg_confidence_*`` comes from the decision proposal JSON. A healthy
    model has higher confidence on winners than losers — if the gap is
    zero or inverted, the confidence signal is noise.

    ``cost_per_dollar_pnl`` is abs(total LLM spend) / abs(net P&L) when
    P&L is non-zero — how many LLM dollars per dollar of outcome.
    """

    executed_decisions: int = 0
    avg_confidence_wins: float | None = None
    avg_confidence_losses: float | None = None
    avg_confidence_rejected: float | None = None
    median_exec_latency_sec: float | None = None  # approved → opened_at
    total_llm_spend_usd: float = 0.0
    cost_per_executed_decision_usd: float | None = None
    cost_per_dollar_pnl: float | None = None  # None when P&L is 0


class AnalyticsOut(BaseModel):
    equity_curve: list[EquityPoint] = Field(default_factory=list)
    equity_curve_daily: list[DailyEquityPoint] = Field(default_factory=list)
    drawdown_curve: list[DrawdownPoint] = Field(default_factory=list)
    daily_pnl: list[DailyPnlPoint] = Field(default_factory=list)
    decision_stats: DecisionStats
    decision_timeline: list[DecisionBucketPoint] = Field(default_factory=list)
    win_rate: WinRateStats
    performance: PerformanceStats = Field(default_factory=PerformanceStats)
    ai_quality: AiQualityStats = Field(default_factory=AiQualityStats)
    trade_outcomes: list[TradeOutcomePoint] = Field(default_factory=list)
    rolling_win_rate: list[RollingWinRatePoint] = Field(default_factory=list)
    llm_cost_vs_pnl: list[LlmCostVsPnlPoint] = Field(default_factory=list)
    hold_time_distribution: list[HoldTimeBucket] = Field(default_factory=list)
    hour_of_day_distribution: list[HourBucket] = Field(default_factory=list)
    pnl_by_symbol: list[PnlBySymbolBar] = Field(default_factory=list)
    budget_cap_usd: float = 0.0
    generated_at: datetime


class BenchmarkBar(BaseModel):
    day: date
    close: float


class BenchmarkSeries(BaseModel):
    symbol: str
    bars: list[BenchmarkBar] = Field(default_factory=list)
    error: str | None = None


class BenchmarkOut(BaseModel):
    start: date
    end: date
    series: list[BenchmarkSeries] = Field(default_factory=list)


class OptionContractOut(BaseModel):
    symbol: str
    side: str  # "call" | "put"
    strike: float
    expiry: str
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    last: float | None = None
    implied_volatility: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    open_interest: int | None = None
    volume: int | None = None


class OptionChainOut(BaseModel):
    underlying: str
    expiries: list[str]
    contracts: list[OptionContractOut]
    fetched_at: datetime


class AIStatusOut(BaseModel):
    provider: str
    model_configured: str
    base_url: str
    reachable: bool
    reachable_error: str | None = None
    loaded_model_id: str | None = None
    configured_model_state: str | None = None
    models: list[AIModelInfo] = Field(default_factory=list)
    gpus: list[GPUInfo] = Field(default_factory=list)
    checked_at: datetime


class LlmRateEntry(BaseModel):
    prompt_per_1k_usd: float
    completion_per_1k_usd: float


class LlmRateCardOut(BaseModel):
    id: int
    rates: dict[str, LlmRateEntry]
    updated_at: datetime | None = None


class LlmRateCardIn(BaseModel):
    rates: dict[str, LlmRateEntry]


class LlmUsageRowOut(BaseModel):
    id: int
    created_at: datetime
    provider: str
    model: str
    purpose: str | None
    agent_id: str | None = None
    round_idx: int | None = None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float


class LlmCallDetailOut(LlmUsageRowOut):
    prompt_messages: list[dict] | None = None
    response_body: dict | None = None


class LlmUsageBucket(BaseModel):
    key: str  # "provider::model"
    provider: str
    model: str
    calls: int
    total_tokens: int
    cost_usd: float


class AgentSummaryOut(BaseModel):
    """One-row summary of an agent's LLM activity over a time window."""

    agent_id: str
    role: str | None = None  # inferred from purpose (e.g. "scout", "research")
    calls: int
    total_tokens: int
    cost_usd: float
    first_call_at: datetime | None = None
    last_call_at: datetime | None = None
    last_model: str | None = None
    last_purpose: str | None = None


class AgentsOverviewOut(BaseModel):
    window_hours: int
    total_agents: int
    total_calls: int
    total_cost_usd: float
    agents: list[AgentSummaryOut] = Field(default_factory=list)
    generated_at: datetime


class AgentRoleOut(BaseModel):
    """One row of the agent roster — what types of agents are enabled."""

    role: str  # stable key: "scout" / "research" / "decision" / "position-review"
    label: str  # human-readable
    description: str
    enabled: bool
    cadence: str  # "every 90s", "on-demand", etc.
    last_run_at: datetime | None = None
    calls_24h: int = 0


class AgentRosterOut(BaseModel):
    generated_at: datetime
    roles: list[AgentRoleOut] = Field(default_factory=list)


class AgentTaskStep(BaseModel):
    """One action the agent took — a tool call extracted from its LLM
    response body. ``terminal`` marks the commit-type tools (report_finding,
    propose_structure, propose_trade, emit_candidates) that conclude the
    agent's work for this cycle."""

    tool: str
    summary: str = ""
    terminal: bool = False
    ts: datetime | None = None


class CycleAgentOut(BaseModel):
    """One agent's participation in a single cycle."""

    agent_id: str
    role: str | None = None  # inferred from purpose
    calls: int
    total_tokens: int
    cost_usd: float
    first_call_at: datetime
    last_call_at: datetime
    # Free-text hint of the agent's focus (e.g. symbol for researchers)
    focus: str | None = None
    # One-line status suitable for a table row — e.g. "researching BMI →
    # report_finding: bullish (0.75)" or "in-flight: web_search".
    task_summary: str | None = None
    # Ordered list of tool calls so the UI can render a trail.
    task_trail: list[AgentTaskStep] = Field(default_factory=list)


class CycleOut(BaseModel):
    """One TradingLoop tick (or scout scan) — the unit the UI groups by.

    Each cycle runs a swarm: scout may feed candidates, orchestrator
    fans out N research agents (one per symbol), then the decision
    agent reads their findings and commits a trade proposal.
    """

    cycle_id: str
    kind: str  # "decision" | "scout" | "legacy"
    started_at: datetime
    ended_at: datetime
    elapsed_sec: float
    total_calls: int
    total_tokens: int
    total_cost_usd: float
    decision_id: int | None = None
    decision_market: str | None = None
    decision_outcome: str | None = None  # "executed" | "approved" | "rejected" | "hold" | "market_closed"
    decision_symbol: str | None = None
    decision_rationale: str | None = None
    agents: list[CycleAgentOut] = Field(default_factory=list)


class CyclesOverviewOut(BaseModel):
    window_hours: int
    total_cycles: int
    total_calls: int
    total_cost_usd: float
    cycles: list[CycleOut] = Field(default_factory=list)
    generated_at: datetime


class LlmUsageSummaryOut(BaseModel):
    window_hours: int
    total_calls: int
    total_tokens: int
    total_cost_usd: float
    by_model: list[LlmUsageBucket]
    recent: list[LlmUsageRowOut]
    generated_at: datetime
