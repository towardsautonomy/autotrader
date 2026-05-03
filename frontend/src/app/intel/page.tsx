"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  Candidate,
  Discovery,
  MarketIntel,
  Mover,
  NewsItem,
  Quote,
  SymbolDecision,
  SymbolIntel,
} from "@/lib/api";
import { fmtDateTime, fmtTime, fmtTimeHM } from "@/lib/time";
import { useRefreshOnResume } from "@/lib/useRefreshOnResume";

export default function IntelPage() {
  const [intel, setIntel] = useState<MarketIntel | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const load = useCallback(
    () =>
      api
        .intel()
        .then((d) => {
          setIntel(d);
          setError(null);
          setSelected(
            (s) =>
              s ?? d.candidates[0]?.symbol ?? d.symbols[0]?.symbol ?? null,
          );
        })
        .catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    load();
    const i = setInterval(load, 30_000);
    return () => clearInterval(i);
  }, [load]);

  useRefreshOnResume(load);

  if (error && !intel) {
    return (
      <div className="frame p-4 text-danger text-sm">
        <span className="text-text-faint">[err]</span> {error}
      </div>
    );
  }
  if (!intel) {
    return (
      <div className="text-text-dim text-sm">
        <span className="blink text-accent">▊</span> pulling context...
      </div>
    );
  }

  const focus = intel.symbols.find((s) => s.symbol === selected) ?? intel.symbols[0];

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between flex-wrap gap-4">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <span className="text-accent">▸</span>
            market_intel
            <span className="text-text-faint text-xs">// what the AI sees</span>
          </h1>
          <p className="text-xs text-text-dim mt-1 leading-relaxed max-w-2xl">
            Live context surface fed into every prompt: quotes, recent news per
            ticker, open position, and the most recent AI verdict. Refreshes
            every 30s. News layer is{" "}
            <span
              className={intel.news_enabled ? "text-accent" : "text-warn"}
            >
              {intel.news_enabled ? "enabled (Finnhub)" : "disabled — set FINNHUB_API_KEY"}
            </span>
            .
          </p>
        </div>
        <span className="text-[10px] text-text-faint tabular">
          last_sync {fmtTime(intel.checked_at)} PT
        </span>
      </header>

      <CandidatesPanel
        candidates={intel.candidates}
        symbols={intel.symbols}
        activeSymbol={focus?.symbol ?? null}
        onPick={setSelected}
      />

      {focus && <FocusPanel s={focus} />}

      <DiscoverySection discovery={intel.discovery} onPick={setSelected} />

      <section>
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-sm uppercase tracking-widest text-text-dim">
            <span className="text-accent">▸</span> market_news
          </h2>
          <span className="text-[10px] text-text-faint tabular">
            [{intel.market_news.length.toString().padStart(2, "0")}]
          </span>
        </div>
        <NewsList items={intel.market_news} empty="no market news available" />
      </section>
    </div>
  );
}

