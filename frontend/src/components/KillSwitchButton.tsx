"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { confirmDialog } from "@/components/Dialog";

export default function KillSwitchButton({
  tradingEnabled,
}: {
  tradingEnabled: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const kill = async () => {
    const ok = await confirmDialog({
      title: "kill_switch",
      message:
        "HALT all trading? Existing positions remain open. New orders will be blocked until you resume.",
      confirmLabel: "halt",
      tone: "danger",
    });
    if (!ok) return;
    setBusy(true);
    setError(null);
    try {
      await api.killSwitch("manual from dashboard");
      location.reload();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  const unpause = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.unpause();
      location.reload();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col items-stretch sm:items-end gap-1 w-full sm:w-auto">
      {tradingEnabled ? (
        <button
          onClick={kill}
          disabled={busy}
          className="border border-danger/60 text-danger bg-danger/10 hover:bg-danger/20 active:bg-danger/30 px-4 py-3 sm:py-2 font-semibold uppercase tracking-widest text-xs disabled:opacity-50"
        >
          ▣ kill_switch.exec
        </button>
      ) : (
        <button
          onClick={unpause}
          disabled={busy}
          className="border border-warn/60 text-warn bg-warn/10 hover:bg-warn/20 active:bg-warn/30 px-4 py-3 sm:py-2 font-semibold uppercase tracking-widest text-xs disabled:opacity-50"
        >
          ▶ resume_trading
        </button>
      )}
      {error && <span className="text-xs text-danger">! {error}</span>}
      {!tradingEnabled && (
        <span className="text-xs text-warn uppercase tracking-widest">
          ⛔ trading halted
        </span>
      )}
    </div>
  );
}
