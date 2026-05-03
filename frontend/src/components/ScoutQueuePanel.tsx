"use client";

import { useEffect, useState } from "react";
import { api, ScoutQueue } from "@/lib/api";

export default function ScoutQueuePanel() {
  const [data, setData] = useState<ScoutQueue | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      api
        .scoutQueue()
        .then((q) => {
          setData(q);
          setError(null);
        })
        .catch((e) => setError(String(e)));
    load();
    const i = setInterval(load, 15_000);
    return () => clearInterval(i);
  }, []);

  if (error) return null;
  if (!data) return null;
  if (!data.enabled) return null;

  const budgetPct =
    data.daily_llm_budget_usd > 0
      ? (data.daily_llm_spent_usd / data.daily_llm_budget_usd) * 100
      : 0;
  const budgetTone =
    budgetPct >= 100
      ? "text-danger"
      : budgetPct >= 80
      ? "text-warn"
      : "text-accent";

  return (
    <section className="frame p-4">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <h2 className="text-sm uppercase tracking-widest text-text-dim">
          <span className="text-accent">▸</span> scout_queue
          <span className="text-text-faint text-xs ml-2">
            {`// continuous scan · ttl ${Math.round(data.ttl_sec / 60)}m`}
          </span>
        </h2>
        <div className="flex items-center gap-3 text-xs tabular">
          <span className="text-text-faint">
            [{data.queue_size.toString().padStart(2, "0")}]
          </span>
          {data.daily_llm_budget_usd > 0 && (
            <span className={budgetTone}>
              ${data.daily_llm_spent_usd.toFixed(4)} / $
              {data.daily_llm_budget_usd.toFixed(2)}
            </span>
          )}
        </div>
      </div>
      {data.candidates.length === 0 ? (
        <p className="text-text-faint text-xs py-2">
          <span className="text-text-dim">$</span> no candidates on the queue
          yet — waiting for next scout tick.
        </p>
      ) : (
        <div className="overflow-x-auto -mx-4 sm:mx-0 px-4 sm:px-0">
          <table className="w-full min-w-[480px] text-xs tabular">
            <thead>
              <tr className="text-text-faint border-b border-border">
                <th className="text-left py-1 font-normal uppercase text-[10px] tracking-widest">
                  symbol
                </th>
                <th className="text-left py-1 font-normal uppercase text-[10px] tracking-widest">
                  source
                </th>
                <th className="text-left py-1 font-normal uppercase text-[10px] tracking-widest">
                  note
                </th>
                <th className="text-right py-1 font-normal uppercase text-[10px] tracking-widest">
                  age
                </th>
              </tr>
            </thead>
            <tbody>
              {data.candidates.map((c) => (
                <tr
                  key={`${c.symbol}-${c.added_at}`}
                  className="border-b border-border/40"
                >
                  <td className="py-1 text-accent font-semibold">{c.symbol}</td>
                  <td className="py-1 text-text-dim">{c.source}</td>
                  <td className="py-1 text-text-dim truncate max-w-[240px]">
                    {c.note || "—"}
                  </td>
                  <td className="py-1 text-right text-text-faint">
                    {formatAge(c.age_sec)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function formatAge(sec: number): string {
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}
