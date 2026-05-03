"use client";

import type { ReactNode } from "react";
import { useMemo, useState } from "react";
import {
  Bar,
  CartesianGrid,
  ComposedChart,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  TooltipProps,
  XAxis,
  YAxis,
} from "recharts";

type Payload = Record<string, unknown> | null;

export interface ToolCardProps {
  name: string;
  args: Record<string, unknown>;
  payload: Payload;
  fallback: string;
}

export function ToolResultCard(props: ToolCardProps) {
  const { name, payload } = props;
  if (!payload) return <FallbackCard {...props} />;
  if (hasError(payload)) return <ErrorCard {...props} />;

  switch (name) {
    case "get_company_profile":
      return <ProfileCard payload={payload} />;
    case "get_quote":
      return <QuoteCard payload={payload} />;
    case "get_company_news":
      return <NewsCard payload={payload} />;
    case "get_peers":
      return <PeersCard payload={payload} />;
    case "get_basic_financials":
      return <FinancialsCard payload={payload} />;
    case "get_analyst_ratings":
      return <AnalystCard payload={payload} />;
    case "get_earnings":
      return <EarningsCard payload={payload} />;
    case "get_insider_transactions":
      return <InsiderCard payload={payload} />;
    case "get_ownership":
      return <OwnershipCard payload={payload} />;
    case "get_sec_filings":
      return <FilingsCard payload={payload} />;
    case "search_sec":
      return <SecSearchCard payload={payload} />;
    case "read_filing":
      return <ReadFilingCard payload={payload} />;
    case "get_technicals":
      return <TechnicalsCard payload={payload} />;
    case "get_market_context":
      return <MarketContextCard payload={payload} />;
    case "get_price_history":
      return (
        <PriceChart
          payload={payload}
          symbol={(payload.symbol as string) ?? (props.args.symbol as string) ?? ""}
        />
      );
    case "get_intraday_history":
      return (
        <IntradayChart
          payload={payload}
          symbol={(payload.symbol as string) ?? (props.args.symbol as string) ?? ""}
        />
      );
    default:
      return <FallbackCard {...props} />;
  }
}

// ============================================================================
// Generic cards

function FallbackCard({ name, args, fallback, payload }: ToolCardProps) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mt-2 text-[11px]">
      <div className="text-text-dim">
        <span className="text-accent-dim">{name}</span>{" "}
        <span className="text-text-faint">{formatArgs(args)}</span>{" "}
        <span className="text-text-dim">→ {fallback}</span>
      </div>
      {payload && (
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="text-text-faint hover:text-text-dim text-[10px] uppercase tracking-widest"
        >
          {open ? "hide raw" : "raw json"}
        </button>
      )}
      {open && payload && (
        <pre className="mt-1 bg-bg-raised border border-border p-2 overflow-x-auto text-[10px] text-text-dim max-h-64 overflow-y-auto">
          {JSON.stringify(payload, null, 2)}
        </pre>
      )}
    </div>
  );
}

function ErrorCard({ name, args, payload }: ToolCardProps) {
  const msg = (payload?.error as string) ?? "tool error";
  return (
    <div className="mt-2 text-[11px] text-warn">
      <span className="text-accent-dim">{name}</span>{" "}
      <span className="text-text-faint">{formatArgs(args)}</span>{" "}
      <span>× {msg}</span>
    </div>
  );
}

// ============================================================================
// Profile

function ProfileCard({ payload }: { payload: Payload }) {
  if (!payload) return null;
  const name = (payload.name as string) ?? (payload.title as string) ?? "—";
  const industry =
    (payload.finnhubIndustry as string) ??
    (payload.industry as string) ??
    null;
  const exchange = (payload.exchange as string) ?? null;
  const mcap = numberOrNull(payload.marketCapitalization);
  const website = (payload.weburl as string) ?? null;
  const logo = (payload.logo as string) ?? null;
  const country = (payload.country as string) ?? null;
  const ipo = (payload.ipo as string) ?? null;
  return (
    <div className="mt-2 frame p-3 bg-bg flex items-start gap-3">
      {logo && (
        /* eslint-disable-next-line @next/next/no-img-element */
        <img
          src={logo}
          alt=""
          className="w-10 h-10 object-contain bg-bg-raised border border-border"
        />
      )}
      <div className="flex-1 min-w-0 text-xs">
        <div className="text-sm text-text">
          <span className="text-accent">{(payload.ticker as string) ?? ""}</span>
          <span className="text-text-faint"> · </span>
          {name}
        </div>
        <div className="text-text-dim mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
          {industry && <span>industry: {industry}</span>}
          {exchange && <span>exchange: {exchange}</span>}
          {country && <span>country: {country}</span>}
          {ipo && <span>ipo: {ipo}</span>}
          {mcap != null && <span>mcap: ${formatCompact(mcap * 1_000_000)}</span>}
        </div>
        {website && (
          <a
            href={website}
            target="_blank"
            rel="noreferrer"
            className="text-accent hover:underline break-all"
          >
            {website}
          </a>
        )}
      </div>
    </div>
  );
}

// ============================================================================
// Quote

function QuoteCard({ payload }: { payload: Payload }) {
  if (!payload) return null;
  const symbol = (payload.symbol as string) ?? "";
  const current = numberOrNull(payload.current);
  const changePct = numberOrNull(payload.change_pct);
  const change = numberOrNull(payload.change);
  const open = numberOrNull(payload.open);
  const high = numberOrNull(payload.high);
  const low = numberOrNull(payload.low);
  const prev = numberOrNull(payload.prev_close);
  if (current == null) return null;
  const up = (changePct ?? 0) >= 0;
  const color = up ? "text-accent" : "text-danger";
  return (
    <div className="mt-2 frame p-3 bg-bg text-xs">
      <div className="flex items-baseline justify-between">
        <div>
          <span className="text-accent text-sm">{symbol}</span>
          <span className="ml-3 text-sm text-text tabular">
            ${current.toFixed(2)}
          </span>
          {change != null && changePct != null && (
            <span className={`ml-2 tabular ${color}`}>
              {up ? "+" : ""}
              {change.toFixed(2)} ({up ? "+" : ""}
              {changePct.toFixed(2)}%)
            </span>
          )}
        </div>
      </div>
      <div className="mt-2 grid grid-cols-4 gap-2 text-text-dim tabular">
        <MiniStat label="open" value={open != null ? `$${open.toFixed(2)}` : "—"} />
        <MiniStat label="high" value={high != null ? `$${high.toFixed(2)}` : "—"} />
        <MiniStat label="low" value={low != null ? `$${low.toFixed(2)}` : "—"} />
        <MiniStat
          label="prev close"
          value={prev != null ? `$${prev.toFixed(2)}` : "—"}
        />
      </div>
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-text-faint text-[10px] uppercase tracking-widest">
        {label}
      </div>
      <div>{value}</div>
    </div>
  );
}

// ============================================================================
// News

interface NewsItem {
  headline: string;
  summary?: string;
  source?: string;
  url?: string;
  datetime?: string;
}

function NewsCard({ payload }: { payload: Payload }) {
  if (!payload) return null;
  const items = (payload.items as NewsItem[] | undefined) ?? [];
  if (items.length === 0) {
    return (
      <div className="mt-2 text-[11px] text-text-faint">
        no recent headlines
      </div>
    );
  }
  return (
    <div className="mt-2 frame bg-bg divide-y divide-border">
      {items.slice(0, 10).map((n, i) => (
        <a
          key={(n.url ?? "") + i}
          href={n.url || undefined}
          target="_blank"
          rel="noreferrer"
          className="block px-3 py-2 text-xs hover:bg-bg-raised"
        >
          <div className="text-text">{n.headline}</div>
          <div className="text-text-faint text-[10px] mt-1 flex gap-2">
            {n.source && <span>{n.source}</span>}
            {n.datetime && <span>· {formatShortDate(n.datetime)}</span>}
          </div>
          {n.summary && (
            <div className="text-text-dim text-[11px] mt-1 line-clamp-2">
              {n.summary}
            </div>
          )}
        </a>
      ))}
    </div>
  );
}

