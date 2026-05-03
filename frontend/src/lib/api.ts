const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:3003/api";
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || "";

export const API_CONFIG = { url: API_URL, key: API_KEY };

export type Mode = "PAPER" | "LIVE";

export interface PositionVerdict {
  status: "HOLD" | "APPROVED" | "EXECUTED" | "REJECTED";
  action: string | null;
  rationale: string | null;
  rejection_code: string | null;
  created_at: string;
}

export interface Position {
  market: string;
  symbol: string;
  size_usd: number;
  entry_price: number;
  current_price: number;
  unrealized_pnl: number;
  last_verdict: PositionVerdict | null;
}

export interface Account {
  mode: Mode;
  active_mode: Mode;
  trading_enabled: boolean;
  agents_paused: boolean;
  pause_when_market_closed: boolean;
  cash_balance: number;
  total_exposure: number;
  total_equity: number;
  day_realized_pnl: number;
  cumulative_pnl: number;
  daily_trade_count: number;
  positions: Position[];
}

export type RiskTier = "conservative" | "moderate" | "aggressive";

export interface RiskConfig {
  id?: number;
  is_active?: boolean;
  budget_cap: number;
  max_position_pct: number;
  max_concurrent_positions: number;
  max_daily_trades: number;
  daily_loss_cap_pct: number;
  max_drawdown_pct: number;
  default_stop_loss_pct: number;
  default_take_profit_pct: number;
  min_trade_size_usd: number;
  blacklist: string[];
  risk_tier: RiskTier;
  max_option_loss_per_spread_pct: number;
  earnings_blackout_days: number;
  max_stop_loss_pct: number;
  paper_cost_bps: number;
  pdt_day_trade_count_5bd: number;
  min_open_confidence: number;
  min_reward_risk_ratio: number;
}

export type ConstraintSeverity = "error" | "warn" | "info";

export interface ConstraintDefinition {
  key: string;
  severity: ConstraintSeverity;
  title: string;
  description: string;
  remedy: string;
}

export interface ConstraintViolation extends ConstraintDefinition {}

export interface RiskConfigWarnings {
  definitions: ConstraintDefinition[];
  violations: ConstraintViolation[];
}

export interface GeneratedRiskConfig {
  budget_cap: number;
  max_position_pct: number;
  max_concurrent_positions: number;
  max_daily_trades: number;
  daily_loss_cap_pct: number;
  max_drawdown_pct: number;
  default_stop_loss_pct: number;
  default_take_profit_pct: number;
  max_stop_loss_pct: number;
  min_trade_size_usd: number;
  max_option_loss_per_spread_pct: number;
  earnings_blackout_days: number;
  paper_cost_bps: number;
  pdt_day_trade_count_5bd: number;
  risk_tier: RiskTier;
  blacklist: string[];
  rationale: string;
}

export interface ResearchArtifact {
  tool: string;
  arguments: Record<string, unknown>;
  result_preview: string;
  result_count: number;
}

export interface Decision {
  id: number;
  created_at: string;
  market: string;
  model: string;
  approved: boolean;
  executed: boolean;
  rejection_code: string | null;
  rejection_reason: string | null;
  rationale: string | null;
  proposal_json: Record<string, unknown> | null;
  execution_error: string | null;
  research: ResearchArtifact[] | null;
}

export interface Trade {
  id: number;
  created_at: string;
  market: string;
  symbol: string;
  action: string;
  size_usd: number;
  entry_price: number | null;
  exit_price: number | null;
  realized_pnl_usd: number;
  status: string;
  paper_mode: boolean;
  opened_at: string | null;
  closed_at: string | null;
}

export type ActivitySeverity =
  | "debug"
  | "info"
  | "warn"
  | "error"
  | "success";

export interface ActivityEvent {
  id: number;
  ts: string;
  type: string;
  severity: ActivitySeverity;
  message: string;
  data?: Record<string, unknown>;
}

export interface ActivityRow {
  id: number;
  created_at: string;
  type: string;
  severity: ActivitySeverity;
  message: string;
  data: Record<string, unknown> | null;
}

export interface AIModelInfo {
  id: string;
  state: string | null;
  arch: string | null;
  quantization: string | null;
  max_context_length: number | null;
  loaded_context_length: number | null;
  capabilities: string[];
}

