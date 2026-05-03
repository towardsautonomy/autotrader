"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AgentSummary,
  AgentTaskStep,
  AgentsOverview,
  Cycle,
  CycleAgent,
  CyclesOverview,
  LlmCallDetail,
  LlmUsageRow,
  api,
} from "@/lib/api";
import { LlmCallView } from "@/components/AgentSwarm";
import AgentsRosterSection from "@/components/AgentsRosterSection";
import RateCardSection from "@/components/RateCardSection";
import { usePagination } from "@/components/Pager";
import { fmtDateTime, fmtTimeHM } from "@/lib/time";
import { useRefreshOnResume } from "@/lib/useRefreshOnResume";

const WINDOWS = [
  { label: "1h", hours: 1 },
  { label: "6h", hours: 6 },
  { label: "24h", hours: 24 },
  { label: "7d", hours: 24 * 7 },
];

export default function AgentsPage() {
  const [overview, setOverview] = useState<AgentsOverview | null>(null);
  const [cycles, setCycles] = useState<CyclesOverview | null>(null);
  const [hours, setHours] = useState(24);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      Promise.all([api.agentsOverview(hours), api.cyclesOverview(hours, 100)])
        .then(([a, c]) => {
          setOverview(a);
          setCycles(c);
          setError(null);
        })
        .catch((e) => setError(String(e))),
    [hours],
  );

  useEffect(() => {
    load();
    const i = setInterval(load, 10_000);
    return () => clearInterval(i);
  }, [load]);

  useRefreshOnResume(load);

  if (error && !overview)
    return (
      <div className="frame p-4 text-danger text-sm">
        <span className="text-text-faint">[err]</span> {error}
      </div>
    );
  if (!overview || !cycles)
    return (
      <div className="text-text-dim text-sm">
        <span className="blink text-accent">▊</span> loading agents...
      </div>
    );

  return (
    <div className="space-y-5">
      <header className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <span className="text-accent">▸</span>
            agent_swarm
            <span className="text-text-faint text-xs">// orchestration</span>
          </h1>
          <p className="text-xs text-text-dim mt-1 leading-relaxed max-w-2xl">
            Each cycle is one decision tick. The scout feeds candidates,
            the orchestrator fans out one research agent per focus symbol,
            and the decision agent reads their findings. Click a cycle to
            expand the swarm hierarchy. Auto-refreshes every 10s.
          </p>
        </div>
        <div className="flex gap-1 text-[11px] uppercase tracking-widest">
          {WINDOWS.map((w) => (
            <button
              key={w.hours}
              onClick={() => setHours(w.hours)}
              className={
                "px-2 py-1 border " +
                (hours === w.hours
                  ? "border-accent/60 text-accent bg-accent/10"
                  : "border-border text-text-dim hover:border-accent/30")
              }
            >
              {w.label}
            </button>
          ))}
        </div>
      </header>

      <section className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Metric
          label="ACTIVE_NOW"
          value={liveAgentCount(overview.agents).toString()}
          live={liveAgentCount(overview.agents) > 0}
        />
        <Metric label="CYCLES" value={cycles.total_cycles.toString()} />
        <Metric label="AGENTS" value={overview.total_agents.toString()} />
        <Metric label="CALLS" value={overview.total_calls.toString()} />
        <Metric
          label="TOTAL_COST"
          value={`$${overview.total_cost_usd.toFixed(4)}`}
        />
      </section>

      <AgentsRosterSection />

      <CyclesSection cycles={cycles.cycles} />

      <section className="frame p-4 space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm uppercase tracking-widest text-text-dim">
            <span className="text-accent">▸</span> agent_activity
            <span className="text-text-faint text-[10px] ml-2">
              // aggregate across all cycles
            </span>
          </h2>
          <span className="text-[10px] text-text-faint tabular">
            [{overview.agents.length.toString().padStart(2, "0")}]
          </span>
        </div>
        {overview.agents.length === 0 ? (
          <p className="text-text-faint text-xs py-4">
            no agent activity in the last {overview.window_hours}h.
          </p>
        ) : (
          <div className="divide-y divide-border/40">
            {overview.agents.map((a) => (
              <AgentRow
                key={a.agent_id}
                a={a}
                open={selected === a.agent_id}
                onToggle={() =>
                  setSelected((s) => (s === a.agent_id ? null : a.agent_id))
                }
              />
            ))}
          </div>
        )}
      </section>

      <RateCardSection hours={hours} />
    </div>
  );
}

