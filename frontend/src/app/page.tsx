"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { api, Account, LlmUsageSummary, Position } from "@/lib/api";
import ActivityLog from "@/components/ActivityLog";
import AgentSwarm from "@/components/AgentSwarm";
import AIStatusPanel from "@/components/AIStatus";
import AgentPauseButton from "@/components/AgentPauseButton";
import BulkActionButtons from "@/components/BulkActionButtons";
import ScoutQueuePanel from "@/components/ScoutQueuePanel";
import { fmtDateTime, fmtTime } from "@/lib/time";
import { useRefreshOnResume } from "@/lib/useRefreshOnResume";
import { useActivityStream } from "@/lib/useActivityStream";

const DASHBOARD_REFRESH_TOPICS = [
  "order.filled",
  "order.failed",
  "order.submit",
  "trade.closed_manual",
  "trade.close_failed",
  "position_review.closed",
  "position_review.tightened",
  "reconciler.bracket_filled",
  "reconciler.pending_filled",
  "reconciler.pending_canceled",
  "reconciler.close_filled",
  "safety.dte_closed",
  "agents.paused",
  "agents.resumed",
];

export default function Dashboard() {
  const [account, setAccount] = useState<Account | null>(null);
  const [usage, setUsage] = useState<LlmUsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastTick, setLastTick] = useState<Date | null>(null);

  const loadAccount = useCallback(
    () =>
      api
        .account()
        .then((a) => {
          setAccount(a);
          setLastTick(new Date());
          setError(null);
        })
        .catch((e) => setError(String(e))),
    [],
  );
  const loadUsage = useCallback(
    () => api.llmUsage(24).then(setUsage).catch(() => {}),
    [],
  );

  useEffect(() => {
    loadAccount();
    const i = setInterval(loadAccount, 10_000);
    return () => clearInterval(i);
  }, [loadAccount]);

  useEffect(() => {
    loadUsage();
    const i = setInterval(loadUsage, 30_000);
    return () => clearInterval(i);
  }, [loadUsage]);

  useRefreshOnResume(
    useCallback(() => {
      loadAccount();
      loadUsage();
    }, [loadAccount, loadUsage]),
  );

  useActivityStream(DASHBOARD_REFRESH_TOPICS, loadAccount);

  if (error)
    return (
      <div className="frame p-4 text-danger text-sm">
        <span className="text-text-faint">[err]</span> {error}
      </div>
    );
  if (!account)
    return (
      <div className="text-text-dim text-sm">
        <span className="blink text-accent">▊</span> awaiting backend...
      </div>
    );

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <span className="text-accent">▸</span>
            dashboard
            <span className="text-text-faint text-xs">// realtime</span>
          </h1>
          <p className="text-xs text-text-faint mt-1">
            last sync: {lastTick ? `${fmtTime(lastTick)} PT` : "—"}
            <span className="mx-2">·</span>
            auto-refresh every 10s
          </p>
        </div>
        <div className="flex flex-col sm:flex-row items-stretch sm:items-start gap-2">
          <AgentPauseButton
            agentsPaused={account.agents_paused}
            pauseWhenClosed={account.pause_when_market_closed}
          />
        </div>
      </header>

      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Metric label="CASH" value={`$${account.cash_balance.toFixed(2)}`} />
        <Metric
          label="EQUITY"
          value={`$${account.total_equity.toFixed(2)}`}
        />
        <Metric
          label="DAY_PNL"
          value={`${account.day_realized_pnl >= 0 ? "+" : ""}$${account.day_realized_pnl.toFixed(2)}`}
          tone={account.day_realized_pnl >= 0 ? "pos" : "neg"}
        />
        <Metric
          label="CUMULATIVE_PNL"
          value={`${account.cumulative_pnl >= 0 ? "+" : ""}$${account.cumulative_pnl.toFixed(2)}`}
          tone={account.cumulative_pnl >= 0 ? "pos" : "neg"}
        />
      </section>

      <section className="frame p-4">
        <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
          <h2 className="text-sm uppercase tracking-widest text-text-dim">
            <span className="text-accent">▸</span> open_positions
            <span className="ml-2 text-xs text-text-faint tabular">
              [{account.positions.length.toString().padStart(2, "0")}]
            </span>
          </h2>
          <BulkActionButtons openPositionCount={account.positions.length} />
        </div>

        {account.positions.length === 0 ? (
          <p className="text-text-faint text-sm py-4">
            <span className="text-text-dim">$</span> no open positions.
          </p>
        ) : (
          <div className="overflow-x-auto -mx-4 sm:mx-0 px-4 sm:px-0">
            <table className="w-full min-w-[520px] text-xs tabular">
              <thead className="text-text-dim border-b border-border">
                <tr>
                  <Th>MARKET</Th>
                  <Th>SYMBOL</Th>
                  <Th align="right">SIZE_$</Th>
                  <Th align="right">ENTRY</Th>
                  <Th align="right">LAST</Th>
                  <Th align="right">UNREALIZED</Th>
                  <Th>AI_VERDICT</Th>
                </tr>
              </thead>
              <tbody>
                {account.positions.map((p) => (
                  <PositionRows key={`${p.market}-${p.symbol}`} p={p} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
        <Stat label="mode" value={account.mode} />
        <Stat
          label="trading"
          value={account.trading_enabled ? "ENABLED" : "HALTED"}
          tone={account.trading_enabled ? "pos" : "neg"}
        />
        <Stat
          label="exposure"
          value={`$${account.total_exposure.toFixed(2)}`}
        />
        <Stat
          label="trades_today"
          value={account.daily_trade_count.toString()}
        />
      </section>

      {usage && (
        <section className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
          <Stat
            label="llm_cost_24h"
            value={`$${usage.total_cost_usd.toFixed(4)}`}
          />
          <Stat
            label="llm_calls_24h"
            value={usage.total_calls.toString()}
          />
          <Stat
            label="llm_tokens_24h"
            value={usage.total_tokens.toLocaleString()}
          />
          <Link
            href="/agents"
            className="border border-border px-3 py-2 bg-bg-panel/50 text-text-dim hover:text-accent hover:border-accent/30"
          >
            <div className="text-[10px] uppercase tracking-widest text-text-dim">
              rate_card
            </div>
            <div className="mt-0.5">configure →</div>
          </Link>
        </section>
      )}

      <AIStatusPanel />

      <ScoutQueuePanel />

      <AgentSwarm />

      <section>
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-sm uppercase tracking-widest text-text-dim">
            <span className="text-accent">▸</span> live_activity
            <span className="text-text-faint text-xs ml-2">// tail -f system</span>
          </h2>
        </div>
        <ActivityLog max={500} />
      </section>
    </div>
  );
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "pos" | "neg";
}) {
  const color =
    tone === "pos"
      ? "text-accent"
      : tone === "neg"
      ? "text-danger"
      : "text-text";
  return (
    <div className="frame p-4 tabular">
      <div className="text-[10px] uppercase tracking-widest text-text-dim">
        {label}
      </div>
      <div className={`text-xl font-semibold mt-1 ${color}`}>{value}</div>
    </div>
  );
}

function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone?: "pos" | "neg";
}) {
  const color =
    tone === "pos"
      ? "text-accent"
      : tone === "neg"
      ? "text-danger"
      : "text-text";
  return (
    <div className="border border-border px-3 py-2 bg-bg-panel/50">
      <div className="text-[10px] uppercase tracking-widest text-text-dim">
        {label}
      </div>
      <div className={`mt-0.5 ${color}`}>{value}</div>
    </div>
  );
}