export interface GPUInfo {
  index: number;
  name: string;
  total_mb: number;
  used_mb: number;
  free_mb: number;
  utilization_pct: number | null;
}

export interface NewsItem {
  symbol: string | null;
  headline: string;
  summary: string;
  source: string;
  url: string;
  datetime: string;
}

export interface Quote {
  symbol: string;
  current: number;
  change: number;
  change_pct: number;
  open: number;
  high: number;
  low: number;
  prev_close: number;
  ts: string;
}

export interface SymbolDecision {
  id: number;
  created_at: string;
  action: string | null;
  approved: boolean;
  executed: boolean;
  rationale: string | null;
  rejection_code: string | null;
}

export interface SymbolIntel {
  symbol: string;
  quote: Quote | null;
  news: NewsItem[];
  position_size_usd: number | null;
  position_unrealized_pnl: number | null;
  last_decision: SymbolDecision | null;
}

export interface Mover {
  symbol: string;
  category: "gainer" | "loser" | "most_active";
  price: number | null;
  change: number | null;
  percent_change: number | null;
  volume: number | null;
  trade_count: number | null;
}

export interface Discovery {
  enabled: boolean;
  gainers: Mover[];
  losers: Mover[];
  most_active: Mover[];
  last_updated: string | null;
  fetched_at: string | null;
}

export interface Candidate {
  symbol: string;
  reason: "position" | "recent_approved" | "discovery" | "shortlist";
  note: string;
  rank: number;
}

export interface MarketIntel {
  candidates: Candidate[];
  symbols: SymbolIntel[];
  market_news: NewsItem[];
  discovery: Discovery;
  checked_at: string;
  news_enabled: boolean;
}

export interface EquityPoint {
  ts: string;
  cumulative_pnl: number;
}

export interface DailyPnlPoint {
  day: string;
  realized_pnl: number;
  trade_count: number;
  wins: number;
  losses: number;
}

export interface DecisionStats {
  total: number;
  approved: number;
  rejected: number;
  executed: number;
}

export interface DecisionBucketPoint {
  day: string;
  approved: number;
  rejected: number;
  executed: number;
}

export interface WinRateStats {
  wins: number;
  losses: number;
  breakeven: number;
  total: number;
  win_rate_pct: number;
  avg_win_usd: number;
  avg_loss_usd: number;
}

export interface TradeOutcomePoint {
  id: number;
  symbol: string;
  action: string;
  closed_at: string;
  realized_pnl_usd: number;
  size_usd: number;
}

export interface RollingWinRatePoint {
  trade_index: number;
  closed_at: string;
  window_size: number;
  win_rate_pct: number;
}

export interface LlmCostVsPnlPoint {
  day: string;
  llm_cost_usd: number;
  realized_pnl_usd: number;
}

export interface HoldTimeBucket {
  bucket: string;
  wins: number;
  losses: number;
  count: number;
}

export interface PnlBySymbolBar {
  symbol: string;
  realized_pnl_usd: number;
  trade_count: number;
  wins: number;
  losses: number;
}

export interface DrawdownPoint {
  ts: string;
  drawdown_usd: number;
}

export interface PerformanceStats {
  max_drawdown_usd: number;
  max_drawdown_pct: number;
  profit_factor: number | null;
  expectancy_usd: number;
  current_streak: number;
  longest_win_streak: number;
  longest_loss_streak: number;
}

export interface DailyEquityPoint {
  day: string;
  cumulative_pnl: number;
}

export interface HourBucket {
  hour: number;
  wins: number;
  losses: number;
  realized_pnl_usd: number;
}

export interface AiQualityStats {
  executed_decisions: number;
  avg_confidence_wins: number | null;
  avg_confidence_losses: number | null;
  avg_confidence_rejected: number | null;
  median_exec_latency_sec: number | null;
  total_llm_spend_usd: number;
  cost_per_executed_decision_usd: number | null;
  cost_per_dollar_pnl: number | null;
}