function CyclesSection({ cycles }: { cycles: Cycle[] }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  const { visible, totalKept, truncated, Pager } = usePagination(cycles);

  // If an expanded card scrolls off the current page, collapse it.
  useEffect(() => {
    if (expanded && !visible.some((c) => c.cycle_id === expanded)) {
      setExpanded(null);
    }
  }, [expanded, visible]);

  return (
    <section className="frame p-4 space-y-2">
      <div className="flex items-center justify-between">
        <h2 className="text-sm uppercase tracking-widest text-text-dim">
          <span className="text-accent">▸</span> recent_cycles
          <span className="text-text-faint text-[10px] ml-2">
            // one per tick — scout · research-&#123;sym&#125; · decision
          </span>
        </h2>
        <span className="text-[10px] text-text-faint tabular">
          [{totalKept.toString().padStart(3, "0")}
          {truncated ? "+" : ""}]
        </span>
      </div>
      {totalKept === 0 ? (
        <p className="text-text-faint text-xs py-4">
          no cycles in window. the swarm runs every 5 min during market
          hours (decision cycles) + every 2 min (scout scans).
        </p>
      ) : (
        <>
          <div className="divide-y divide-border/40">
            {visible.map((c) => (
              <CycleCard
                key={c.cycle_id}
                c={c}
                open={expanded === c.cycle_id}
                onToggle={() =>
                  setExpanded((s) => (s === c.cycle_id ? null : c.cycle_id))
                }
              />
            ))}
          </div>
          <Pager />
        </>
      )}
    </section>
  );
}

function CycleCard({
  c,
  open,
  onToggle,
}: {
  c: Cycle;
  open: boolean;
  onToggle: () => void;
}) {
  const researchers = c.agents.filter((a) => a.agent_id.startsWith("research-"));
  const hasScout = c.agents.some((a) => a.agent_id === "scout");
  const hasDecision = c.agents.some((a) => a.agent_id === "decision");

  // A cycle is "live" if its last LLM call was very recent AND it hasn't
  // settled into a terminal decision outcome yet. We treat a decision
  // cycle without a logged decision row as still-in-flight.
  const lastActivityAgeSec = secondsSince(c.ended_at);
  const settled =
    c.kind === "decision"
      ? c.decision_id !== null
      : true; // scout & legacy resolve on their last call
  const isLive =
    lastActivityAgeSec !== null &&
    lastActivityAgeSec < 45 &&
    !(settled && lastActivityAgeSec > 15);

  const outcomeTone =
    c.decision_outcome === "executed"
      ? "text-accent"
      : c.decision_outcome === "rejected"
        ? "text-danger"
        : c.decision_outcome === "market_closed"
          ? "text-warn"
          : "text-text-dim";

  const kindPill =
    c.kind === "scout"
      ? { label: "SCOUT", tone: "text-accent border-accent/40 bg-accent/5" }
      : c.kind === "legacy"
        ? { label: "LEGACY", tone: "text-text-dim border-border bg-bg-panel/40" }
        : { label: "DECISION", tone: "text-warn border-warn/40 bg-warn/5" };

  const liveFrame = isLive
    ? "border-l-2 border-l-accent bg-accent/[0.04] shadow-[inset_4px_0_0_0_rgba(34,211,155,0.25)]"
    : "border-l-2 border-l-transparent";

  const outcomeLabel =
    isLive && c.kind === "decision" && !c.decision_id
      ? "running"
      : c.decision_outcome
        ? `${c.decision_outcome}${c.decision_symbol ? ` ${c.decision_symbol}` : ""}`
        : c.kind === "scout"
          ? "scan"
          : "—";

  return (
    <div className={liveFrame}>
      <button
        onClick={onToggle}
        className="w-full py-2 px-2 text-xs tabular hover:bg-accent/5 text-left block"
      >
        {/* Row 1: pill / composition / outcome — always visible */}
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-accent shrink-0">{open ? "▾" : "▸"}</span>
          <span
            className={`px-1.5 py-0.5 border text-[9px] uppercase tracking-widest shrink-0 ${kindPill.tone}`}
          >
            {kindPill.label}
          </span>
          {isLive && <LivePulse />}
          <span className="text-[10px] uppercase tracking-widest truncate min-w-0">
            {hasScout && <span className="mr-1 text-accent">scout</span>}
            {researchers.length > 0 && (
              <span className="mr-1 text-text">
                × {researchers.length} research
              </span>
            )}
            {hasDecision && <span className="text-warn">decision</span>}
          </span>
          <span
            className={`ml-auto text-[10px] uppercase tracking-widest truncate max-w-[45%] ${outcomeTone}`}
          >
            {outcomeLabel}
          </span>
        </div>
        {/* Row 2: timestamp + metrics — wraps on narrow screens */}
        <div className="mt-1 flex items-center gap-x-3 gap-y-1 flex-wrap text-[10px] text-text-dim">
          <span className="truncate">{fmtDateTime(c.started_at)}</span>
          <span className="text-text-faint">·</span>
          <span>{c.elapsed_sec.toFixed(1)}s</span>
          <span className="text-text-faint">·</span>
          <span className="text-text">{c.total_calls} call{c.total_calls === 1 ? "" : "s"}</span>
          <span className="text-text-faint">·</span>
          <span>{c.total_tokens.toLocaleString()} tok</span>
        </div>
      </button>
      {open && <CycleDetail c={c} />}
    </div>
  );
}

