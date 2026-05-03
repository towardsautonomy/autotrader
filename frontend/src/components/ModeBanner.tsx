"use client";

import { useEffect, useState } from "react";
import { api, SystemStatusResponse, Mode } from "@/lib/api";
import { confirmDialog, promptDialog } from "@/components/Dialog";

type DisplayMode = Mode | "OFFLINE";

const LIVE_CONFIRM = "I UNDERSTAND I CAN LOSE REAL MONEY";

export default function ModeBanner() {
  const [health, setHealth] = useState<SystemStatusResponse | null>(null);
  const [offline, setOffline] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const load = async () => {
    try {
      const h = await api.systemStatus();
      setHealth(h);
      setOffline(false);
    } catch {
      setOffline(true);
    }
  };

  useEffect(() => {
    let active = true;
    const run = async () => {
      await load();
      if (!active) return;
    };
    run();
    const i = setInterval(run, 15_000);
    return () => {
      active = false;
      clearInterval(i);
    };
  }, []);

  const mode: DisplayMode = offline ? "OFFLINE" : (health?.mode ?? "PAPER");
  const pending = Boolean(health?.pending_restart);

  const flip = async () => {
    if (!health || busy) return;
    setErr(null);
    const target: Mode = health.mode === "PAPER" ? "LIVE" : "PAPER";
    let confirmPhrase = "";
    if (target === "LIVE") {
      const entered = await promptDialog({
        title: "flip_to_live",
        tone: "danger",
        message:
          "You are about to switch to LIVE trading. Real money will be at risk.\n\n" +
          `Type the phrase exactly to confirm:\n\n${LIVE_CONFIRM}`,
        placeholder: LIVE_CONFIRM,
        confirmLabel: "▸ flip to live",
      });
      if (!entered) return;
      if (entered !== LIVE_CONFIRM) {
        setErr("confirm_phrase mismatch — mode unchanged");
        return;
      }
      confirmPhrase = entered;
    } else {
      const ok = await confirmDialog({
        title: "flip_to_paper",
        message:
          "Switch back to PAPER mode? Real-money trading will be disabled after restart.",
        confirmLabel: "▸ flip to paper",
      });
      if (!ok) return;
    }

    setBusy(true);
    try {
      const r = await api.setMode(target, confirmPhrase);
      setHealth((prev) =>
        prev
          ? { ...prev, mode: r.mode, pending_restart: r.pending_restart }
          : prev,
      );
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  const config: Record<DisplayMode, {
    label: string;
    bg: string;
    dot: string;
    btn: string;
  }> = {
    LIVE: {
      label: "MODE=LIVE  //  REAL CAPITAL AT RISK",
      bg: "bg-danger/15 border-danger/50 text-danger",
      dot: "bg-danger",
      btn: "border-danger/60 hover:bg-danger/20 text-danger",
    },
    PAPER: {
      label: "MODE=PAPER  //  SIMULATED — no real money",
      bg: "bg-accent/10 border-accent/40 text-accent",
      dot: "bg-accent",
      btn: "border-accent/60 hover:bg-accent/20 text-accent",
    },
    OFFLINE: {
      label: "OFFLINE  //  backend not reachable",
      bg: "bg-warn/10 border-warn/40 text-warn",
      dot: "bg-warn",
      btn: "border-warn/60 hover:bg-warn/20 text-warn",
    },
  };

  const c = config[mode];

  return (
    <div
      className={`border-b ${c.bg} text-[11px] tracking-widest py-1.5 px-3 font-semibold uppercase relative z-10`}
    >
      <div className="max-w-6xl mx-auto flex items-center justify-center gap-3 flex-wrap">
        <span className="flex items-center gap-2">
          <span
            className={`inline-block w-1.5 h-1.5 rounded-full ${c.dot} blink`}
          />
          {c.label}
        </span>
        {mode !== "OFFLINE" && (
          <button
            onClick={flip}
            disabled={busy}
            title={
              mode === "PAPER"
                ? "Switch to LIVE (real money)"
                : "Switch back to PAPER (simulated)"
            }
            className={
              "border px-2 py-0.5 text-[10px] tracking-widest disabled:opacity-50 " +
              c.btn
            }
          >
            {busy
              ? "..."
              : `▸ flip → ${mode === "PAPER" ? "LIVE" : "PAPER"}`}
          </button>
        )}
        {pending && (
          <span className="text-warn border border-warn/60 bg-warn/10 px-2 py-0.5 text-[10px] tracking-widest">
            ⟲ restart required (active: {health?.active_mode})
          </span>
        )}
        {err && (
          <span className="text-danger normal-case tracking-normal">
            ! {err}
          </span>
        )}
      </div>
    </div>
  );
}