function CandidatesPanel({
  candidates,
  symbols,
  activeSymbol,
  onPick,
}: {
  candidates: Candidate[];
  symbols: SymbolIntel[];
  activeSymbol: string | null;
  onPick: (sym: string) => void;
}) {
  const quoteBySymbol = new Map(symbols.map((s) => [s.symbol, s.quote]));

  return (
    <section className="frame p-4 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-sm uppercase tracking-widest text-text-dim">
          <span className="text-accent">▸</span> candidates · what the ai is
          thinking about
          <span className="ml-2 text-[10px] text-text-faint normal-case tracking-normal">
            positions · recent verdicts · fresh movers — ranked
          </span>
        </h2>
        <span className="text-[10px] text-text-faint tabular">
          [{candidates.length.toString().padStart(2, "0")}]
        </span>
      </div>

      {candidates.length === 0 ? (
        <p className="text-text-faint text-xs">
          no active candidates — no positions, no approved decisions, and
          discovery is quiet. AI will hold this cycle.
        </p>
      ) : (
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2">
          {candidates.map((c) => (
            <CandidateCard
              key={`${c.symbol}-${c.rank}`}
              c={c}
              q={quoteBySymbol.get(c.symbol) ?? null}
              active={activeSymbol === c.symbol}
              onClick={() => onPick(c.symbol)}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function CandidateCard({
  c,
  q,
  active,
  onClick,
}: {
  c: Candidate;
  q: Quote | null;
  active: boolean;
  onClick: () => void;
}) {
  const reasonStyle: Record<Candidate["reason"], string> = {
    position: "text-warn border-warn/60 bg-warn/5",
    recent_approved: "text-accent border-accent/60 bg-accent/5",
    discovery: "text-accent-dim border-accent-dim/60 bg-accent-dim/5",
    shortlist: "text-accent-dim border-accent-dim/40 bg-accent-dim/5",
  };
  const reasonLabel: Record<Candidate["reason"], string> = {
    position: "held",
    recent_approved: "approved",
    discovery: "discovery",
    shortlist: "needle",
  };
  const pct = q?.change_pct ?? null;
  const toneClass =
    pct === null
      ? "text-text-dim"
      : pct > 0
        ? "text-accent"
        : pct < 0
          ? "text-danger"
          : "text-text";

  return (
    <button
      onClick={onClick}
      className={
        "frame p-3 text-left tabular transition-colors " +
        (active
          ? "border-accent/60 bg-accent/5"
          : "hover:border-accent/30 hover:bg-accent/5")
      }
    >
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-text-faint text-[10px] tabular w-5 shrink-0">
            #{c.rank.toString().padStart(2, "0")}
          </span>
          <span className="text-accent font-semibold truncate">
            {c.symbol}
          </span>
        </div>
        <span
          className={`text-[10px] uppercase tracking-widest border px-1.5 py-0.5 shrink-0 ${reasonStyle[c.reason]}`}
        >
          {reasonLabel[c.reason]}
        </span>
      </div>
      <div className={`text-lg font-semibold mt-1 ${toneClass}`}>
        {q ? `$${q.current.toFixed(2)}` : "—"}
      </div>
      <div className={`text-xs ${toneClass}`}>
        {q
          ? `${pct! >= 0 ? "+" : ""}${pct!.toFixed(2)}%  ${q.change >= 0 ? "+" : ""}${q.change.toFixed(2)}`
          : "no quote"}
      </div>
      {c.note && (
        <div className="text-[10px] text-text-dim mt-1.5 truncate">
          {c.note}
        </div>
      )}
    </button>
  );
}

function DiscoverySection({
  discovery,
  onPick,
}: {
  discovery: Discovery;
  onPick: (sym: string) => void;
}) {
  if (!discovery.enabled)
    return (
      <section className="frame p-3 text-xs text-text-faint">
        <span className="text-warn">!</span> discovery disabled — set
        ALPACA_API_KEY
      </section>
    );
  const empty =
    discovery.gainers.length +
      discovery.losers.length +
      discovery.most_active.length ===
    0;
  return (
    <section className="frame p-4 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-sm uppercase tracking-widest text-text-dim">
          <span className="text-accent">▸</span> discovery · top_movers
          <span className="ml-2 text-[10px] text-text-faint">
            liquidity-filtered · fed into ai prompt
          </span>
        </h2>
        {discovery.last_updated && (
          <span className="text-[10px] text-text-faint tabular">
            tape {fmtTime(discovery.last_updated)} PT
          </span>
        )}
      </div>

      {empty ? (
        <p className="text-text-faint text-xs">
          no movers right now (market closed or filters are strict)
        </p>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
          <MoverBucket
            title="gainers"
            tone="pos"
            items={discovery.gainers.slice(0, 5)}
            onPick={onPick}
          />
          <MoverBucket
            title="losers"
            tone="neg"
            items={discovery.losers.slice(0, 5)}
            onPick={onPick}
          />
          <MoverBucket
            title="most_active"
            tone="neutral"
            items={discovery.most_active.slice(0, 5)}
            onPick={onPick}
          />
        </div>
      )}
    </section>
  );
}

function MoverBucket({
  title,
  tone,
  items,
  onPick,
}: {
  title: string;
  tone: "pos" | "neg" | "neutral";
  items: Mover[];
  onPick: (sym: string) => void;
}) {
  const headColor =
    tone === "pos" ? "text-accent" : tone === "neg" ? "text-danger" : "text-text-dim";
  return (
    <div>
      <div className={`text-[10px] uppercase tracking-widest mb-1 ${headColor}`}>
        {title} [{items.length}]
      </div>
      <div className="border border-border divide-y divide-border/50 tabular text-xs">
        {items.length === 0 ? (
          <div className="px-2 py-1.5 text-text-faint">(none)</div>
        ) : (
          items.map((m) => (
            <button
              key={m.symbol}
              onClick={() => onPick(m.symbol)}
              className="w-full flex items-center justify-between gap-2 px-2 py-1.5 hover:bg-accent/5 text-left"
            >
              <span className="text-accent font-semibold w-14 shrink-0">
                {m.symbol}
              </span>
              <span className="text-text w-16 text-right shrink-0">
                {m.price !== null ? `$${m.price.toFixed(2)}` : "—"}
              </span>
              <span
                className={
                  "w-16 text-right shrink-0 " +
                  (m.percent_change === null
                    ? "text-text-faint"
                    : m.percent_change > 0
                    ? "text-accent"
                    : m.percent_change < 0
                    ? "text-danger"
                    : "text-text-dim")
                }
              >
                {m.percent_change !== null
                  ? `${m.percent_change >= 0 ? "+" : ""}${m.percent_change.toFixed(1)}%`
                  : m.volume !== null
                  ? _compact(m.volume)
                  : ""}
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

function _compact(n: number): string {
  if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(0) + "k";
  return n.toString();
}

function VerdictGlyph({ d }: { d: SymbolDecision }) {
  if (d.executed)
    return <span className="text-accent">● executed · {d.action}</span>;
  if (d.approved)
    return <span className="text-warn">● approved · {d.action}</span>;
  return (
    <span className="text-text-faint">
      ● {d.rejection_code ?? "rejected"}
    </span>
  );
}

function FocusPanel({ s }: { s: SymbolIntel }) {
  return (
    <section className="frame p-4 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-sm uppercase tracking-widest text-text-dim">
          <span className="text-accent">▸</span> focus · {s.symbol}
        </h2>
        {s.position_size_usd != null && (
          <span className="text-[10px] text-warn uppercase tracking-widest border border-warn/60 bg-warn/5 px-2 py-0.5">
            open position · ${s.position_size_usd.toFixed(2)}
            {s.position_unrealized_pnl != null && (
              <>
                {" "}
                (unreal {s.position_unrealized_pnl >= 0 ? "+" : ""}$
                {s.position_unrealized_pnl.toFixed(2)})
              </>
            )}
          </span>
        )}
      </div>

      {s.quote && <QuoteRow q={s.quote} />}

      {s.last_decision ? (
        <DecisionPanel d={s.last_decision} />
      ) : (
        <p className="text-xs text-text-faint">no decisions logged for this symbol yet</p>
      )}

      <div>
        <div className="text-[10px] uppercase tracking-widest text-text-dim mb-1">
          headlines [{s.news.length.toString().padStart(2, "0")}]
        </div>
        <NewsList items={s.news} empty={`no recent news for ${s.symbol}`} />
      </div>
    </section>
  );
}

function QuoteRow({ q }: { q: Quote }) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-xs tabular">
      <Stat label="open" value={`$${q.open.toFixed(2)}`} />
      <Stat label="prev_close" value={`$${q.prev_close.toFixed(2)}`} />
      <Stat label="day_high" value={`$${q.high.toFixed(2)}`} />
      <Stat label="day_low" value={`$${q.low.toFixed(2)}`} />
      <Stat
        label="change"
        value={`${q.change_pct >= 0 ? "+" : ""}${q.change_pct.toFixed(2)}%`}
        tone={q.change_pct >= 0 ? "pos" : "neg"}
      />
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
    tone === "pos" ? "text-accent" : tone === "neg" ? "text-danger" : "text-text";
  return (
    <div className="border border-border px-2 py-1.5 bg-bg-panel/50">
      <div className="text-[10px] uppercase tracking-widest text-text-dim">
        {label}
      </div>
      <div className={`mt-0.5 ${color}`}>{value}</div>
    </div>
  );
}

function DecisionPanel({ d }: { d: SymbolDecision }) {
  const isHold = d.action === "hold";
  const status = isHold
    ? { label: "HOLD", tone: "text-text-dim border-border bg-bg-panel/60" }
    : d.executed
    ? { label: "EXECUTED", tone: "text-accent border-accent/60 bg-accent/5" }
    : d.approved
    ? { label: "APPROVED_NOT_EXEC", tone: "text-warn border-warn/60 bg-warn/5" }
    : { label: d.rejection_code ?? "REJECTED", tone: "text-danger border-danger/60 bg-danger/5" };

  return (
    <div className="border border-border bg-bg-panel/40 p-3 text-xs space-y-2">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-widest text-text-dim">
            last_ai_verdict
          </span>
          <span className={`text-[10px] uppercase tracking-widest border px-2 py-0.5 ${status.tone}`}>
            {status.label}
          </span>
          <span className="text-text-dim uppercase">{d.action ?? "—"}</span>
        </div>
        <span className="text-text-faint tabular">
          {fmtDateTime(d.created_at)}
        </span>
      </div>
      {d.rationale ? (
        <p className="text-text-dim leading-snug">{d.rationale}</p>
      ) : (
        <p className="text-text-faint italic leading-snug">
          no rationale recorded
        </p>
      )}
    </div>
  );
}

function NewsList({ items, empty }: { items: NewsItem[]; empty: string }) {
  if (items.length === 0)
    return <p className="text-text-faint text-xs">{empty}</p>;
  return (
    <ul className="border border-border divide-y divide-border/50 text-xs">
      {items.map((n, i) => (
        <li key={`${n.url || n.headline}-${i}`} className="p-2 hover:bg-accent/5">
          <div className="flex items-start gap-2">
            <span className="text-text-faint tabular shrink-0 w-16">
              {fmtTimeHM(n.datetime)}
            </span>
            <div className="flex-1 min-w-0">
              <a
                href={n.url || "#"}
                target="_blank"
                rel="noopener noreferrer"
                className="text-text hover:text-accent break-words"
              >
                {n.headline || "(no headline)"}
              </a>
              {n.summary && (
                <p className="text-text-dim mt-0.5 line-clamp-2 leading-snug">
                  {n.summary}
                </p>
              )}
              <div className="text-[10px] text-text-faint mt-1 uppercase tracking-widest">
                {n.source}
                {n.symbol && (
                  <>
                    <span className="mx-1">·</span>
                    <span className="text-accent-dim">{n.symbol}</span>
                  </>
                )}
              </div>
            </div>
          </div>
        </li>
      ))}
    </ul>
  );
}
