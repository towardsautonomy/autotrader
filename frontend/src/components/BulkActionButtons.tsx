"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import { confirmDialog } from "@/components/Dialog";

export default function BulkActionButtons({
  openPositionCount,
}: {
  openPositionCount: number;
}) {
  const [busy, setBusy] = useState<"close" | "cancel" | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [tone, setTone] = useState<"accent" | "danger" | "warn">("accent");

  const closeAll = async () => {
    if (openPositionCount === 0) {
      setTone("warn");
      setMsg("no open positions");
      return;
    }
    const ok = await confirmDialog({
      title: "close_all_positions",
      message: `Close ALL ${openPositionCount} open position(s) at market?\nThis is irreversible.`,
      confirmLabel: "close all",
      tone: "danger",
    });
    if (!ok) return;
    setBusy("close");
    setMsg(null);
    try {
      const r = await api.closeAllPositions();
      setTone(r.closed === r.attempted ? "accent" : "warn");
      setMsg(`closed ${r.closed}/${r.attempted}`);
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      setTone("danger");
      setMsg(String(e));
    } finally {
      setBusy(null);
    }
  };

  const cancelAll = async () => {
    const ok = await confirmDialog({
      title: "cancel_all_orders",
      message: "Cancel every open/resting broker order?",
      confirmLabel: "cancel orders",
      tone: "warn",
    });
    if (!ok) return;
    setBusy("cancel");
    setMsg(null);
    try {
      const r = await api.cancelAllOrders();
      setTone("accent");
      setMsg(
        `cancelled ${r.cancelled} broker / ${r.local_reconciled} local row(s)`,
      );
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      setTone("danger");
      setMsg(String(e));
    } finally {
      setBusy(null);
    }
  };

  const toneClass =
    tone === "accent"
      ? "text-accent"
      : tone === "warn"
        ? "text-warn"
        : "text-danger";

  return (
    <div className="flex flex-col gap-1 w-full sm:w-auto">
      <div className="flex flex-col sm:flex-row gap-2">
        <button
          onClick={closeAll}
          disabled={busy !== null}
          className="border border-danger/60 text-danger bg-danger/5 hover:bg-danger/15 px-3 py-2 text-[11px] font-semibold uppercase tracking-widest disabled:opacity-50"
        >
          {busy === "close" ? "closing..." : "▣ close_all_positions"}
        </button>
        <button
          onClick={cancelAll}
          disabled={busy !== null}
          className="border border-warn/60 text-warn bg-warn/5 hover:bg-warn/15 px-3 py-2 text-[11px] font-semibold uppercase tracking-widest disabled:opacity-50"
        >
          {busy === "cancel" ? "cancelling..." : "✕ cancel_all_orders"}
        </button>
      </div>
      {msg && <span className={`text-[11px] ${toneClass}`}>{msg}</span>}
    </div>
  );
}
