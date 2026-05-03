"use client";

import { useEffect, useMemo, useState } from "react";
import {
  ActivityEvent,
  LlmCallDetail,
  activityStreamUrl,
  api,
} from "@/lib/api";
import { fmtTime } from "@/lib/time";

type AgentState = "running" | "done" | "failed";

interface TimelineEntry {
  ts: number;
  type: string;
  tool?: string;
  preview?: string;
  call_id?: number;
  round_idx?: number;
  note?: string;
}

interface StructureSummary {
  structure?: string;
  max_loss_usd?: number;
  max_profit_usd?: number;
  entry_price_estimate?: number;
}

interface AgentCard {
  agent_id: string;
  symbol: string;
  state: AgentState;
  bias?: string;
  confidence?: number;
  elapsed_sec?: number;
  latest_tool?: string;
  latest_preview?: string;
  error?: string;
  started_at: number;
  timeline: TimelineEntry[];
  structure?: StructureSummary;
}

export default function AgentSwarm() {
  const [agents, setAgents] = useState<Record<string, AgentCard>>({});
  const [openId, setOpenId] = useState<string | null>(null);

  useEffect(() => {
    const es = new EventSource(activityStreamUrl());
    es.onmessage = (ev) => {
      try {
        const evt = JSON.parse(ev.data) as ActivityEvent;
        if (!evt.type?.startsWith("agent.")) return;
        const d = (evt.data || {}) as Record<string, unknown>;

        if (evt.type === "agent.fanout") {
          const syms = (d.symbols as string[]) || [];
          setAgents((prev) => {
            const next = { ...prev };
            for (const sym of syms) {
              const id = `research-${sym.toLowerCase()}`;
              next[id] = {
                agent_id: id,
                symbol: sym,
                state: "running",
                started_at: Date.now(),
                timeline: [
                  { ts: Date.now(), type: "agent.fanout", note: "dispatched" },
                ],
              };
            }
            return next;
          });
          return;
        }

        const id = d.agent_id as string | undefined;
        if (!id) return;
        setAgents((prev) => {
          const current = prev[id] || {
            agent_id: id,
            symbol: (d.symbol as string) || id,
            state: "running" as AgentState,
            started_at: Date.now(),
            timeline: [],
          };
          const entry: TimelineEntry = {
            ts: Date.now(),
            type: evt.type,
            tool: d.tool as string | undefined,
            preview: d.preview as string | undefined,
            call_id: d.call_id as number | undefined,
            round_idx: d.round_idx as number | undefined,
          };
          if (evt.type === "agent.started") {
            return {
              ...prev,
              [id]: {
                ...current,
                state: "running",
                started_at: Date.now(),
                timeline: [...current.timeline, entry],
              },
            };
          }
          if (evt.type === "agent.progress") {
            return {
              ...prev,
              [id]: {
                ...current,
                latest_tool: entry.tool,
                latest_preview: entry.preview,
                timeline: [...current.timeline, entry],
              },
            };
          }
          if (evt.type === "agent.done") {
            const finalCallId = d.final_call_id as number | undefined;
            const structure = d.structure as StructureSummary | null | undefined;
            return {
              ...prev,
              [id]: {
                ...current,
                state: "done",
                bias: d.bias as string | undefined,
                confidence: d.confidence as number | undefined,
                elapsed_sec: d.elapsed_sec as number | undefined,
                structure: structure ?? undefined,
                timeline: [
                  ...current.timeline,
                  { ...entry, call_id: entry.call_id ?? finalCallId },
                ],
              },
            };
          }
          if (evt.type === "agent.failed") {
            return {
              ...prev,
              [id]: {
                ...current,
                state: "failed",
                error: d.error as string | undefined,
                timeline: [...current.timeline, entry],
              },
            };
          }
          return prev;
        });
      } catch {
        /* ignore */
      }
    };
    return () => es.close();
  }, []);

  const cards = Object.values(agents).sort(
    (a, b) => a.symbol.localeCompare(b.symbol)
  );
  if (cards.length === 0) return null;

  return (
    <section className="frame p-4">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-sm uppercase tracking-widest text-text-dim">
          <span className="text-accent">▸</span> agent_swarm
        </h2>
        <span className="text-xs text-text-faint tabular">
          [{cards.length.toString().padStart(2, "0")}]
        </span>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
        {cards.map((a) => (
          <AgentCardView
            key={a.agent_id}
            a={a}
            open={openId === a.agent_id}
            onToggle={() =>
              setOpenId((id) => (id === a.agent_id ? null : a.agent_id))
            }
          />
        ))}
      </div>
    </section>
  );
}

