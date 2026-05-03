"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, SymbolIntel, Trade } from "@/lib/api";
import { confirmDialog } from "@/components/Dialog";
import { usePagination } from "@/components/Pager";
import { fmtDateTime } from "@/lib/time";
import { useRefreshOnResume } from "@/lib/useRefreshOnResume";
import { useActivityStream } from "@/lib/useActivityStream";

// Topics that imply the trade list may have changed. We subscribe to the
// activity bus and reload immediately when any of these fire so the tab
// matches broker-platform snappiness instead of waiting for the 15s poll.
const TRADE_REFRESH_TOPICS = [
  "order.filled",
  "order.failed",
  "order.submit",
  "trade.closed_manual",
  "trade.close_failed",
  "position_review.closed",
  "reconciler.bracket_filled",
  "reconciler.pending_filled",
  "reconciler.pending_canceled",
  "reconciler.close_filled",
  "safety.dte_closed",
];

export default function TradesPage() {
  const [trades, setTrades] = useState<Trade[]>([]);
  const [intelBySymbol, setIntelBySymbol] = useState<Map<string, SymbolIntel>>(
    new Map(),
  );
  const [error, setError] = useState<string | null>(null);
  const [filterMarket, setFilterMarket] = useState<string>("");
  const [filterStatus, setFilterStatus] = useState<string>("");
  const [closingId, setClosingId] = useState<number | null>(null);
  const [expandedId, setExpandedId] = useState<number | null>(null);

  const reloadRef = useRef<() => void>(() => {});
  const reload = useCallback(
    () =>
      Promise.all([api.trades(200), api.intel().catch(() => null)])
        .then(([t, intel]) => {
          setTrades(t);
          if (intel) {
            setIntelBySymbol(
              new Map(intel.symbols.map((s) => [s.symbol.toUpperCase(), s])),
            );
          }
          setError(null);
        })
        .catch((e) => setError(String(e))),
    [],
  );
  useEffect(() => {
    reloadRef.current = reload;
  }, [reload]);

  useEffect(() => {
    reloadRef.current();
    const i = setInterval(() => reloadRef.current(), 15_000);
    return () => clearInterval(i);
  }, []);

  useRefreshOnResume(reload);

  useActivityStream(
    TRADE_REFRESH_TOPICS,
    useCallback(() => reloadRef.current(), []),
  );

  const close = async (t: Trade) => {
    const label = `${t.symbol} (#${t.id}, $${t.size_usd.toFixed(2)})`;
    const ok = await confirmDialog({
      title: "close_position",
      message: `Close ${label} at market?`,
      confirmLabel: "close",
      tone: "danger",
    });
    if (!ok) return;
    setClosingId(t.id);
    try {
      await api.closeTrade(t.id);
      await reload();
    } catch (e) {
      setError(String(e));
    } finally {
      setClosingId(null);
    }
  };

  const filtered = trades.filter((t) => {
    if (filterMarket && t.market !== filterMarket) return false;
    if (filterStatus && t.status !== filterStatus) return false;
    return true;
  });

  const { visible, totalKept, truncated, Pager } = usePagination(filtered, {
    perPage: 20,
    maxPages: 10,
  });

  if (error)
    return (
      <div className="frame p-4 text-danger text-sm">
        <span className="text-text-faint">[err]</span> {error}
      </div>
    );

  return (
    <div className="space-y-4">
      <header className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <span className="text-accent">▸</span>
            trade_history
          </h1>
          <p className="text-xs text-text-dim mt-1">
            Filled orders and their outcomes. P&L is marked on close.
          </p>
        </div>

        <div className="flex flex-wrap gap-2 text-xs w-full sm:w-auto">
          <select
            value={filterMarket}
            onChange={(e) => setFilterMarket(e.target.value)}
            className="flex-1 sm:flex-initial border border-border bg-bg-raised px-2 py-1.5 text-text"
          >
            <option value="">all markets</option>
            <option value="stocks">stocks</option>
            <option value="polymarket">polymarket</option>
          </select>
          <select
            value={filterStatus}
            onChange={(e) => setFilterStatus(e.target.value)}
            className="flex-1 sm:flex-initial border border-border bg-bg-raised px-2 py-1.5 text-text"
          >
            <option value="">all statuses</option>
            <option value="open">open</option>
            <option value="pending">pending</option>
            <option value="closed">closed</option>
            <option value="canceled">canceled</option>
            <option value="rejected">rejected</option>
            <option value="failed">failed</option>
          </select>
        </div>
      </header>

      <div className="frame p-4 overflow-x-auto">
        <div className="sm:hidden text-[10px] text-text-faint mb-2 uppercase tracking-widest">
          ← scroll horizontally →
        </div>
        <table className="w-full min-w-[900px] text-xs tabular">
          <thead className="text-text-dim border-b border-border uppercase tracking-widest text-[10px]">
            <tr>
              <Th>TIMESTAMP</Th>
              <Th>MARKET</Th>
              <Th>SYMBOL</Th>
              <Th>ACTION</Th>
              <Th align="right">SIZE_$</Th>
              <Th align="right">ENTRY</Th>
              <Th align="right">EXIT</Th>
              <Th align="right">REALIZED_PNL</Th>
              <Th>STATUS</Th>
              <Th align="right">ACTIONS</Th>
            </tr>
          </thead>
          <tbody>
            {visible.map((t) => {
              const isExpanded = expandedId === t.id;
              const intel = intelBySymbol.get(t.symbol.toUpperCase()) ?? null;
              return (
                <TradeRow
                  key={t.id}
                  trade={t}
                  intel={intel}
                  expanded={isExpanded}
                  onToggle={() =>
                    setExpandedId((cur) => (cur === t.id ? null : t.id))
                  }
                  onClose={() => close(t)}
                  closing={closingId === t.id}
                />
              );
            })}
          </tbody>
        </table>
        {filtered.length === 0 && (
          <p className="text-text-dim text-sm py-4">
            <span className="text-text-faint">$</span> no trades match filters.
          </p>
        )}
      </div>
      {totalKept > 0 && (
        <div className="px-1 text-[10px] text-text-faint tabular">
          [{totalKept.toString().padStart(3, "0")}
          {truncated ? "+" : ""}]
        </div>
      )}
      <Pager />
    </div>
  );
}

