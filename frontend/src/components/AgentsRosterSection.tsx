"use client";

import { useEffect, useState } from "react";
import { api, AgentRoster } from "@/lib/api";
import { fmtDateTime } from "@/lib/time";

export default function AgentsRosterSection() {
  const [roster, setRoster] = useState<AgentRoster | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      api
        .agentsRoster()
        .then((r) => {
          setRoster(r);
          setError(null);
        })
        .catch((e) => setError(String(e)));
    load();
    const t = setInterval(load, 15_000);
    return () => clearInterval(t);
  }, []);

  return (
    <section className="frame p-4 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-sm uppercase tracking-widest text-text-dim">
          <span className="text-accent">▸</span> enabled_agents
          <span className="text-text-faint text-[10px] ml-2">
            // agent types wired into this system
          </span>
        </h2>
        {roster && (
          <span className="text-[10px] text-text-faint tabular">
            [
            {roster.roles
              .filter((r) => r.enabled)
              .length.toString()
              .padStart(2, "0")}
            /{roster.roles.length.toString().padStart(2, "0")}]
          </span>
        )}
      </div>

      {error && !roster && (
        <p className="text-danger text-xs">{error}</p>
      )}

      {roster && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {roster.roles.map((r) => (
            <div
              key={r.role}
              className={
                "border px-3 py-2 text-xs space-y-1 " +
                (r.enabled
                  ? "border-accent/40 bg-accent/5"
                  : "border-border bg-bg-panel/50 opacity-60")
              }
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span
                    className={
                      r.enabled ? "text-accent" : "text-text-faint"
                    }
                  >
                    {r.enabled ? "●" : "○"}
                  </span>
                  <span className="font-semibold text-text">{r.label}</span>
                </div>
                <span className="text-[10px] text-text-faint tabular uppercase">
                  {r.enabled ? r.cadence : "disabled"}
                </span>
              </div>
              <p className="text-text-dim leading-relaxed">
                {r.description}
              </p>
              <div className="flex items-center justify-between text-[10px] text-text-faint tabular">
                <span>
                  calls_24h: <span className="text-text-dim">{r.calls_24h}</span>
                </span>
                <span>
                  last_run:{" "}
                  <span className="text-text-dim">
                    {r.last_run_at ? fmtDateTime(r.last_run_at) : "—"}
                  </span>
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