function AgentCardView({
  a,
  open,
  onToggle,
}: {
  a: AgentCard;
  open: boolean;
  onToggle: () => void;
}) {
  const toneClass =
    a.state === "done"
      ? "border-accent/40 bg-accent/5"
      : a.state === "failed"
      ? "border-danger/40 bg-danger/5"
      : "border-border bg-bg-panel/50 animate-pulse";

  const biasColor =
    a.bias === "bullish"
      ? "text-accent"
      : a.bias === "bearish"
      ? "text-danger"
      : a.bias === "avoid"
      ? "text-text-faint"
      : "text-text-dim";

  return (
    <div className={`border ${toneClass} text-xs tabular`}>
      <button
        onClick={onToggle}
        className="w-full p-3 text-left hover:bg-bg-raised/30 transition-colors"
      >
        <div className="flex items-center justify-between">
          <span className="font-semibold text-accent">{a.symbol}</span>
          <span className="text-[10px] uppercase text-text-faint">
            {open ? "▾" : "▸"} {a.state}
          </span>
        </div>
        {a.state === "running" && a.latest_tool && (
          <div className="mt-1 text-text-dim text-[11px] truncate">
            → {a.latest_tool}: {a.latest_preview || "..."}
          </div>
        )}
        {a.state === "done" && (
          <div className="mt-1">
            <span className={`uppercase ${biasColor}`}>{a.bias || "—"}</span>
            {typeof a.confidence === "number" && (
              <span className="text-text-dim">
                {" "}
                · conf {a.confidence.toFixed(2)}
              </span>
            )}
            {typeof a.elapsed_sec === "number" && (
              <span className="text-text-faint">
                {" "}
                · {a.elapsed_sec.toFixed(1)}s
              </span>
            )}
            {a.structure?.structure && (
              <div className="mt-1 text-[11px] text-text-dim">
                <span className="text-accent">◆</span>{" "}
                <span className="uppercase">{a.structure.structure}</span>
                {typeof a.structure.max_loss_usd === "number" && (
                  <span className="text-text-faint">
                    {" "}
                    · max_loss ${a.structure.max_loss_usd.toFixed(0)}
                  </span>
                )}
                {typeof a.structure.max_profit_usd === "number" && (
                  <span className="text-text-faint">
                    {" "}
                    · max_profit ${a.structure.max_profit_usd.toFixed(0)}
                  </span>
                )}
                {typeof a.structure.entry_price_estimate === "number" && (
                  <span className="text-text-faint">
                    {" "}
                    · entry ${a.structure.entry_price_estimate.toFixed(2)}
                  </span>
                )}
              </div>
            )}
          </div>
        )}
        {a.state === "failed" && a.error && (
          <div className="mt-1 text-danger text-[11px] truncate">
            {a.error}
          </div>
        )}
      </button>
      {open && <AgentDrawer a={a} />}
    </div>
  );
}

function AgentDrawer({ a }: { a: AgentCard }) {
  return (
    <div className="border-t border-border/60 bg-bg-raised/20 p-3 space-y-2">
      <div className="text-[10px] uppercase tracking-widest text-text-faint">
        timeline · {a.agent_id}
      </div>
      {a.timeline.length === 0 ? (
        <div className="text-text-faint text-[11px]">no events yet</div>
      ) : (
        <ul className="space-y-1">
          {a.timeline.map((e, i) => (
            <TimelineRow key={`${e.ts}-${i}`} e={e} />
          ))}
        </ul>
      )}
    </div>
  );
}