function LivePulse() {
  return (
    <span
      title="active now"
      className="inline-flex items-center gap-1 text-[9px] uppercase tracking-widest text-accent"
    >
      <span className="relative inline-flex h-1.5 w-1.5">
        <span className="absolute inline-flex h-full w-full rounded-full bg-accent/60 animate-ping"></span>
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent"></span>
      </span>
      live
    </span>
  );
}

function secondsSince(iso: string | null): number | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  return (Date.now() - t) / 1000;
}

function CycleDetail({ c }: { c: Cycle }) {
  const scout = c.agents.find((a) => a.agent_id === "scout") || null;
  const researchers = c.agents
    .filter((a) => a.agent_id.startsWith("research-"))
    .sort((a, b) => a.first_call_at.localeCompare(b.first_call_at));
  const decision = c.agents.find((a) => a.agent_id === "decision") || null;
  const leftovers = c.agents.filter(
    (a) =>
      a.agent_id !== "scout" &&
      !a.agent_id.startsWith("research-") &&
      a.agent_id !== "decision",
  );

  return (
    <div className="px-2 pb-3 pt-1 space-y-3 text-[11px]">
      <div className="text-text-faint tabular">
        <span className="text-text-dim">cycle_id:</span> {c.cycle_id}
        {c.decision_id && (
          <span className="ml-3">
            <span className="text-text-dim">decision_id:</span> #{c.decision_id}
          </span>
        )}
        {c.decision_market && (
          <span className="ml-3 uppercase">
            <span className="text-text-dim">market:</span> {c.decision_market}
          </span>
        )}
      </div>

      {c.decision_rationale && (
        <div className="text-text leading-relaxed whitespace-pre-wrap border-l-2 border-accent/40 pl-2">
          {c.decision_rationale}
        </div>
      )}

      {scout && (
        <SwarmRow label="SCOUT">
          <AgentCard a={scout} />
        </SwarmRow>
      )}

      {researchers.length > 0 && (
        <SwarmRow label={`RESEARCH × ${researchers.length}`}>
          <div className="grid md:grid-cols-2 gap-2">
            {researchers.map((a) => (
              <AgentCard key={a.agent_id} a={a} />
            ))}
          </div>
        </SwarmRow>
      )}

      {decision && (
        <SwarmRow label="DECISION">
          <AgentCard a={decision} />
        </SwarmRow>
      )}

      {!scout && researchers.length === 0 && !decision && (
        <EmptySlot hint="no agent activity captured — raw prompts may be truncated" />
      )}

      {leftovers.length > 0 && (
        <SwarmRow label="OTHER">
          <div className="grid md:grid-cols-2 gap-2">
            {leftovers.map((a) => (
              <AgentCard key={a.agent_id} a={a} />
            ))}
          </div>
        </SwarmRow>
      )}
    </div>
  );
}