// ============================================================================
// Peers

interface PeerRow {
  symbol: string;
  name?: string;
  industry?: string;
  exchange?: string;
  market_cap_usd_m?: number;
  industry_match?: boolean;
  cap_ratio?: number | null;
}

function PeerTable({ rows, tone }: { rows: PeerRow[]; tone: "strong" | "weak" }) {
  return (
    <div className="frame bg-bg overflow-x-auto">
      <table className="min-w-full text-xs">
        <thead>
          <tr className="text-text-faint text-[10px] uppercase tracking-widest">
            <th className="px-2 py-1 text-left">symbol</th>
            <th className="px-2 py-1 text-left">name</th>
            <th className="px-2 py-1 text-left">industry</th>
            <th className="px-2 py-1 text-right">mcap</th>
            <th className="px-2 py-1 text-right">cap×</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr
              key={p.symbol}
              className={
                "border-t border-border " +
                (tone === "weak" ? "text-text-dim" : "text-text")
              }
            >
              <td className="px-2 py-1 text-accent tabular">{p.symbol}</td>
              <td className="px-2 py-1 truncate max-w-[260px]">{p.name ?? "—"}</td>
              <td className="px-2 py-1 text-text-dim">{p.industry ?? "—"}</td>
              <td className="px-2 py-1 text-right tabular">
                {p.market_cap_usd_m != null
                  ? `$${formatCompact(p.market_cap_usd_m * 1_000_000)}`
                  : "—"}
              </td>
              <td className="px-2 py-1 text-right tabular text-text-faint">
                {p.cap_ratio != null ? `${p.cap_ratio}×` : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PeersCard({ payload }: { payload: Payload }) {
  if (!payload) return null;
  const subjectIndustry = (payload.subject_industry as string) ?? null;
  const recommended =
    (payload.recommended_peers as PeerRow[] | undefined) ?? [];
  const weak = (payload.weak_peers as PeerRow[] | undefined) ?? [];
  const detailed = (payload.peers_detailed as PeerRow[] | undefined) ?? [];
  const peers = (payload.peers as string[] | undefined) ?? [];
  const notes = (payload.notes as string[] | undefined) ?? [];
  const webQ = (payload.web_peer_search as
    | { query?: string; results?: Array<{ title?: string; url?: string }> }
    | undefined);
  const webResearch =
    (payload.web_research_results as
      | Array<{ query?: string; title?: string; url?: string; snippet?: string }>
      | undefined) ?? [];
  const webDiscovered =
    (payload.web_discovered_peers as PeerRow[] | undefined) ?? [];
  const hasAny =
    recommended.length +
      weak.length +
      detailed.length +
      peers.length +
      webDiscovered.length >
    0;
  if (!hasAny) {
    return <div className="mt-2 text-[11px] text-text-faint">no peers</div>;
  }
  // Prefer the sorted recommended/weak split from the backend; fall back
  // to peers_detailed / symbol-only list for older payloads.
  const strong =
    recommended.length > 0
      ? recommended
      : detailed.filter((p) => p.industry_match);
  const weakRows =
    recommended.length > 0
      ? weak
      : detailed.filter((p) => !p.industry_match);
  const noneFromBackend = strong.length === 0 && weakRows.length === 0;
  return (
    <div className="mt-2 space-y-2">
      {subjectIndustry && (
        <div className="text-[10px] uppercase tracking-widest text-text-faint">
          subject industry: {subjectIndustry}
        </div>
      )}
      {notes.length > 0 && (
        <div className="text-[11px] text-warn space-y-0.5">
          {notes.map((n, i) => (
            <div key={i}>· {n}</div>
          ))}
        </div>
      )}
      {strong.length > 0 && (
        <div className="space-y-1">
          <div className="text-[10px] uppercase tracking-widest text-accent-dim">
            strong peers ({strong.length})
          </div>
          <PeerTable rows={strong} tone="strong" />
        </div>
      )}
      {weakRows.length > 0 && (
        <div className="space-y-1">
          <div className="text-[10px] uppercase tracking-widest text-text-faint">
            weak / excluded ({weakRows.length})
          </div>
          <PeerTable rows={weakRows} tone="weak" />
        </div>
      )}
      {noneFromBackend && peers.length > 0 && (
        <PeerTable
          rows={peers.map((s) => ({ symbol: s }))}
          tone="weak"
        />
      )}
      {webDiscovered.length > 0 && (
        <div className="space-y-1">
          <div className="text-[10px] uppercase tracking-widest text-accent-dim">
            discovered via web research ({webDiscovered.length})
          </div>
          <PeerTable rows={webDiscovered} tone="strong" />
        </div>
      )}
      {webResearch.length > 0 && (
        <div className="space-y-1">
          <div className="text-[10px] uppercase tracking-widest text-text-faint">
            competitor web research ({webResearch.length})
          </div>
          <ul className="text-[11px] text-text-dim space-y-0.5">
            {webResearch.slice(0, 8).map((r, i) => (
              <li key={i} className="truncate">
                <span className="text-text-faint">· </span>
                {r.url ? (
                  <a
                    href={r.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-accent underline decoration-accent/30 hover:decoration-accent"
                  >
                    {r.title || r.url}
                  </a>
                ) : (
                  r.title
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {webResearch.length === 0 && webQ?.results && webQ.results.length > 0 && (
        <div className="space-y-1">
          <div className="text-[10px] uppercase tracking-widest text-text-faint">
            competitor web search{webQ.query ? ` · ${webQ.query}` : ""}
          </div>
          <ul className="text-[11px] text-text-dim space-y-0.5">
            {webQ.results.slice(0, 5).map((r, i) => (
              <li key={i} className="truncate">
                <span className="text-text-faint">· </span>
                {r.url ? (
                  <a
                    href={r.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-accent underline decoration-accent/30 hover:decoration-accent"
                  >
                    {r.title || r.url}
                  </a>
                ) : (
                  r.title
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Financials

function FinancialsCard({ payload }: { payload: Payload }) {
  if (!payload) return null;
  const s = (payload.summary as Record<string, number | null>) ?? {};
  const hi = numberOrNull(s["52WeekHigh"]);
  const lo = numberOrNull(s["52WeekLow"]);
  const pe = numberOrNull(
    s.peNormalizedAnnual ?? s.peBasicExclExtraTTM,
  );
  const beta = numberOrNull(s.beta);
  const mcap = numberOrNull(s.marketCapitalization);
  const gm = numberOrNull(s.grossMarginTTM);
  const om = numberOrNull(s.operatingMarginTTM);
  const nm = numberOrNull(s.netProfitMarginTTM);
  const div = numberOrNull(s.dividendYieldIndicatedAnnual);
  const revG = numberOrNull(s.revenueGrowthTTMYoy);
  const epsG = numberOrNull(s.epsGrowthTTMYoy);
  return (
    <div className="mt-2 frame p-3 bg-bg text-xs">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 tabular">
        <MetricPair label="52w range" value={hi != null && lo != null ? `$${lo.toFixed(2)} – $${hi.toFixed(2)}` : "—"} />
        <MetricPair label="P/E" value={pe != null ? pe.toFixed(1) : "—"} />
        <MetricPair label="beta" value={beta != null ? beta.toFixed(2) : "—"} />
        <MetricPair label="mcap" value={mcap != null ? `$${formatCompact(mcap * 1_000_000)}` : "—"} />
        <MetricPair label="rev growth" value={revG != null ? `${revG.toFixed(1)}%` : "—"} />
        <MetricPair label="eps growth" value={epsG != null ? `${epsG.toFixed(1)}%` : "—"} />
        <MetricPair label="gross margin" value={gm != null ? `${gm.toFixed(1)}%` : "—"} />
        <MetricPair label="op margin" value={om != null ? `${om.toFixed(1)}%` : "—"} />
        <MetricPair label="net margin" value={nm != null ? `${nm.toFixed(1)}%` : "—"} />
        <MetricPair label="div yield" value={div != null ? `${div.toFixed(2)}%` : "—"} />
      </div>
    </div>
  );
}

function MetricPair({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div>
      <div className="text-text-faint text-[10px] uppercase tracking-widest">
        {label}
      </div>
      <div className="text-text">{value}</div>
    </div>
  );
}

// ============================================================================
// Analyst

interface RecRow {
  period?: string;
  strongBuy?: number;
  buy?: number;
  hold?: number;
  sell?: number;
  strongSell?: number;
}

function AnalystCard({ payload }: { payload: Payload }) {
  if (!payload) {
    return (
      <div className="mt-2 frame p-3 bg-bg text-xs text-text-dim">
        analyst data unavailable
      </div>
    );
  }
  if (typeof payload.error === "string") {
    return (
      <div className="mt-2 frame p-3 bg-bg text-xs text-warn">
        analyst data unavailable — {payload.error}
      </div>
    );
  }
  const target = (payload.price_target as Record<string, unknown>) ?? {};
  const recs = (payload.recommendations as RecRow[] | undefined) ?? [];
  const latest = recs[0];
  const buy = (latest?.strongBuy ?? 0) + (latest?.buy ?? 0);
  const hold = latest?.hold ?? 0;
  const sell = (latest?.sell ?? 0) + (latest?.strongSell ?? 0);
  const total = buy + hold + sell || 1;
  const tMedian = numberOrNull(target.targetMedian);
  const tHigh = numberOrNull(target.targetHigh);
  const tLow = numberOrNull(target.targetLow);
  const tMean = numberOrNull(target.targetMean);
  const hasRecs = latest && (buy + hold + sell > 0);
  const hasTargets = tMedian != null || tHigh != null || tLow != null || tMean != null;
  if (!hasRecs && !hasTargets) {
    return (
      <div className="mt-2 frame p-3 bg-bg text-xs text-text-dim">
        no analyst coverage found
      </div>
    );
  }
  return (
    <div className="mt-2 frame p-3 bg-bg text-xs space-y-2">
      {latest && (
        <>
          <div className="text-text-faint text-[10px] uppercase tracking-widest">
            analyst consensus ({latest.period ?? "—"})
          </div>
          <div className="h-3 flex overflow-hidden border border-border bg-bg-raised">
            <div
              className="bg-accent/80"
              style={{ width: `${(buy / total) * 100}%` }}
              title={`${buy} buy`}
            />
            <div
              className="bg-text-faint/40"
              style={{ width: `${(hold / total) * 100}%` }}
              title={`${hold} hold`}
            />
            <div
              className="bg-danger/80"
              style={{ width: `${(sell / total) * 100}%` }}
              title={`${sell} sell`}
            />
          </div>
          <div className="flex justify-between text-[10px] tabular text-text-dim">
            <span className="text-accent">buy {buy}</span>
            <span>hold {hold}</span>
            <span className="text-danger">sell {sell}</span>
          </div>
        </>
      )}
      {(tMedian != null || tHigh != null || tLow != null) && (
        <div className="grid grid-cols-4 gap-2 tabular">
          <MetricPair label="target low" value={tLow != null ? `$${tLow.toFixed(2)}` : "—"} />
          <MetricPair label="median" value={tMedian != null ? `$${tMedian.toFixed(2)}` : "—"} />
          <MetricPair label="mean" value={tMean != null ? `$${tMean.toFixed(2)}` : "—"} />
          <MetricPair label="target high" value={tHigh != null ? `$${tHigh.toFixed(2)}` : "—"} />
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Earnings

function EarningsCard({ payload }: { payload: Payload }) {
  if (!payload) return null;
  const next = payload.next_earnings as Record<string, unknown> | null;
  const surprises = (payload.surprises as Array<Record<string, unknown>> | undefined) ?? [];
  return (
    <div className="mt-2 frame p-3 bg-bg text-xs space-y-2">
      {next && (
        <div>
          <div className="text-text-faint text-[10px] uppercase tracking-widest">
            next earnings
          </div>
          <div className="text-text">
            {(next.date as string) ?? "—"}{" "}
            {next.hour ? (
              <span className="text-text-dim">
                ({String(next.hour).toLowerCase()})
              </span>
            ) : null}
            {next.epsEstimate != null && (
              <span className="text-text-dim ml-2 tabular">
                EPS est {String(next.epsEstimate)}
              </span>
            )}
          </div>
        </div>
      )}
      {surprises.length > 0 && (
        <div>
          <div className="text-text-faint text-[10px] uppercase tracking-widest mb-1">
            recent quarters
          </div>
          <table className="min-w-full tabular">
            <thead>
              <tr className="text-text-faint text-[10px] uppercase">
                <th className="text-left">period</th>
                <th className="text-right">est</th>
                <th className="text-right">actual</th>
                <th className="text-right">surprise</th>
              </tr>
            </thead>
            <tbody>
              {surprises.slice(0, 5).map((s, i) => {
                const est = numberOrNull(s.estimate);
                const act = numberOrNull(s.actual);
                const surp = est != null && act != null ? act - est : null;
                const color =
                  surp == null
                    ? "text-text-dim"
                    : surp > 0
                      ? "text-accent"
                      : "text-danger";
                return (
                  <tr key={i} className="border-t border-border">
                    <td className="py-1">{(s.period as string) ?? "—"}</td>
                    <td className="text-right">{est != null ? est.toFixed(2) : "—"}</td>
                    <td className="text-right">{act != null ? act.toFixed(2) : "—"}</td>
                    <td className={"text-right " + color}>
                      {surp != null ? (surp > 0 ? "+" : "") + surp.toFixed(2) : "—"}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ============================================================================
// Insider

interface InsiderRow {
  name?: string;
  share?: number;
  change?: number;
  transactionDate?: string;
  transactionPrice?: number;
  transactionCode?: string;
  position?: string;
}

interface InsiderSummary {
  total_txns?: number;
  buy_count?: number;
  sell_count?: number;
  net_shares?: number;
  net_usd?: number;
  buy_usd?: number;
  sell_usd?: number;
  unique_insiders?: number;
  date_range?: { from?: string; to?: string };
}

interface InsiderWindow {
  buys?: number;
  sells?: number;
  net_shares?: number;
  net_usd?: number;
}

interface InsiderAgg {
  name?: string;
  title?: string;
  buys?: number;
  sells?: number;
  net_shares?: number;
  net_usd?: number;
  last_date?: string | null;
}

interface InsiderNotable {
  name?: string;
  date?: string;
  code?: string;
  shares?: number;
  price?: number | null;
  usd?: number;
}

function fmtUsd(n: number | undefined | null): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1_000_000_000) return `${sign}$${(abs / 1_000_000_000).toFixed(2)}B`;
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}K`;
  return `${sign}$${abs.toFixed(0)}`;
}

function fmtShares(n: number | undefined | null): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1_000_000) return `${sign}${(abs / 1_000_000).toFixed(2)}M`;
  if (abs >= 1_000) return `${sign}${(abs / 1_000).toFixed(1)}K`;
  return `${sign}${abs.toLocaleString()}`;
}

function InsiderCard({ payload }: { payload: Payload }) {
  if (!payload) {
    return (
      <div className="mt-2 text-[11px] text-text-faint">
        insider data unavailable
      </div>
    );
  }
  if (typeof payload.error === "string") {
    return (
      <div className="mt-2 text-[11px] text-warn">
        insider data unavailable — {payload.error}
      </div>
    );
  }
  const rows = (payload.rows as InsiderRow[] | undefined) ?? [];
  const summary = (payload.summary as InsiderSummary | undefined) ?? {};
  const windows = (payload.windows as Record<string, InsiderWindow> | undefined) ?? {};
  const topBuyers = (payload.top_buyers as InsiderAgg[] | undefined) ?? [];
  const topSellers = (payload.top_sellers as InsiderAgg[] | undefined) ?? [];
  const notable = (payload.notable as
    | { largest_buys?: InsiderNotable[]; largest_sells?: InsiderNotable[] }
    | undefined) ?? {};

  if (rows.length === 0) {
    return (
      <div className="mt-2 text-[11px] text-text-faint">
        no recent insider filings
      </div>
    );
  }

  const netUsd = summary.net_usd ?? 0;
  const netClass = netUsd > 0 ? "text-accent" : netUsd < 0 ? "text-danger" : "text-text-dim";

  return (
    <div className="mt-2 space-y-2">
      {/* Top strip: aggregate stats */}
      <div className="frame bg-bg px-3 py-2 grid grid-cols-2 md:grid-cols-5 gap-3 text-xs tabular">
        <MetricPair label="txns" value={String(summary.total_txns ?? rows.length)} />
        <MetricPair
          label="buys / sells"
          value={
            <>
              <span className="text-accent">{summary.buy_count ?? 0}</span>
              {" / "}
              <span className="text-danger">{summary.sell_count ?? 0}</span>
            </>
          }
        />
        <div>
          <div className="text-text-faint text-[10px] uppercase tracking-widest">
            net $
          </div>
          <div className={netClass}>{fmtUsd(netUsd)}</div>
        </div>
        <MetricPair label="net shares" value={fmtShares(summary.net_shares)} />
        <MetricPair label="unique insiders" value={String(summary.unique_insiders ?? "—")} />
      </div>

      {/* Time-window rollups */}
      <div className="frame bg-bg px-3 py-2 text-xs">
        <div className="text-text-faint text-[10px] uppercase tracking-widest mb-1">
          net flow by window
        </div>
        <div className="grid grid-cols-3 gap-3 tabular">
          {(["d30", "d90", "d365"] as const).map((k) => {
            const w = windows[k] ?? {};
            const wn = w.net_usd ?? 0;
            const cls = wn > 0 ? "text-accent" : wn < 0 ? "text-danger" : "text-text-dim";
            const label = k === "d30" ? "30d" : k === "d90" ? "90d" : "365d";
            return (
              <div key={k}>
                <div className="text-text-faint text-[10px] uppercase tracking-widest">{label}</div>
                <div className={cls}>{fmtUsd(wn)}</div>
                <div className="text-text-faint text-[10px]">
                  <span className="text-accent">{w.buys ?? 0}</span>
                  {" / "}
                  <span className="text-danger">{w.sells ?? 0}</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Top buyers + sellers side by side */}
      {(topBuyers.length > 0 || topSellers.length > 0) && (
        <div className="grid md:grid-cols-2 gap-2">
          <InsiderAggList title="top buyers (net)" rows={topBuyers} positive />
          <InsiderAggList title="top sellers (net)" rows={topSellers} />
        </div>
      )}

      {/* Notable transactions */}
      {((notable.largest_buys?.length ?? 0) > 0 ||
        (notable.largest_sells?.length ?? 0) > 0) && (
        <div className="frame bg-bg">
          <div className="px-3 py-2 text-[10px] uppercase tracking-widest text-text-faint border-b border-border">
            notable transactions
          </div>
          <div className="grid md:grid-cols-2 divide-x divide-border">
            <NotableList rows={notable.largest_buys ?? []} positive />
            <NotableList rows={notable.largest_sells ?? []} />
          </div>
        </div>
      )}

      {/* Raw rows collapsible */}
      <details className="frame bg-bg">
        <summary className="cursor-pointer px-3 py-2 text-[10px] uppercase tracking-widest text-text-faint hover:text-text-dim">
          raw form-4 rows ({rows.length})
        </summary>
        <div className="overflow-x-auto border-t border-border">
          <table className="min-w-full text-xs tabular">
            <thead>
              <tr className="text-text-faint text-[10px] uppercase tracking-widest">
                <th className="px-2 py-1 text-left">date</th>
                <th className="px-2 py-1 text-left">insider</th>
                <th className="px-2 py-1 text-left">title</th>
                <th className="px-2 py-1 text-right">shares</th>
                <th className="px-2 py-1 text-right">price</th>
                <th className="px-2 py-1 text-left">code</th>
              </tr>
            </thead>
            <tbody>
              {rows.slice(0, 30).map((r, i) => {
                const code = (r.transactionCode ?? "").toUpperCase();
                const isBuy = code.startsWith("P");
                const isSell = code.startsWith("S") || code === "F";
                const color = isBuy
                  ? "text-accent"
                  : isSell
                    ? "text-danger"
                    : "text-text-dim";
                return (
                  <tr key={i} className="border-t border-border">
                    <td className="px-2 py-1">{r.transactionDate ?? "—"}</td>
                    <td className="px-2 py-1 truncate max-w-[18ch]">
                      {r.name ?? "—"}
                    </td>
                    <td className="px-2 py-1 truncate max-w-[16ch] text-text-dim">
                      {r.position ?? "—"}
                    </td>
                    <td className={`px-2 py-1 text-right ${color}`}>
                      {r.change != null ? r.change.toLocaleString() : "—"}
                    </td>
                    <td className="px-2 py-1 text-right">
                      {r.transactionPrice != null
                        ? `$${r.transactionPrice.toFixed(2)}`
                        : "—"}
                    </td>
                    <td className={`px-2 py-1 ${color}`}>{code || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}

function InsiderAggList({
  title,
  rows,
  positive = false,
}: {
  title: string;
  rows: InsiderAgg[];
  positive?: boolean;
}) {
  if (rows.length === 0) return null;
  const cls = positive ? "text-accent" : "text-danger";
  return (
    <div className="frame bg-bg">
      <div className="px-3 py-2 text-[10px] uppercase tracking-widest text-text-faint border-b border-border">
        {title}
      </div>
      <div className="divide-y divide-border">
        {rows.slice(0, 10).map((r, i) => (
          <div
            key={i}
            className="px-3 py-1.5 flex items-center justify-between text-xs tabular"
          >
            <div className="min-w-0 flex-1">
              <div className="truncate text-text">{r.name ?? "—"}</div>
              {r.title && (
                <div className="truncate text-[10px] text-text-faint">
                  {r.title}
                </div>
              )}
            </div>
            <div className="text-right">
              <div className={cls}>{fmtUsd(r.net_usd)}</div>
              <div className="text-[10px] text-text-faint">
                {fmtShares(r.net_shares)} sh
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function NotableList({
  rows,
  positive = false,
}: {
  rows: InsiderNotable[];
  positive?: boolean;
}) {
  if (rows.length === 0) {
    return (
      <div className="px-3 py-3 text-[11px] text-text-faint">
        {positive ? "no notable buys" : "no notable sells"}
      </div>
    );
  }
  const cls = positive ? "text-accent" : "text-danger";
  return (
    <div className="divide-y divide-border">
      {rows.map((r, i) => (
        <div key={i} className="px-3 py-1.5 text-xs tabular">
          <div className="flex items-center justify-between gap-2">
            <span className="truncate flex-1 text-text">{r.name ?? "—"}</span>
            <span className={`${cls} shrink-0`}>{fmtUsd(r.usd)}</span>
          </div>
          <div className="text-[10px] text-text-faint flex gap-2">
            <span>{r.date ?? "—"}</span>
            <span>·</span>
            <span>{fmtShares(r.shares)} sh</span>
            {r.price != null && <span>@ ${r.price.toFixed(2)}</span>}
            <span>·</span>
            <span>{(r.code ?? "").toUpperCase() || "—"}</span>
          </div>
        </div>
      ))}
    </div>
  );
}

// ============================================================================
// Ownership (top institutional + fund shareholders)

interface OwnershipHolder {
  name?: string;
  shares?: number;
  share_change?: number;
  filed?: string;
  percent?: number;
  value_usd?: number;
}

function OwnershipCard({ payload }: { payload: Payload }) {
  if (!payload) {
    return (
      <div className="mt-2 text-[11px] text-text-faint">
        ownership data unavailable
      </div>
    );
  }
  if (typeof payload.error === "string") {
    return (
      <div className="mt-2 text-[11px] text-warn">
        ownership data unavailable — {payload.error}
      </div>
    );
  }
  const inst = (payload.institutions as OwnershipHolder[] | undefined) ?? [];
  const funds = (payload.funds as OwnershipHolder[] | undefined) ?? [];
  const priceUsed = payload.price_used as number | null | undefined;
  if (inst.length === 0 && funds.length === 0) {
    return <div className="mt-2 text-[11px] text-text-faint">no ownership data</div>;
  }
  return (
    <div className="mt-2 space-y-2">
      {priceUsed != null && (
        <div className="text-[10px] text-text-faint">
          values estimated at latest close ${priceUsed.toFixed(2)}
        </div>
      )}
      {inst.length > 0 && (
        <HolderTable title="top institutional holders" rows={inst} />
      )}
      {funds.length > 0 && <HolderTable title="top fund holders" rows={funds} />}
    </div>
  );
}

function HolderTable({ title, rows }: { title: string; rows: OwnershipHolder[] }) {
  return (
    <div className="frame bg-bg">
      <div className="px-3 py-2 text-[10px] uppercase tracking-widest text-text-faint border-b border-border">
        {title}
      </div>
      <div className="overflow-x-auto">
        <table className="min-w-full text-xs tabular">
          <thead>
            <tr className="text-text-faint text-[10px] uppercase tracking-widest">
              <th className="px-2 py-1 text-left">holder</th>
              <th className="px-2 py-1 text-right">shares</th>
              <th className="px-2 py-1 text-right">Δ shares</th>
              <th className="px-2 py-1 text-right">value</th>
              <th className="px-2 py-1 text-right">% port</th>
              <th className="px-2 py-1 text-left">filed</th>
            </tr>
          </thead>
          <tbody>
            {rows.slice(0, 10).map((r, i) => {
              const change = r.share_change ?? 0;
              const changeClass =
                change > 0
                  ? "text-accent"
                  : change < 0
                    ? "text-danger"
                    : "text-text-dim";
              return (
                <tr key={i} className="border-t border-border">
                  <td className="px-2 py-1 truncate max-w-[24ch]">
                    {r.name ?? "—"}
                  </td>
                  <td className="px-2 py-1 text-right">
                    {r.shares != null ? fmtShares(r.shares) : "—"}
                  </td>
                  <td className={`px-2 py-1 text-right ${changeClass}`}>
                    {change !== 0 ? fmtShares(change) : "—"}
                  </td>
                  <td className="px-2 py-1 text-right">
                    {r.value_usd != null ? fmtUsd(r.value_usd) : "—"}
                  </td>
                  <td className="px-2 py-1 text-right text-text-dim">
                    {r.percent != null ? `${r.percent.toFixed(2)}%` : "—"}
                  </td>
                  <td className="px-2 py-1 text-text-dim">{r.filed ?? "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ============================================================================
// Filings

interface Filing {
  form?: string;
  filed?: string;
  accession?: string;
  url?: string;
}

function FilingsCard({ payload }: { payload: Payload }) {
  if (!payload) return null;
  const rows = (payload.filings as Filing[] | undefined) ?? [];
  if (rows.length === 0) {
    return <div className="mt-2 text-[11px] text-text-faint">no filings</div>;
  }
  return (
    <div className="mt-2 frame bg-bg divide-y divide-border">
      {rows.slice(0, 12).map((f, i) => (
        <div
          key={(f.accession ?? "") + i}
          className="flex items-center gap-3 px-3 py-2 text-xs"
        >
          <FormPill form={f.form ?? ""} />
          <span className="text-text-dim tabular w-24">{f.filed ?? "—"}</span>
          {f.url ? (
            <a
              href={f.url}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline truncate flex-1"
            >
              {f.accession ?? "view"}
            </a>
          ) : (
            <span className="text-text-faint truncate flex-1">
              {f.accession ?? "—"}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

function FormPill({ form }: { form: string }) {
  const major = new Set(["10-K", "10-Q", "S-1", "S-4", "S-1/A", "10-K/A"]);
  const eventy = new Set(["8-K", "8-K/A"]);
  const bg = major.has(form)
    ? "bg-accent/15 text-accent border-accent/40"
    : eventy.has(form)
      ? "bg-warn/15 text-warn border-warn/40"
      : "bg-bg-raised text-text-dim border-border";
  return (
    <span
      className={`inline-block px-2 py-0.5 text-[10px] uppercase tracking-widest border ${bg} w-16 text-center tabular`}
    >
      {form || "—"}
    </span>
  );
}

function SecSearchCard({ payload }: { payload: Payload }) {
  if (!payload) return null;
  const hits = (payload.hits as Array<Record<string, unknown>> | undefined) ?? [];
  if (hits.length === 0) {
    return <div className="mt-2 text-[11px] text-text-faint">no hits</div>;
  }
  return (
    <div className="mt-2 frame bg-bg divide-y divide-border">
      {hits.slice(0, 10).map((h, i) => (
        <div key={i} className="px-3 py-2 text-xs">
          <div className="flex items-center gap-3">
            <FormPill form={(h.form as string) ?? ""} />
            <span className="text-text-dim tabular w-24">
              {(h.filed as string) ?? "—"}
            </span>
            <span className="text-text flex-1 truncate">
              {(h.company as string) ?? "—"}
            </span>
          </div>
          {h.search_url != null && (
            <a
              href={h.search_url as string}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline text-[10px] break-all"
            >
              {h.search_url as string}
            </a>
          )}
        </div>
      ))}
    </div>
  );
}

interface FilingHighlights {
  is_8k?: boolean;
  sections?: Record<string, string>;
  items_8k?: Array<{ item: string; title: string; excerpt: string }>;
  money?: Array<{ text: string; usd: number; context: string }>;
  share_actions?: Array<{
    action: string;
    count: number;
    raw: string;
    context: string;
  }>;
  percent_moves?: Array<{ pct: number; context: string }>;
}

const SECTION_LABELS: Record<string, string> = {
  overview: "business overview",
  risk_factors: "risk factors",
  mdna: "MD&A",
  liquidity: "liquidity & capital",
  revenue: "revenue",
  results_of_operations: "results of operations",
};

function ReadFilingCard({ payload }: { payload: Payload }) {
  const [openText, setOpenText] = useState(false);
  if (!payload) return null;
  const highlights = (payload.highlights as FilingHighlights | undefined) ?? {};
  const text = (payload.text as string) ?? "";
  const total = payload.total_chars as number | undefined;
  const items = highlights.items_8k ?? [];
  const sections = highlights.sections ?? {};
  const money = highlights.money ?? [];
  const shares = highlights.share_actions ?? [];
  const pcts = highlights.percent_moves ?? [];

  const hasAnything =
    items.length > 0 ||
    Object.keys(sections).length > 0 ||
    money.length > 0 ||
    shares.length > 0 ||
    pcts.length > 0;

  return (
    <div className="mt-2 space-y-2 text-xs">
      {items.length > 0 && (
        <div className="frame bg-bg">
          <div className="px-3 py-2 text-[10px] uppercase tracking-widest text-text-faint border-b border-border">
            8-K items
          </div>
          <div className="divide-y divide-border">
            {items.map((it, i) => (
              <div key={i} className="px-3 py-2">
                <div className="flex gap-2 items-baseline">
                  <span className="text-accent tabular shrink-0">{it.item}</span>
                  <span className="text-text truncate">{it.title}</span>
                </div>
                {it.excerpt && (
                  <div className="text-text-dim mt-1 leading-relaxed">
                    {it.excerpt}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {Object.keys(sections).length > 0 && (
        <div className="frame bg-bg divide-y divide-border">
          {Object.entries(sections).map(([key, excerpt]) => (
            <div key={key} className="px-3 py-2">
              <div className="text-[10px] uppercase tracking-widest text-text-faint mb-1">
                {SECTION_LABELS[key] ?? key.replace(/_/g, " ")}
              </div>
              <div className="text-text-dim leading-relaxed">{excerpt}</div>
            </div>
          ))}
        </div>
      )}

      {(money.length > 0 || shares.length > 0 || pcts.length > 0) && (
        <div className="frame bg-bg">
          <div className="px-3 py-2 text-[10px] uppercase tracking-widest text-text-faint border-b border-border">
            notable figures
          </div>
          <div className="divide-y divide-border">
            {money.slice(0, 6).map((m, i) => (
              <div key={`m${i}`} className="px-3 py-1.5 flex gap-3">
                <span className="text-accent tabular shrink-0 w-20">
                  {fmtUsd(m.usd)}
                </span>
                <span className="text-text-dim flex-1">{m.context}</span>
              </div>
            ))}
            {shares.slice(0, 4).map((s, i) => (
              <div key={`s${i}`} className="px-3 py-1.5 flex gap-3">
                <span className="text-warn tabular shrink-0 w-20 uppercase text-[10px]">
                  {s.action}
                </span>
                <span className="text-text-dim flex-1">{s.context}</span>
              </div>
            ))}
            {pcts.slice(0, 4).map((p, i) => (
              <div key={`p${i}`} className="px-3 py-1.5 flex gap-3">
                <span className="text-accent tabular shrink-0 w-20">
                  {p.pct.toFixed(1)}%
                </span>
                <span className="text-text-dim flex-1">{p.context}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {!hasAnything && !text && (
        <div className="text-[11px] text-text-faint">
          nothing extracted from this filing
        </div>
      )}

      {text && (
        <details
          className="frame bg-bg"
          open={openText}
          onToggle={(e) => setOpenText((e.target as HTMLDetailsElement).open)}
        >
          <summary className="cursor-pointer px-3 py-2 text-[10px] uppercase tracking-widest text-text-faint hover:text-text-dim flex justify-between">
            <span>raw filing text</span>
            <span>
              {text.length.toLocaleString()} chars
              {total && total > text.length ? ` of ${total.toLocaleString()}` : ""}
            </span>
          </summary>
          <div className="px-3 py-2 max-h-[500px] overflow-y-auto whitespace-pre-wrap text-text-dim leading-relaxed border-t border-border">
            {text}
          </div>
        </details>
      )}
    </div>
  );
}

// ============================================================================
// Technicals

function TechnicalsCard({ payload }: { payload: Payload }) {
  if (!payload) return null;
  const last = numberOrNull(payload.last_close);
  const sma20 = numberOrNull(payload.sma_20);
  const sma50 = numberOrNull(payload.sma_50);
  const sma200 = numberOrNull(payload.sma_200);
  const rsi = numberOrNull(payload.rsi14);
  const macd = numberOrNull(payload.macd);
  const macdSignal = numberOrNull(payload.macd_signal);
  const macdHist = numberOrNull(payload.macd_hist);
  const atr = numberOrNull(payload.atr14);
  const hi52 = numberOrNull(payload.high_52w);
  const lo52 = numberOrNull(payload.low_52w);
  const pctHi = numberOrNull(payload.pct_from_52w_high);
  const pctLo = numberOrNull(payload.pct_from_52w_low);
  const rsiColor =
    rsi == null
      ? "text-text-dim"
      : rsi >= 70
        ? "text-danger"
        : rsi <= 30
          ? "text-accent"
          : "text-text";
  return (
    <div className="mt-2 frame p-3 bg-bg text-xs space-y-3">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 tabular">
        <MetricPair label="close" value={last != null ? `$${last.toFixed(2)}` : "—"} />
        <div>
          <div className="text-text-faint text-[10px] uppercase tracking-widest">
            RSI-14
          </div>
          <div className={rsiColor}>{rsi != null ? rsi.toFixed(0) : "—"}</div>
        </div>
        <MetricPair label="ATR-14" value={atr != null ? `$${atr.toFixed(2)}` : "—"} />
        <MetricPair
          label="52w hi/lo"
          value={hi52 != null && lo52 != null ? `$${lo52.toFixed(2)}–${hi52.toFixed(2)}` : "—"}
        />
        <MetricPair label="SMA-20" value={sma20 != null ? `$${sma20.toFixed(2)}` : "—"} />
        <MetricPair label="SMA-50" value={sma50 != null ? `$${sma50.toFixed(2)}` : "—"} />
        <MetricPair label="SMA-200" value={sma200 != null ? `$${sma200.toFixed(2)}` : "—"} />
        <MetricPair
          label="vs 52w hi"
          value={pctHi != null ? `${pctHi.toFixed(1)}%` : "—"}
        />
        <MetricPair
          label="vs 52w lo"
          value={pctLo != null ? `${pctLo > 0 ? "+" : ""}${pctLo.toFixed(1)}%` : "—"}
        />
        <MetricPair label="MACD" value={macd != null ? macd.toFixed(3) : "—"} />
        <MetricPair
          label="MACD signal"
          value={macdSignal != null ? macdSignal.toFixed(3) : "—"}
        />
        <MetricPair
          label="MACD hist"
          value={macdHist != null ? macdHist.toFixed(3) : "—"}
        />
      </div>
    </div>
  );
}

// ============================================================================
// Market context

function MarketContextCard({ payload }: { payload: Payload }) {
  if (!payload) return null;
  const rows = (payload.snapshots as Array<Record<string, unknown>> | undefined) ?? [];
  if (rows.length === 0) {
    return <div className="mt-2 text-[11px] text-text-faint">no snapshots</div>;
  }
  return (
    <div className="mt-2 frame bg-bg p-2 grid grid-cols-3 md:grid-cols-7 gap-1 text-xs">
      {rows.map((r) => {
        const sym = r.symbol as string;
        const cur = numberOrNull(r.current);
        const chg = numberOrNull(r.change_pct);
        const color = chg == null ? "text-text-dim" : chg >= 0 ? "text-accent" : "text-danger";
        return (
          <div key={sym} className="bg-bg-raised border border-border p-2 tabular">
            <div className="text-text-faint text-[10px] uppercase tracking-widest">
              {sym}
            </div>
            <div className="text-text">{cur != null ? cur.toFixed(2) : "—"}</div>
            <div className={`text-[10px] ${color}`}>
              {chg != null ? `${chg >= 0 ? "+" : ""}${chg.toFixed(2)}%` : ""}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ============================================================================
// Charts — daily (with volume, SMA, ref lines, trade markers, TF picker)

export interface Bar {
  t: string;
  o: number;
  h: number;
  l: number;
  c: number;
  v: number;
}

interface TradeMark {
  kind: "entry" | "exit";
  action: string;
  price: number;
  date: string;
  pnl?: number;
}

type TF = "1M" | "3M" | "6M" | "1Y" | "ALL";

function PriceChart({ payload, symbol }: { payload: Payload; symbol: string }) {
  const allBars = useMemo(
    () =>
      parseBars((payload?.bars as Array<Record<string, unknown>> | undefined) ?? []),
    [payload],
  );
  const trades = (payload?.our_trades as TradeMark[] | undefined) ?? [];
  const earningsDates = (payload?.earnings_dates as string[] | undefined) ?? [];
  const [tf, setTf] = useState<TF>(pickInitialTF(allBars.length));
  const [showVolume, setShowVolume] = useState(true);
  const [showSMA20, setShowSMA20] = useState(true);
  const [showSMA50, setShowSMA50] = useState(true);
  const [showSMA200, setShowSMA200] = useState(false);

  const bars = useMemo(() => filterByTF(allBars, tf), [allBars, tf]);
  const enriched = useMemo(
    () => enrichWithSMAs(allBars, bars.length),
    [allBars, bars.length],
  );
  const { high52w, low52w } = useMemo(() => compute52w(allBars), [allBars]);

  if (bars.length === 0) {
    return (
      <div className="mt-2 text-[11px] text-text-faint">no bar data</div>
    );
  }

  const first = bars[0].c;
  const last = bars[bars.length - 1].c;
  const move = first ? ((last - first) / first) * 100 : 0;
  const up = move >= 0;
  const priceColor = up ? "#22d39b" : "#ff5c7a";

  const entryDots = trades
    .filter((t) => t.kind === "entry")
    .map((t) => ({ date: t.date.slice(0, 10), value: t.price }));
  const exitDots = trades
    .filter((t) => t.kind === "exit")
    .map((t) => ({ date: t.date.slice(0, 10), value: t.price }));
  const earningsInRange = earningsDates.filter((d) =>
    bars.some((b) => b.t.slice(0, 10) === d),
  );

  return (
    <div className="mt-2 frame p-2 bg-bg">
      <div className="flex items-baseline justify-between flex-wrap gap-2 text-[11px]">
        <div className="text-text-dim">
          <span className="text-accent">{symbol}</span>
          <span className="text-text-faint ml-2">daily · {bars.length} bars</span>
        </div>
        <div className="tabular text-text-dim">
          ${first.toFixed(2)} → ${last.toFixed(2)}{" "}
          <span style={{ color: priceColor }}>
            ({up ? "+" : ""}
            {move.toFixed(1)}%)
          </span>
        </div>
      </div>

      <div className="flex flex-wrap gap-1 mt-2 text-[10px]">
        <TfGroup tf={tf} setTf={setTf} totalBars={allBars.length} />
        <div className="flex-1" />
        <ToggleChip label="VOL" on={showVolume} onClick={() => setShowVolume((v) => !v)} />
        <ToggleChip label="SMA20" on={showSMA20} onClick={() => setShowSMA20((v) => !v)} />
        <ToggleChip label="SMA50" on={showSMA50} onClick={() => setShowSMA50((v) => !v)} />
        <ToggleChip label="SMA200" on={showSMA200} onClick={() => setShowSMA200((v) => !v)} />
      </div>

      <ResponsiveContainer width="100%" height={260} minWidth={0}>
        <ComposedChart
          data={enriched.slice(-bars.length)}
          margin={{ top: 6, right: 8, left: 0, bottom: 0 }}
        >
          <CartesianGrid stroke="#1a2631" strokeDasharray="2 2" />
          <XAxis
            dataKey="date"
            tick={{ fill: "#6b7d8a", fontSize: 10 }}
            stroke="#1a2631"
            tickFormatter={(d: string) => d.slice(5)}
            minTickGap={28}
          />
          <YAxis
            yAxisId="price"
            domain={["auto", "auto"]}
            tick={{ fill: "#6b7d8a", fontSize: 10 }}
            stroke="#1a2631"
            width={48}
            tickFormatter={(v: number) => `$${v.toFixed(0)}`}
          />
          {showVolume && (
            <YAxis
              yAxisId="volume"
              orientation="right"
              tick={{ fill: "#6b7d8a", fontSize: 10 }}
              stroke="#1a2631"
              width={44}
              tickFormatter={(v: number) => formatCompact(v)}
              domain={[0, "dataMax"]}
            />
          )}
          <Tooltip content={<DailyBarTooltip />} />
          {showVolume && (
            <Bar
              yAxisId="volume"
              dataKey="v"
              fill="#1a2631"
              opacity={0.9}
              isAnimationActive={false}
            />
          )}
          {high52w != null && (
            <ReferenceLine
              yAxisId="price"
              y={high52w}
              stroke="#22d39b"
              strokeDasharray="3 3"
              strokeOpacity={0.4}
              label={{ value: "52w hi", fill: "#22d39b", fontSize: 10, position: "insideTopRight" }}
            />
          )}
          {low52w != null && (
            <ReferenceLine
              yAxisId="price"
              y={low52w}
              stroke="#ff5c7a"
              strokeDasharray="3 3"
              strokeOpacity={0.4}
              label={{ value: "52w lo", fill: "#ff5c7a", fontSize: 10, position: "insideBottomRight" }}
            />
          )}
          {earningsInRange.map((d) => (
            <ReferenceLine
              key={d}
              yAxisId="price"
              x={d}
              stroke="#f5b84a"
              strokeOpacity={0.35}
              strokeDasharray="2 4"
              label={{ value: "E", fill: "#f5b84a", fontSize: 9, position: "insideTopLeft" }}
            />
          ))}
          <Line
            yAxisId="price"
            type="monotone"
            dataKey="c"
            stroke={priceColor}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
          {showSMA20 && (
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="sma20"
              stroke="#8ecae6"
              strokeWidth={1}
              strokeDasharray="3 2"
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          )}
          {showSMA50 && (
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="sma50"
              stroke="#ffb703"
              strokeWidth={1}
              strokeDasharray="3 2"
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          )}
          {showSMA200 && (
            <Line
              yAxisId="price"
              type="monotone"
              dataKey="sma200"
              stroke="#b388ff"
              strokeWidth={1}
              strokeDasharray="3 2"
              dot={false}
              isAnimationActive={false}
              connectNulls
            />
          )}
          {entryDots.length > 0 && (
            <Scatter
              yAxisId="price"
              data={entryDots}
              dataKey="value"
              fill="#22d39b"
              shape="triangle"
              isAnimationActive={false}
            />
          )}
          {exitDots.length > 0 && (
            <Scatter
              yAxisId="price"
              data={exitDots}
              dataKey="value"
              fill="#ff5c7a"
              shape="triangle"
              isAnimationActive={false}
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>

      <ChartLegend
        showSMA20={showSMA20}
        showSMA50={showSMA50}
        showSMA200={showSMA200}
        hasTrades={entryDots.length > 0 || exitDots.length > 0}
        hasEarnings={earningsInRange.length > 0}
      />
    </div>
  );
}

function IntradayChart({
  payload,
  symbol,
}: {
  payload: Payload;
  symbol: string;
}) {
  const bars = useMemo(
    () =>
      parseBars((payload?.bars as Array<Record<string, unknown>> | undefined) ?? []),
    [payload],
  );
  const tf = (payload?.timeframe as string) ?? "";
  if (bars.length === 0) {
    return <div className="mt-2 text-[11px] text-text-faint">no intraday bars</div>;
  }
  const first = bars[0].c;
  const last = bars[bars.length - 1].c;
  const move = first ? ((last - first) / first) * 100 : 0;
  const up = move >= 0;
  const priceColor = up ? "#22d39b" : "#ff5c7a";
  const data = bars.map((b) => ({
    t: b.t.slice(5, 16).replace("T", " "),
    c: b.c,
    v: b.v,
  }));
  return (
    <div className="mt-2 frame p-2 bg-bg">
      <div className="flex items-baseline justify-between text-[11px] mb-1">
        <div className="text-text-dim">
          <span className="text-accent">{symbol}</span>
          <span className="text-text-faint ml-2">
            intraday {tf} · {bars.length} bars
          </span>
        </div>
        <div className="tabular text-text-dim">
          ${first.toFixed(2)} → ${last.toFixed(2)}{" "}
          <span style={{ color: priceColor }}>
            ({up ? "+" : ""}
            {move.toFixed(2)}%)
          </span>
        </div>
      </div>
      <ResponsiveContainer width="100%" height={180} minWidth={0}>
        <ComposedChart data={data} margin={{ top: 4, right: 6, left: 0, bottom: 0 }}>
          <CartesianGrid stroke="#1a2631" strokeDasharray="2 2" />
          <XAxis
            dataKey="t"
            tick={{ fill: "#6b7d8a", fontSize: 10 }}
            stroke="#1a2631"
            minTickGap={40}
          />
          <YAxis
            yAxisId="price"
            domain={["auto", "auto"]}
            tick={{ fill: "#6b7d8a", fontSize: 10 }}
            stroke="#1a2631"
            width={48}
            tickFormatter={(v: number) => `$${v.toFixed(2)}`}
          />
          <YAxis
            yAxisId="volume"
            orientation="right"
            tick={{ fill: "#6b7d8a", fontSize: 10 }}
            stroke="#1a2631"
            width={44}
            tickFormatter={(v: number) => formatCompact(v)}
            domain={[0, "dataMax"]}
          />
          <Tooltip
            contentStyle={{
              background: "#0e1620",
              border: "1px solid #1a2631",
              fontSize: 11,
            }}
            labelStyle={{ color: "#6b7d8a" }}
          />
          <Bar
            yAxisId="volume"
            dataKey="v"
            fill="#1a2631"
            opacity={0.9}
            isAnimationActive={false}
          />
          <Line
            yAxisId="price"
            type="monotone"
            dataKey="c"
            stroke={priceColor}
            strokeWidth={1.5}
            dot={false}
            isAnimationActive={false}
          />
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  );
}

function DailyBarTooltip(props: TooltipProps<number, string>) {
  const p = props as TooltipProps<number, string> & {
    payload?: Array<{ payload?: Record<string, number | string | null> }>;
    label?: string;
  };
  const { active, payload, label } = p;
  if (!active || !payload || payload.length === 0) return null;
  const row = (payload[0].payload ?? {}) as Record<string, number | string | null>;
  const c = Number(row.c);
  const o = Number(row.o);
  const h = Number(row.h);
  const l = Number(row.l);
  const v = Number(row.v);
  const sma20 = row.sma20 as number | null | undefined;
  const sma50 = row.sma50 as number | null | undefined;
  const sma200 = row.sma200 as number | null | undefined;
  return (
    <div
      style={{
        background: "#0e1620",
        border: "1px solid #1a2631",
        fontSize: 11,
        padding: 6,
        color: "#c9d1d9",
      }}
      className="tabular"
    >
      <div style={{ color: "#6b7d8a", marginBottom: 2 }}>{label}</div>
      <div>O ${o.toFixed(2)} H ${h.toFixed(2)}</div>
      <div>L ${l.toFixed(2)} C ${c.toFixed(2)}</div>
      <div style={{ color: "#6b7d8a" }}>vol {formatCompact(v)}</div>
      {sma20 != null && <div style={{ color: "#8ecae6" }}>SMA20 ${Number(sma20).toFixed(2)}</div>}
      {sma50 != null && <div style={{ color: "#ffb703" }}>SMA50 ${Number(sma50).toFixed(2)}</div>}
      {sma200 != null && <div style={{ color: "#b388ff" }}>SMA200 ${Number(sma200).toFixed(2)}</div>}
    </div>
  );
}

function TfGroup({
  tf,
  setTf,
  totalBars,
}: {
  tf: TF;
  setTf: (v: TF) => void;
  totalBars: number;
}) {
  const opts: TF[] = ["1M", "3M", "6M", "1Y", "ALL"];
  const minBars: Record<TF, number> = {
    "1M": 0,
    "3M": 23,
    "6M": 67,
    "1Y": 133,
    ALL: 0,
  };
  return (
    <div className="flex border border-border">
      {opts.map((o) => {
        const disabled = totalBars < minBars[o];
        return (
          <button
            key={o}
            type="button"
            onClick={() => !disabled && setTf(o)}
            disabled={disabled}
            title={disabled ? `need ≥ ${minBars[o]} bars` : undefined}
            className={
              "px-2 py-0.5 tabular " +
              (tf === o
                ? "bg-accent/15 text-accent"
                : disabled
                  ? "text-text-faint/50 cursor-not-allowed"
                  : "text-text-dim hover:text-text")
            }
          >
            {o}
          </button>
        );
      })}
    </div>
  );
}

function ToggleChip({
  label,
  on,
  onClick,
}: {
  label: string;
  on: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={
        "px-2 py-0.5 border tabular " +
        (on
          ? "border-accent/40 text-accent"
          : "border-border text-text-faint hover:text-text-dim")
      }
    >
      {label}
    </button>
  );
}

function ChartLegend({
  showSMA20,
  showSMA50,
  showSMA200,
  hasTrades,
  hasEarnings,
}: {
  showSMA20: boolean;
  showSMA50: boolean;
  showSMA200: boolean;
  hasTrades: boolean;
  hasEarnings: boolean;
}) {
  const items: Array<{ color: string; label: string; style?: "dash" | "solid" | "dot" }> = [];
  if (showSMA20) items.push({ color: "#8ecae6", label: "SMA20", style: "dash" });
  if (showSMA50) items.push({ color: "#ffb703", label: "SMA50", style: "dash" });
  if (showSMA200) items.push({ color: "#b388ff", label: "SMA200", style: "dash" });
  if (hasTrades) {
    items.push({ color: "#22d39b", label: "entry", style: "dot" });
    items.push({ color: "#ff5c7a", label: "exit", style: "dot" });
  }
  if (hasEarnings) items.push({ color: "#f5b84a", label: "earnings", style: "dash" });
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-3 mt-1 text-[10px] text-text-faint tabular">
      {items.map((it) => (
        <span key={it.label} className="flex items-center gap-1">
          <span
            style={{ background: it.color, width: 10, height: 2, borderRadius: 1 }}
          />
          {it.label}
        </span>
      ))}
    </div>
  );
}

// ============================================================================
// Helpers

function hasError(payload: Payload): boolean {
  return !!payload && typeof payload.error === "string";
}

function numberOrNull(x: unknown): number | null {
  if (typeof x === "number" && Number.isFinite(x)) return x;
  if (typeof x === "string" && x.trim() !== "") {
    const n = Number(x);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function formatCompact(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e12) return (n / 1e12).toFixed(1) + "T";
  if (abs >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return n.toFixed(0);
}

function formatShortDate(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  } catch {
    return iso.slice(0, 10);
  }
}

function formatArgs(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(args)) {
    const s =
      typeof v === "string"
        ? v.length > 40
          ? v.slice(0, 40) + "…"
          : v
        : JSON.stringify(v);
    parts.push(`${k}=${s}`);
  }
  return parts.join(" · ");
}

function parseBars(raw: Array<Record<string, unknown>>): Bar[] {
  const out: Bar[] = [];
  for (const b of raw) {
    const c = Number(b.c);
    if (!Number.isFinite(c)) continue;
    out.push({
      t: String(b.t ?? ""),
      o: Number(b.o),
      h: Number(b.h),
      l: Number(b.l),
      c,
      v: Number(b.v),
    });
  }
  return out;
}

function pickInitialTF(totalBars: number): TF {
  if (totalBars <= 22) return "1M";
  if (totalBars <= 66) return "3M";
  if (totalBars <= 132) return "6M";
  return "1Y";
}

function filterByTF(bars: Bar[], tf: TF): Bar[] {
  const windows: Record<TF, number> = {
    "1M": 22,
    "3M": 66,
    "6M": 132,
    "1Y": 252,
    ALL: bars.length,
  };
  const n = windows[tf];
  return bars.slice(-n);
}

function sma(values: number[], window: number, index: number): number | null {
  if (index < window - 1) return null;
  let sum = 0;
  for (let i = index - window + 1; i <= index; i++) sum += values[i];
  return sum / window;
}

interface EnrichedBar extends Bar {
  sma20: number | null;
  sma50: number | null;
  sma200: number | null;
  date: string;
}

function enrichWithSMAs(allBars: Bar[], visible: number): EnrichedBar[] {
  const closes = allBars.map((b) => b.c);
  const out: EnrichedBar[] = allBars.map((b, i) => ({
    ...b,
    date: b.t.slice(0, 10),
    sma20: sma(closes, 20, i),
    sma50: sma(closes, 50, i),
    sma200: sma(closes, 200, i),
  }));
  return out.slice(-visible);
}

function compute52w(bars: Bar[]): { high52w: number | null; low52w: number | null } {
  if (bars.length === 0) return { high52w: null, low52w: null };
  const tail = bars.slice(-252);
  let hi = -Infinity;
  let lo = Infinity;
  for (const b of tail) {
    if (b.h > hi) hi = b.h;
    if (b.l < lo) lo = b.l;
  }
  return {
    high52w: Number.isFinite(hi) ? hi : null,
    low52w: Number.isFinite(lo) ? lo : null,
  };
}