export interface Analytics {
  equity_curve: EquityPoint[];
  equity_curve_daily: DailyEquityPoint[];
  drawdown_curve: DrawdownPoint[];
  daily_pnl: DailyPnlPoint[];
  decision_stats: DecisionStats;
  decision_timeline: DecisionBucketPoint[];
  win_rate: WinRateStats;
  performance: PerformanceStats;
  ai_quality: AiQualityStats;
  trade_outcomes: TradeOutcomePoint[];
  rolling_win_rate: RollingWinRatePoint[];
  llm_cost_vs_pnl: LlmCostVsPnlPoint[];
  hold_time_distribution: HoldTimeBucket[];
  hour_of_day_distribution: HourBucket[];
  pnl_by_symbol: PnlBySymbolBar[];
  budget_cap_usd: number;
  generated_at: string;
}

export interface BenchmarkBar {
  day: string;
  close: number;
}

export interface BenchmarkSeries {
  symbol: string;
  bars: BenchmarkBar[];
  error: string | null;
}

export interface Benchmark {
  start: string;
  end: string;
  series: BenchmarkSeries[];
}

export interface OptionContract {
  symbol: string;
  side: "call" | "put";
  strike: number;
  expiry: string;
  bid: number | null;
  ask: number | null;
  mid: number | null;
  last: number | null;
  implied_volatility: number | null;
  delta: number | null;
  gamma: number | null;
  theta: number | null;
  vega: number | null;
  open_interest: number | null;
  volume: number | null;
}

export interface OptionChain {
  underlying: string;
  expiries: string[];
  contracts: OptionContract[];
  fetched_at: string;
}

export interface LlmRateEntry {
  prompt_per_1k_usd: number;
  completion_per_1k_usd: number;
}

export interface LlmRateCard {
  id: number;
  rates: Record<string, LlmRateEntry>;
  updated_at: string | null;
}

export interface LlmUsageRow {
  id: number;
  created_at: string;
  provider: string;
  model: string;
  purpose: string | null;
  agent_id: string | null;
  round_idx: number | null;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cost_usd: number;
}

export interface LlmCallDetail extends LlmUsageRow {
  prompt_messages: Record<string, unknown>[] | null;
  response_body: Record<string, unknown> | null;
}

export interface LlmUsageBucket {
  key: string;
  provider: string;
  model: string;
  calls: number;
  total_tokens: number;
  cost_usd: number;
}

export interface AgentSummary {
  agent_id: string;
  role: string | null;
  calls: number;
  total_tokens: number;
  cost_usd: number;
  first_call_at: string | null;
  last_call_at: string | null;
  last_model: string | null;
  last_purpose: string | null;
}

export interface AgentsOverview {
  window_hours: number;
  total_agents: number;
  total_calls: number;
  total_cost_usd: number;
  agents: AgentSummary[];
  generated_at: string;
}

export interface AgentRole {
  role: string;
  label: string;
  description: string;
  enabled: boolean;
  cadence: string;
  last_run_at: string | null;
  calls_24h: number;
}

export interface AgentRoster {
  generated_at: string;
  roles: AgentRole[];
}

export interface AgentTaskStep {
  tool: string;
  summary: string;
  terminal: boolean;
  ts: string | null;
}

export interface CycleAgent {
  agent_id: string;
  role: string | null;
  calls: number;
  total_tokens: number;
  cost_usd: number;
  first_call_at: string;
  last_call_at: string;
  focus: string | null;
  task_summary: string | null;
  task_trail: AgentTaskStep[];
}

export interface Cycle {
  cycle_id: string;
  kind: "decision" | "scout" | "legacy";
  started_at: string;
  ended_at: string;
  elapsed_sec: number;
  total_calls: number;
  total_tokens: number;
  total_cost_usd: number;
  decision_id: number | null;
  decision_market: string | null;
  decision_outcome:
    | "executed"
    | "approved_not_executed"
    | "rejected"
    | "hold"
    | "market_closed"
    | null;
  decision_symbol: string | null;
  decision_rationale: string | null;
  agents: CycleAgent[];
}

export interface CyclesOverview {
  window_hours: number;
  total_cycles: number;
  total_calls: number;
  total_cost_usd: number;
  cycles: Cycle[];
  generated_at: string;
}

export interface LlmUsageSummary {
  window_hours: number;
  total_calls: number;
  total_tokens: number;
  total_cost_usd: number;
  by_model: LlmUsageBucket[];
  recent: LlmUsageRow[];
  generated_at: string;
}