const VERDICT_TONE: Record<string, string> = {
  HOLD: "text-text-dim border-border bg-bg-panel/60",
  APPROVED: "text-warn border-warn/60 bg-warn/5",
  EXECUTED: "text-accent border-accent/60 bg-accent/5",
  REJECTED: "text-danger border-danger/60 bg-danger/5",
};

function PositionRows({ p }: { p: Position }) {
  const v = p.last_verdict;
  return (
    <>
      <tr className="border-b border-border/30 hover:bg-accent/5">
        <Td className="text-text-dim uppercase">{p.market}</Td>
        <Td className="text-accent font-semibold">{p.symbol}</Td>
        <Td align="right">${p.size_usd.toFixed(2)}</Td>
        <Td align="right">${p.entry_price.toFixed(2)}</Td>
        <Td align="right">${p.current_price.toFixed(2)}</Td>
        <Td
          align="right"
          className={
            p.unrealized_pnl >= 0 ? "text-accent" : "text-danger"
          }
        >
          {p.unrealized_pnl >= 0 ? "+" : ""}
          ${p.unrealized_pnl.toFixed(2)}
        </Td>
        <Td>
          {v ? (
            <span
              className={`text-[10px] uppercase tracking-widest border px-2 py-0.5 ${
                VERDICT_TONE[v.status] ?? VERDICT_TONE.REJECTED
              }`}
            >
              {v.status === "REJECTED" ? v.rejection_code ?? "REJECTED" : v.status}
            </span>
          ) : (
            <span className="text-text-faint text-[10px]">—</span>
          )}
        </Td>
      </tr>
      {v && (
        <tr className="border-b border-border/60 bg-bg-panel/20">
          <td colSpan={7} className="px-1 pb-3 pt-1">
            <div className="flex items-start gap-2 text-[11px] leading-snug">
              <span className="text-text-faint tabular shrink-0 w-28">
                {fmtDateTime(v.created_at)}
              </span>
              <span className="text-text-dim shrink-0 uppercase">
                {v.action ?? "—"}
              </span>
              <span className="text-text flex-1">
                {v.rationale ? (
                  v.rationale
                ) : (
                  <span className="italic text-text-faint">
                    no rationale recorded
                  </span>
                )}
              </span>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function Th({
  children,
  align = "left",
}: {
  children: React.ReactNode;
  align?: "left" | "right";
}) {
  return (
    <th
      className={`text-${align} py-2 font-semibold uppercase tracking-widest text-[10px] px-1`}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align = "left",
  className = "",
}: {
  children: React.ReactNode;
  align?: "left" | "right";
  className?: string;
}) {
  return (
    <td className={`text-${align} py-1.5 px-1 ${className}`}>{children}</td>
  );
}
