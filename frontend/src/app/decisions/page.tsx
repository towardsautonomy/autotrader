"use client";

import { useCallback, useEffect, useState } from "react";
import { api, Decision } from "@/lib/api";
import { usePagination } from "@/components/Pager";
import { fmtDateTime } from "@/lib/time";
import { useRefreshOnResume } from "@/lib/useRefreshOnResume";
import { useActivityStream } from "@/lib/useActivityStream";

const DECISION_REFRESH_TOPICS = [
  "risk.approved",
  "risk.rejected",
  "order.submit",
  "order.failed",
  "order.filled",
  "loop.tick.end",
];

export default function DecisionsPage() {
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api
        .decisions(100)
        .then((d) => {
          setDecisions(d);
          setError(null);
        })
        .catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    load();
    const i = setInterval(load, 10_000);
    return () => clearInterval(i);
  }, [load]);

  useRefreshOnResume(load);

  useActivityStream(DECISION_REFRESH_TOPICS, load);

  const { visible, totalKept, truncated, Pager } = usePagination(decisions);

  if (error)
    return (
      <div className="frame p-4 text-danger text-sm">
        <span className="text-text-faint">[err]</span> {error}
      </div>
    );

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-xl font-semibold flex items-center gap-2">
          <span className="text-accent">▸</span>
          ai_decisions
          <span className="text-text-faint text-xs">// audit log</span>
          <span className="text-text-faint text-[10px] tabular ml-2">
            [{totalKept.toString().padStart(3, "0")}
            {truncated ? "+" : ""}]
          </span>
        </h1>
        <p className="text-xs text-text-dim mt-1">
          Every cycle the AI runs — including holds and rejections — is recorded
          for audit.
        </p>
      </header>

      <div className="space-y-2">
        {visible.map((d) => (
          <DecisionRow key={d.id} d={d} />
        ))}
        {totalKept === 0 && (
          <div className="frame p-4 text-text-dim text-sm">
            <span className="text-text-faint">$</span> no decisions logged yet.
            The scheduler runs every 5 min during market hours — come back
            after the first cycle.
          </div>
        )}
      </div>
      <Pager />
    </div>
  );
}

function DecisionRow({ d }: { d: Decision }) {
  const status = d.executed
    ? { label: "EXECUTED", tone: "accent" }
    : d.approved
    ? { label: "APPROVED", tone: "warn" }
    : { label: "REJECTED", tone: "danger" };

  const toneClass = {
    accent: "text-accent border-accent/40 bg-accent/5",
    warn: "text-warn border-warn/40 bg-warn/5",
    danger: "text-danger border-danger/40 bg-danger/5",
  }[status.tone];

  const leftBorderClass = {
    accent: "border-l-accent",
    warn: "border-l-warn",
    danger: "border-l-danger",
  }[status.tone];

  return (
    <div
      className={`border border-border border-l-2 ${leftBorderClass} bg-bg-panel p-3 text-xs`}
    >
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-2 tabular">
          <span className={`px-2 py-0.5 border ${toneClass} uppercase tracking-widest font-semibold`}>
            {status.label}
          </span>
          <span className="text-text-dim">
            #{d.id.toString().padStart(4, "0")}
          </span>
          <span className="text-text-faint">·</span>
          <span className="uppercase text-text-dim">{d.market}</span>
          <span className="text-text-faint">·</span>
          <span className="text-accent">{d.model}</span>
        </div>
        <span className="text-text-faint tabular">
          {fmtDateTime(d.created_at)}
        </span>
      </div>

      {d.rejection_code && (
        <div className="mt-2 text-danger text-xs">
          <span className="text-text-faint">→</span> {d.rejection_code}
          {d.rejection_reason && (
            <span className="text-text-dim"> // {d.rejection_reason}</span>
          )}
        </div>
      )}

      {d.rationale && (
        <p className="text-sm text-text mt-2 leading-relaxed whitespace-pre-wrap">
          {d.rationale}
        </p>
      )}

      {d.research && d.research.length > 0 && (
        <details className="mt-2 text-text-dim">
          <summary className="cursor-pointer hover:text-accent">
            <span className="text-text-faint">▸</span> research_trail
            <span className="text-text-faint ml-1">
              [{d.research.length}]
            </span>
          </summary>
          <ul className="mt-2 space-y-1 text-[11px] leading-snug border-l-2 border-border/50 pl-2">
            {d.research.map((r, i) => (
              <li key={i}>
                <span className="text-accent">{r.tool}</span>
                <span className="text-text-faint">
                  {" "}
                  ({Object.entries(r.arguments)
                    .map(([k, v]) => `${k}=${String(v).slice(0, 40)}`)
                    .join(", ")})
                </span>
                <div className="text-text pl-4">
                  → {r.result_preview}
                </div>
              </li>
            ))}
          </ul>
        </details>
      )}

      {d.proposal_json && (
        <details className="mt-2 text-text-dim">
          <summary className="cursor-pointer hover:text-accent">
            <span className="text-text-faint">▸</span> raw_proposal.json
          </summary>
          <pre className="mt-2 overflow-auto bg-bg p-3 border border-border text-[11px] leading-snug">
            {JSON.stringify(d.proposal_json, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}