function TimelineRow({ e }: { e: TimelineEntry }) {
  const [expanded, setExpanded] = useState(false);
  const [detail, setDetail] = useState<LlmCallDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const canExpand = e.call_id !== undefined;

  const toggle = async () => {
    if (!canExpand) return;
    const next = !expanded;
    setExpanded(next);
    if (next && !detail && e.call_id !== undefined) {
      setLoading(true);
      setError(null);
      try {
        const d = await api.llmCall(e.call_id);
        setDetail(d);
      } catch (err) {
        setError(String(err));
      } finally {
        setLoading(false);
      }
    }
  };

  const tsLabel = fmtTime(e.ts);
  const label =
    e.type === "agent.progress"
      ? `tool ${e.tool ?? "?"}`
      : e.type === "agent.done"
      ? "committed"
      : e.type === "agent.failed"
      ? "failed"
      : e.type === "agent.started"
      ? "started"
      : e.type;

  return (
    <li className="text-[11px]">
      <button
        onClick={toggle}
        disabled={!canExpand}
        className={`w-full text-left flex items-start gap-2 ${
          canExpand ? "hover:text-accent" : "cursor-default"
        }`}
      >
        <span className="text-text-faint tabular w-[60px] shrink-0">
          {tsLabel.slice(-8)}
        </span>
        <span className="text-text-dim w-[120px] shrink-0 truncate">
          {label}
          {e.round_idx !== undefined && (
            <span className="text-text-faint"> #{e.round_idx}</span>
          )}
        </span>
        <span className="text-text-dim flex-1 truncate">
          {e.preview || e.note || ""}
        </span>
        {canExpand && (
          <span className="text-accent shrink-0">{expanded ? "▾" : "▸"}</span>
        )}
      </button>
      {expanded && (
        <div className="mt-1 ml-2 pl-2 border-l border-border/60">
          {loading && <div className="text-text-faint">loading...</div>}
          {error && <div className="text-danger">{error}</div>}
          {detail && <LlmCallView detail={detail} />}
        </div>
      )}
    </li>
  );
}

export function LlmCallView({ detail }: { detail: LlmCallDetail }) {
  const messages = detail.prompt_messages ?? [];
  return (
    <div className="space-y-2 text-[11px]">
      <div className="text-text-faint tabular">
        {detail.provider}::{detail.model} · {detail.prompt_tokens}p/
        {detail.completion_tokens}c/{detail.total_tokens} · $
        {detail.cost_usd.toFixed(4)}
      </div>
      <div>
        <div className="text-[10px] uppercase text-text-faint tracking-widest">
          prompt_messages [{messages.length}]
        </div>
        <div className="space-y-1">
          {messages.map((m, i) => (
            <MessageBlock key={i} m={m} />
          ))}
        </div>
      </div>
      <div>
        <div className="text-[10px] uppercase text-text-faint tracking-widest">
          response_body
        </div>
        <pre className="bg-bg-panel/50 border border-border/40 p-2 whitespace-pre-wrap break-all text-text-dim max-h-60 overflow-auto">
          {safeStringify(detail.response_body)}
        </pre>
      </div>
    </div>
  );
}

function MessageBlock({ m }: { m: Record<string, unknown> }) {
  const role = (m.role as string) || "?";
  const roleTone =
    role === "system"
      ? "text-warn"
      : role === "assistant"
      ? "text-accent"
      : role === "tool"
      ? "text-text-faint"
      : "text-text-dim";
  const body = useMemo(() => renderContent(m), [m]);
  return (
    <div className="bg-bg-panel/40 border border-border/40 p-2">
      <div className={`text-[10px] uppercase ${roleTone} tracking-widest`}>
        {role}
      </div>
      <pre className="whitespace-pre-wrap break-all text-text-dim max-h-48 overflow-auto">
        {body}
      </pre>
    </div>
  );
}

function renderContent(m: Record<string, unknown>): string {
  const content = m.content;
  if (typeof content === "string") return content;
  if (content == null) {
    // tool_calls or other structured bits
    const rest: Record<string, unknown> = { ...m };
    delete rest.role;
    return safeStringify(rest);
  }
  return safeStringify(content);
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