function SwarmRow({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="grid md:grid-cols-[80px_1fr] gap-2 items-start">
      <div className="text-[9px] uppercase tracking-widest text-text-faint pt-2">
        {label}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function AgentCard({ a }: { a: CycleAgent }) {
  const ageSec = secondsSince(a.last_call_at);
  const isLive = ageSec !== null && ageSec < 30;
  const committed = a.task_trail.some((t) => t.terminal);
  const tone = a.agent_id.startsWith("research-")
    ? "border-accent/40"
    : a.agent_id === "decision"
      ? "border-warn/40"
      : a.agent_id === "scout"
        ? "border-accent/40"
        : "border-border";
  const displayName = a.agent_id.startsWith("research-")
    ? `research · ${a.focus ?? a.agent_id}`
    : a.agent_id;

  return (
    <div
      className={
        "border bg-bg-panel/30 p-2 space-y-1 " +
        tone +
        (isLive ? " ring-1 ring-accent/60" : "")
      }
    >
      <div className="flex items-baseline gap-2 flex-wrap text-[11px] min-w-0">
        {isLive && (
          <span className="relative inline-flex h-1.5 w-1.5 shrink-0 self-center">
            <span className="absolute inline-flex h-full w-full rounded-full bg-accent/60 animate-ping"></span>
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent"></span>
          </span>
        )}
        <span className="text-accent font-semibold tabular truncate max-w-full">
          {displayName}
        </span>
        <span className="text-text-faint tabular text-[10px]">
          {a.calls}c · {(a.total_tokens / 1000).toFixed(1)}k · ${a.cost_usd.toFixed(4)}
        </span>
        <span className="text-text-faint tabular text-[10px] sm:ml-auto">
          {fmtTimeHM(a.first_call_at)} → {fmtTimeHM(a.last_call_at)}
        </span>
      </div>

      {a.task_summary && (
        <div
          className={
            "text-[11px] leading-snug " +
            (committed ? "text-text" : "text-warn")
          }
        >
          <span className="text-text-faint">→</span> {a.task_summary}
        </div>
      )}

      {a.task_trail.length > 0 && <TaskTrail steps={a.task_trail} />}
    </div>
  );
}

function TaskTrail({ steps }: { steps: AgentTaskStep[] }) {
  return (
    <details className="text-[10px]">
      <summary className="cursor-pointer text-text-faint hover:text-accent select-none">
        <span className="text-text-faint">▸</span> trail
        <span className="text-text-faint ml-1">[{steps.length}]</span>
      </summary>
      <ol className="mt-1 space-y-0.5 border-l border-border/50 pl-2 text-text-dim">
        {steps.map((s, i) => (
          <li key={i} className="leading-tight">
            <span
              className={
                s.terminal
                  ? "text-accent font-semibold"
                  : s.tool === "web_search"
                    ? "text-warn"
                    : s.tool === "fetch_url"
                      ? "text-text"
                      : "text-text-dim"
              }
            >
              {s.tool}
            </span>
            {s.summary && <span className="text-text"> — {s.summary}</span>}
          </li>
        ))}
      </ol>
    </details>
  );
}

function SwarmSlot({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <div className="text-[9px] uppercase tracking-widest text-text-faint mb-1">
        {label}
      </div>
      {children}
    </div>
  );
}

function Arrow() {
  return (
    <div className="text-text-faint text-lg self-center hidden md:block">→</div>
  );
}

function EmptySlot({ hint }: { hint: string }) {
  return (
    <div className="border border-dashed border-border/60 text-text-faint text-[10px] px-2 py-1 italic">
      {hint}
    </div>
  );
}

function AgentPill({ a }: { a: CycleAgent }) {
  const isResearch = a.agent_id.startsWith("research-");
  const ageSec = secondsSince(a.last_call_at);
  const isLive = ageSec !== null && ageSec < 30;
  const baseTone = isResearch
    ? "border-accent/40 bg-accent/5"
    : a.agent_id === "decision"
      ? "border-warn/40 bg-warn/5"
      : a.agent_id === "scout"
        ? "border-accent/40 bg-accent/5"
        : "border-border bg-bg-panel/40";
  const liveTone = isLive
    ? " ring-1 ring-accent/60 shadow-[0_0_6px_rgba(34,211,155,0.35)]"
    : "";
  const title =
    `${a.agent_id} · ${a.calls} call${a.calls === 1 ? "" : "s"} · ` +
    `${a.total_tokens.toLocaleString()} tok · ` +
    `${fmtTimeHM(a.first_call_at)} → ${fmtTimeHM(a.last_call_at)}` +
    (isLive ? " · ACTIVE" : "");
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 border px-1.5 py-0.5 tabular text-[10px] whitespace-nowrap ${baseTone}${liveTone}`}
    >
      {isLive && (
        <span className="relative inline-flex h-1.5 w-1.5 shrink-0">
          <span className="absolute inline-flex h-full w-full rounded-full bg-accent/60 animate-ping"></span>
          <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent"></span>
        </span>
      )}
      <span className="text-accent">
        {isResearch && a.focus
          ? a.focus
          : a.agent_id === "decision"
            ? "decision"
            : a.agent_id === "scout"
              ? "scout"
              : a.agent_id}
      </span>
      <span className="text-text-faint">·</span>
      <span className="text-text-dim">{a.calls}c</span>
      <span className="text-text-faint">·</span>
      <span className="text-text-dim">{(a.total_tokens / 1000).toFixed(1)}k</span>
    </span>
  );
}

function Metric({
  label,
  value,
  live = false,
}: {
  label: string;
  value: string;
  live?: boolean;
}) {
  return (
    <div
      className={
        "border p-3 " +
        (live
          ? "border-accent/60 bg-accent/[0.08]"
          : "border-border bg-bg-panel/50")
      }
    >
      <div className="text-[10px] uppercase tracking-widest text-text-dim flex items-center gap-1">
        {live && (
          <span className="relative inline-flex h-1.5 w-1.5">
            <span className="absolute inline-flex h-full w-full rounded-full bg-accent/60 animate-ping"></span>
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent"></span>
          </span>
        )}
        {label}
      </div>
      <div
        className={
          "text-lg font-semibold tabular mt-1 " +
          (live ? "text-accent" : "")
        }
      >
        {value}
      </div>
    </div>
  );
}

function liveAgentCount(agents: AgentSummary[]): number {
  const now = Date.now();
  return agents.filter((a) => {
    if (!a.last_call_at) return false;
    const t = new Date(a.last_call_at).getTime();
    if (Number.isNaN(t)) return false;
    return (now - t) / 1000 < 60;
  }).length;
}

function AgentRow({
  a,
  open,
  onToggle,
}: {
  a: AgentSummary;
  open: boolean;
  onToggle: () => void;
}) {
  const last = a.last_call_at ? new Date(a.last_call_at) : null;
  const ageSec = last ? (Date.now() - last.getTime()) / 1000 : null;
  const ageTone =
    ageSec === null
      ? "text-text-faint"
      : ageSec < 120
        ? "text-accent"
        : ageSec < 600
          ? "text-warn"
          : "text-text-dim";
  const isLive = ageSec !== null && ageSec < 60;
  const liveFrame = isLive
    ? "border-l-2 border-l-accent bg-accent/[0.04]"
    : "border-l-2 border-l-transparent";

  const ageLabel =
    ageSec === null
      ? "—"
      : ageSec < 60
        ? `${ageSec.toFixed(0)}s ago`
        : ageSec < 3600
          ? `${(ageSec / 60).toFixed(0)}m ago`
          : `${(ageSec / 3600).toFixed(1)}h ago`;

  return (
    <div className={liveFrame}>
      <button
        onClick={onToggle}
        className="w-full py-2 px-2 text-xs tabular hover:bg-accent/5 text-left block"
      >
        {/* Row 1: caret + id + role + live-ness */}
        <div className="flex items-center gap-2 flex-wrap min-w-0">
          <span className="text-accent shrink-0">{open ? "▾" : "▸"}</span>
          {isLive && (
            <span className="relative inline-flex h-1.5 w-1.5 shrink-0">
              <span className="absolute inline-flex h-full w-full rounded-full bg-accent/60 animate-ping"></span>
              <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent"></span>
            </span>
          )}
          <span className="text-accent font-semibold truncate min-w-0">
            {a.agent_id}
          </span>
          {a.role && (
            <span className="text-text-dim uppercase tracking-widest text-[10px]">
              {a.role}
            </span>
          )}
          <span className={`ml-auto text-[10px] ${ageTone}`}>{ageLabel}</span>
        </div>
        {/* Row 2: metrics wrap */}
        <div className="mt-1 flex items-center gap-x-3 gap-y-1 flex-wrap text-[10px] text-text-dim">
          <span className="text-text">{a.calls} call{a.calls === 1 ? "" : "s"}</span>
          <span className="text-text-faint">·</span>
          <span>{a.total_tokens.toLocaleString()} tok</span>
          <span className="text-text-faint">·</span>
          <span className="text-text">${a.cost_usd.toFixed(4)}</span>
        </div>
      </button>
      {open && <AgentCallsPanel agentId={a.agent_id} />}
    </div>
  );
}

function AgentCallsPanel({ agentId }: { agentId: string }) {
  const [calls, setCalls] = useState<LlmUsageRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [expandedCall, setExpandedCall] = useState<number | null>(null);

  useEffect(() => {
    setCalls(null);
    const realId = agentId === "(unlabelled)" ? "" : agentId;
    const params = realId ? { agent_id: realId, limit: 50 } : { limit: 50 };
    api
      .llmCalls(params)
      .then((rows) => {
        const filtered = realId
          ? rows
          : rows.filter((r) => !r.agent_id);
        setCalls(filtered);
      })
      .catch((e) => setError(String(e)));
  }, [agentId]);

  if (error)
    return <div className="text-xs text-danger px-4 py-2">{error}</div>;
  if (calls === null)
    return <div className="text-xs text-text-faint px-4 py-2">loading...</div>;
  if (calls.length === 0)
    return (
      <div className="text-xs text-text-faint px-4 py-2">
        no call rows in window.
      </div>
    );

  return (
    <div className="px-4 pb-3 space-y-1">
      {calls.map((c) => (
        <CallRow
          key={c.id}
          c={c}
          open={expandedCall === c.id}
          onToggle={() =>
            setExpandedCall((s) => (s === c.id ? null : c.id))
          }
        />
      ))}
    </div>
  );
}

function CallRow({
  c,
  open,
  onToggle,
}: {
  c: LlmUsageRow;
  open: boolean;
  onToggle: () => void;
}) {
  const [detail, setDetail] = useState<LlmCallDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open || detail || loading) return;
    setLoading(true);
    api
      .llmCall(c.id)
      .then((d) => {
        setDetail(d);
        setError(null);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [open, c.id, detail, loading]);

  return (
    <div className="border border-border/40 bg-bg-panel/30">
      <button
        onClick={onToggle}
        className="w-full py-1.5 px-2 text-[11px] tabular hover:bg-accent/5 text-left block"
      >
        <div className="flex items-center gap-2 flex-wrap min-w-0">
          <span className="text-text-faint shrink-0">#{c.id}</span>
          <span className="text-text-dim truncate min-w-0">
            {fmtDateTime(c.created_at)}
          </span>
          <span className="text-text-faint shrink-0">r{c.round_idx ?? 0}</span>
          <span className="ml-auto text-text shrink-0">
            {c.total_tokens} tok · ${c.cost_usd.toFixed(4)}
          </span>
        </div>
        <div className="text-text-dim text-[10px] truncate mt-0.5">
          {c.provider}::{c.model}
        </div>
      </button>
      {open && (
        <div className="border-t border-border/40 p-2">
          {loading && <div className="text-text-faint text-xs">loading...</div>}
          {error && <div className="text-danger text-xs">{error}</div>}
          {detail && <LlmCallView detail={detail} />}
        </div>
      )}
    </div>
  );
}
