"use client";

import { useState } from "react";
import { api } from "@/lib/api";

export default function AgentPauseButton({
  agentsPaused,
  pauseWhenClosed,
}: {
  agentsPaused: boolean;
  pauseWhenClosed: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [closedBusy, setClosedBusy] = useState(false);
  const [localClosed, setLocalClosed] = useState(pauseWhenClosed);

  const flip = async () => {
    setBusy(true);
    setError(null);
    try {
      if (agentsPaused) {
        await api.resumeAgents();
      } else {
        await api.pauseAgents();
      }
      location.reload();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const flipClosed = async () => {
    setClosedBusy(true);
    setError(null);
    const next = !localClosed;
    try {
      await api.setPauseWhenClosed(next);
      setLocalClosed(next);
    } catch (e) {
      setError(String(e));
    } finally {
      setClosedBusy(false);
    }
  };

  return (
    <div className="flex flex-col items-stretch sm:items-end gap-1 w-full sm:w-auto">
      <div className="flex flex-col sm:flex-row items-stretch gap-2">
        <button
          type="button"
          role="switch"
          aria-checked={localClosed}
          onClick={flipClosed}
          disabled={closedBusy}
          className={
            "flex items-center justify-between gap-2 border px-3 py-2 text-[10px] uppercase tracking-widest disabled:opacity-50 " +
            (localClosed
              ? "border-accent/50 text-accent bg-accent/5"
              : "border-border text-text-dim hover:text-text hover:border-accent/30")
          }
          title="When on, the decision + scout loops skip entirely while US equity market is closed. Monitor (stop-loss, EOD close) still runs."
        >
          <span className="flex items-center gap-2">
            <span
              className={
                "inline-block w-2 h-2 rounded-full " +
                (localClosed ? "bg-accent" : "bg-border")
              }
            />
            idle_when_market_closed
          </span>
          <span className={localClosed ? "text-accent" : "text-text-faint"}>
            {localClosed ? "ON" : "OFF"}
          </span>
        </button>

        <button
          onClick={flip}
          disabled={busy}
          className={
            agentsPaused
              ? "border border-accent/60 text-accent bg-accent/10 hover:bg-accent/20 active:bg-accent/30 px-4 py-3 sm:py-2 font-semibold uppercase tracking-widest text-xs disabled:opacity-50"
              : "border border-warn/60 text-warn bg-warn/10 hover:bg-warn/20 active:bg-warn/30 px-4 py-3 sm:py-2 font-semibold uppercase tracking-widest text-xs disabled:opacity-50"
          }
        >
          {agentsPaused ? "▶ resume_agents" : "⏸ pause_agents"}
        </button>
      </div>

      {error && <span className="text-xs text-danger">! {error}</span>}
      {agentsPaused && (
        <span className="text-xs text-warn uppercase tracking-widest">
          ⏸ all agents paused
        </span>
      )}
    </div>
  );
}