export interface ScoutCandidateApi {
  symbol: string;
  source: string;
  note: string;
  score: number | null;
  added_at: string;
  age_sec: number;
}

export interface ScoutQueue {
  enabled: boolean;
  queue_size: number;
  ttl_sec: number;
  daily_llm_budget_usd: number;
  daily_llm_spent_usd: number;
  candidates: ScoutCandidateApi[];
}

export interface AIStatus {
  provider: string;
  model_configured: string;
  base_url: string;
  reachable: boolean;
  reachable_error: string | null;
  loaded_model_id: string | null;
  configured_model_state: string | null;
  models: AIModelInfo[];
  gpus: GPUInfo[];
  checked_at: string;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? "GET").toUpperCase();
  const idempotent = method === "GET" || method === "HEAD";

  const doFetch = () =>
    fetch(`${API_URL}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": API_KEY,
        ...(init.headers || {}),
      },
      cache: "no-store",
    });

  // Mobile cold-starts (especially iOS Safari after the app was
  // backgrounded) drop the first fetch with a TypeError: Failed to fetch.
  // One quiet retry turns the blank-until-pull-to-refresh experience into
  // a small hiccup. Only applied to idempotent methods to avoid
  // double-submitting orders.
  let res: Response;
  try {
    res = await doFetch();
  } catch (err) {
    if (!idempotent) throw err;
    await new Promise((r) => setTimeout(r, 1500));
    res = await doFetch();
  }
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export interface HealthResponse {
  status: string;
}

export interface SystemStatusResponse {
  status: string;
  mode: Mode;
  active_mode: Mode;
  pending_restart: boolean;
}

export interface SetModeResponse {
  mode: Mode;
  active_mode: Mode;
  pending_restart: boolean;
}

export const api = {
  health: () => request<HealthResponse>("/health"),
  systemStatus: () => request<SystemStatusResponse>("/system/status"),
  setMode: (target: Mode, confirmPhrase = "") =>
    request<SetModeResponse>("/mode", {
      method: "POST",
      body: JSON.stringify({
        target_mode: target,
        confirm_phrase: confirmPhrase,
      }),
    }),
  account: () => request<Account>("/account"),
  decisions: (limit = 50) => request<Decision[]>(`/decisions?limit=${limit}`),
  trades: (limit = 100) => request<Trade[]>(`/trades?limit=${limit}`),
  riskConfig: () => request<RiskConfig>("/risk-config"),
  updateRiskConfig: (cfg: RiskConfig) =>
    request<RiskConfig>("/risk-config", {
      method: "PUT",
      body: JSON.stringify(cfg),
    }),
  riskConfigWarnings: () =>
    request<RiskConfigWarnings>("/risk-config/warnings"),
  evaluateRiskConfig: (cfg: RiskConfig) =>
    request<{ violations: ConstraintViolation[] }>("/risk-config/evaluate", {
      method: "POST",
      body: JSON.stringify(cfg),
    }),
  generateRiskConfig: (budget_cap: number, preference?: string) =>
    request<GeneratedRiskConfig>("/risk-config/generate", {
      method: "POST",
      body: JSON.stringify({
        budget_cap,
        preference: preference || null,
      }),
    }),
  killSwitch: (reason = "manual") =>
    request<{ trading_enabled: boolean }>("/kill-switch", {
      method: "POST",
      body: JSON.stringify({ confirm: "KILL", reason }),
    }),
  unpause: () =>
    request<{ trading_enabled: boolean }>("/unpause", { method: "POST" }),
  pauseAgents: () =>
    request<{ agents_paused: boolean }>("/agents/pause", { method: "POST" }),
  setPauseWhenClosed: (enabled: boolean) =>
    request<{ pause_when_market_closed: boolean }>(
      "/agents/pause-when-closed",
      { method: "POST", body: JSON.stringify({ enabled }) },
    ),
  resumeAgents: () =>
    request<{ agents_paused: boolean }>("/agents/resume", { method: "POST" }),
  closeTrade: (id: number) =>
    request<{ trade_id: number; symbol: string; success: boolean }>(
      `/trades/${id}/close`,
      { method: "POST" },
    ),
  closeAllPositions: () =>
    request<{
      attempted: number;
      closed: number;
      results: { trade_id: number; symbol: string; success: boolean; error?: string }[];
    }>("/positions/close-all", { method: "POST" }),
  cancelAllOrders: () =>
    request<{ cancelled: number; local_reconciled: number }>(
      "/orders/cancel-all",
      { method: "POST" },
    ),
  activity: (limit = 200) =>
    request<ActivityRow[]>(`/activity?limit=${limit}`),
  aiStatus: () => request<AIStatus>("/ai/status"),
  intel: () => request<MarketIntel>("/intel"),
  analytics: () => request<Analytics>("/analytics"),
  benchmark: (symbols: string[], start: string, end: string) => {
    const q = new URLSearchParams({
      symbols: symbols.join(","),
      start,
      end,
    });
    return request<Benchmark>(`/analytics/benchmark?${q.toString()}`);
  },
  optionsChain: (symbol: string) =>
    request<OptionChain>(`/options/${encodeURIComponent(symbol.toUpperCase())}`),
  llmRateCard: () => request<LlmRateCard>("/llm/rate-card"),
  updateLlmRateCard: (rates: Record<string, LlmRateEntry>) =>
    request<LlmRateCard>("/llm/rate-card", {
      method: "PUT",
      body: JSON.stringify({ rates }),
    }),
  llmUsage: (hours = 24) =>
    request<LlmUsageSummary>(`/llm/usage?hours=${hours}`),
  llmCalls: (
    params: {
      agent_id?: string;
      purpose?: string;
      decision_id?: number;
      limit?: number;
    } = {}
  ) => {
    const q = new URLSearchParams();
    if (params.agent_id) q.set("agent_id", params.agent_id);
    if (params.purpose) q.set("purpose", params.purpose);
    if (params.decision_id !== undefined)
      q.set("decision_id", String(params.decision_id));
    if (params.limit !== undefined) q.set("limit", String(params.limit));
    const qs = q.toString();
    return request<LlmUsageRow[]>(`/llm/calls${qs ? `?${qs}` : ""}`);
  },
  llmCall: (id: number) => request<LlmCallDetail>(`/llm/calls/${id}`),
  agentsOverview: (hours = 24) =>
    request<AgentsOverview>(`/agents?hours=${hours}`),
  agentsRoster: () => request<AgentRoster>(`/agents/roster`),

  cyclesOverview: (hours = 24, limit = 50) =>
    request<CyclesOverview>(`/cycles?hours=${hours}&limit=${limit}`),
  scoutQueue: () => request<ScoutQueue>("/scout/queue"),

  researchConversations: () =>
    request<ResearchConversationSummary[]>("/research/conversations"),
  researchConversation: (id: number) =>
    request<ResearchConversationDetail>(`/research/conversations/${id}`),
  createResearchConversation: (title?: string) =>
    request<ResearchConversationSummary>("/research/conversations", {
      method: "POST",
      body: JSON.stringify({ title: title || null }),
    }),
  deleteResearchConversation: (id: number) =>
    request<{ ok: boolean }>(`/research/conversations/${id}`, {
      method: "DELETE",
    }),
};

export interface ResearchMessageRow {
  id: number;
  role: "user" | "assistant" | "tool_call" | "tool_result";
  content: string;
  tool_name: string | null;
  tool_payload: Record<string, unknown> | null;
  created_at: string;
}

export interface ResearchConversationSummary {
  id: number;
  title: string;
  created_at: string;
  message_count: number;
}

export interface ResearchConversationDetail {
  id: number;
  title: string;
  created_at: string;
  messages: ResearchMessageRow[];
}

export function researchChatUrl(): string {
  const u = new URL(`${API_URL}/research/chat`);
  if (API_KEY) u.searchParams.set("api_key", API_KEY);
  return u.toString();
}

export function researchStreamUrl(
  conversationId: number,
  afterSeq: number = 0,
): string {
  const u = new URL(`${API_URL}/research/conversations/${conversationId}/stream`);
  if (API_KEY) u.searchParams.set("api_key", API_KEY);
  if (afterSeq > 0) u.searchParams.set("after_seq", String(afterSeq));
  return u.toString();
}

export function activityStreamUrl(): string {
  const u = new URL(`${API_URL}/events`);
  if (API_KEY) u.searchParams.set("api_key", API_KEY);
  return u.toString();
}