function TradeRow({
  trade: t,
  intel,
  expanded,
  onToggle,
  onClose,
  closing,
}: {
  trade: Trade;
  intel: SymbolIntel | null;
  expanded: boolean;
  onToggle: () => void;
  onClose: () => void;
  closing: boolean;
}) {
  return (
    <>
      <tr className="border-b border-border/40 hover:bg-accent/5">
        <Td className="text-text-dim">{fmtDateTime(t.created_at)}</Td>
        <Td className="text-text-dim uppercase">{t.market}</Td>
        <Td>
          <button
            type="button"
            onClick={onToggle}
            className="text-accent font-semibold hover:underline focus:outline-none focus:underline inline-flex items-center gap-1"
            aria-expanded={expanded}
            aria-controls={`trade-${t.id}-detail`}
          >
            <span className="text-text-faint w-2 inline-block">
              {expanded ? "▾" : "▸"}
            </span>
            {t.symbol}
          </button>
        </Td>
        <Td>
          <span
            className={
              t.action.includes("LONG")
                ? "text-accent"
                : t.action.includes("SHORT")
                ? "text-danger"
                : "text-warn"
            }
          >
            {t.action}
          </span>
        </Td>
        <Td align="right">${t.size_usd.toFixed(2)}</Td>
        <Td align="right">
          {t.entry_price ? `$${t.entry_price.toFixed(2)}` : "—"}
        </Td>
        <Td align="right">
          {t.exit_price ? `$${t.exit_price.toFixed(2)}` : "—"}
        </Td>
        <Td
          align="right"
          className={
            t.realized_pnl_usd > 0
              ? "text-accent"
              : t.realized_pnl_usd < 0
              ? "text-danger"
              : "text-text-dim"
          }
        >
          {t.status === "closed"
            ? `${t.realized_pnl_usd >= 0 ? "+" : ""}$${t.realized_pnl_usd.toFixed(2)}`
            : "—"}
        </Td>
        <Td>
          <StatusPill status={t.status} />
        </Td>
        <Td align="right">
          {t.status === "open" ? (
            <button
              onClick={onClose}
              disabled={closing}
              className="border border-danger/50 text-danger bg-danger/10 hover:bg-danger/20 px-2 py-0.5 text-[10px] uppercase tracking-widest disabled:opacity-50"
            >
              {closing ? "closing..." : "close"}
            </button>
          ) : (
            <span className="text-text-faint text-[10px]">—</span>
          )}
        </Td>
      </tr>
      {expanded && (
        <tr
          id={`trade-${t.id}-detail`}
          className="border-b border-border/40 bg-bg-raised/40"
        >
          <td colSpan={10} className="p-4">
            <TradeDetail trade={t} intel={intel} />
          </td>
        </tr>
      )}
    </>
  );
}

function TradeDetail({
  trade: t,
  intel,
}: {
  trade: Trade;
  intel: SymbolIntel | null;
}) {
  const quote = intel?.quote ?? null;
  const lastDecision = intel?.last_decision ?? null;
  const news = useMemo(() => (intel?.news ?? []).slice(0, 3), [intel]);

  const livePnlUsd = useMemo(() => {
    if (!quote || !t.entry_price || t.status !== "open") return null;
    const priceDelta = quote.current - t.entry_price;
    const qty = t.size_usd / t.entry_price;
    const sign = t.action.toUpperCase().includes("SHORT") ? -1 : 1;
    return sign * priceDelta * qty;
  }, [quote, t.entry_price, t.size_usd, t.action, t.status]);

  const livePnlPct = useMemo(() => {
    if (!quote || !t.entry_price || t.status !== "open") return null;
    const raw = (quote.current - t.entry_price) / t.entry_price;
    const sign = t.action.toUpperCase().includes("SHORT") ? -1 : 1;
    return sign * raw * 100;
  }, [quote, t.entry_price, t.action, t.status]);

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-6 text-xs">
      <section>
        <h3 className="text-[10px] uppercase tracking-widest text-text-dim mb-2">
          live_quote
        </h3>
        {quote ? (
          <dl className="space-y-1">
            <Kv label="current" value={`$${quote.current.toFixed(2)}`} />
            <Kv
              label="day_change"
              value={`${quote.change >= 0 ? "+" : ""}${quote.change.toFixed(2)} (${quote.change_pct >= 0 ? "+" : ""}${quote.change_pct.toFixed(2)}%)`}
              tone={quote.change >= 0 ? "accent" : "danger"}
            />
            <Kv label="open" value={`$${quote.open.toFixed(2)}`} />
            <Kv label="high" value={`$${quote.high.toFixed(2)}`} />
            <Kv label="low" value={`$${quote.low.toFixed(2)}`} />
            <Kv label="prev_close" value={`$${quote.prev_close.toFixed(2)}`} />
            <Kv
              label="as_of"
              value={fmtDateTime(quote.ts)}
              className="text-text-faint"
            />
          </dl>
        ) : (
          <p className="text-text-faint">
            no live quote — symbol not in watchlist / intel feed.
          </p>
        )}
      </section>

      <section>
        <h3 className="text-[10px] uppercase tracking-widest text-text-dim mb-2">
          trade_stats
        </h3>
        <dl className="space-y-1">
          <Kv
            label="entry"
            value={t.entry_price ? `$${t.entry_price.toFixed(2)}` : "—"}
          />
          <Kv
            label="exit"
            value={t.exit_price ? `$${t.exit_price.toFixed(2)}` : "—"}
          />
          <Kv label="size" value={`$${t.size_usd.toFixed(2)}`} />
          {t.entry_price && (
            <Kv
              label="qty"
              value={(t.size_usd / t.entry_price).toFixed(4)}
              className="text-text-faint"
            />
          )}
          {t.status === "open" && livePnlUsd !== null && livePnlPct !== null && (
            <Kv
              label="unrealized"
              tone={livePnlUsd >= 0 ? "accent" : "danger"}
              value={`${livePnlUsd >= 0 ? "+" : ""}$${livePnlUsd.toFixed(2)} (${livePnlPct >= 0 ? "+" : ""}${livePnlPct.toFixed(2)}%)`}
            />
          )}
          {t.status === "closed" && (
            <Kv
              label="realized"
              tone={
                t.realized_pnl_usd > 0
                  ? "accent"
                  : t.realized_pnl_usd < 0
                  ? "danger"
                  : undefined
              }
              value={`${t.realized_pnl_usd >= 0 ? "+" : ""}$${t.realized_pnl_usd.toFixed(2)}`}
            />
          )}
          <Kv
            label="opened"
            value={t.opened_at ? fmtDateTime(t.opened_at) : "—"}
            className="text-text-faint"
          />
          <Kv
            label="closed"
            value={t.closed_at ? fmtDateTime(t.closed_at) : "—"}
            className="text-text-faint"
          />
        </dl>
        {lastDecision && (
          <div className="mt-3 border-l-2 border-border pl-2">
            <p className="text-[10px] uppercase tracking-widest text-text-dim">
              last_decision
            </p>
            <p className="text-text">
              <span className="text-accent font-semibold">
                {lastDecision.action ?? "—"}
              </span>{" "}
              <span className="text-text-faint">
                · {fmtDateTime(lastDecision.created_at)}
              </span>
            </p>
            {lastDecision.rationale && (
              <p className="text-text-dim mt-1 leading-snug line-clamp-4">
                {lastDecision.rationale}
              </p>
            )}
          </div>
        )}
      </section>

      <section>
        <h3 className="text-[10px] uppercase tracking-widest text-text-dim mb-2">
          news
        </h3>
        {news.length === 0 ? (
          <p className="text-text-faint">no headlines cached.</p>
        ) : (
          <ul className="space-y-2">
            {news.map((n, i) => (
              <li key={`${n.url}-${i}`} className="leading-snug">
                <a
                  href={n.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-accent hover:underline"
                >
                  {n.headline}
                </a>
                <p className="text-text-faint text-[10px]">
                  {n.source} · {fmtDateTime(n.datetime)}
                </p>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function Kv({
  label,
  value,
  tone,
  className = "",
}: {
  label: string;
  value: string;
  tone?: "accent" | "danger";
  className?: string;
}) {
  const toneClass =
    tone === "accent"
      ? "text-accent"
      : tone === "danger"
      ? "text-danger"
      : "text-text";
  return (
    <div className="flex justify-between gap-4">
      <dt className="text-text-dim text-[10px] uppercase tracking-widest">
        {label}
      </dt>
      <dd className={`${toneClass} tabular ${className}`}>{value}</dd>
    </div>
  );
}

function StatusPill({ status }: { status: string }) {
  const map: Record<string, string> = {
    pending: "border-text-dim/50 text-text-dim bg-text-dim/5",
    open: "border-warn/50 text-warn bg-warn/10",
    closed: "border-accent/50 text-accent bg-accent/10",
    canceled: "border-text-faint/60 text-text-faint bg-text-faint/5",
    rejected: "border-danger/50 text-danger bg-danger/10",
    failed: "border-danger/50 text-danger bg-danger/10",
  };
  return (
    <span
      className={`border px-1.5 py-0.5 text-[10px] uppercase tracking-widest ${
        map[status] || "border-border text-text-dim"
      }`}
    >
      {status}
    </span>
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
    <th className={`text-${align} py-2 px-1 font-semibold`}>{children}</th>
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
    <td className={`text-${align} py-1.5 px-1 whitespace-nowrap ${className}`}>
      {children}
    </td>
  );
}
