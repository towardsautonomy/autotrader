"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { Analytics, Benchmark, api } from "@/lib/api";
import { fmtDate, fmtDateTime, fmtTime } from "@/lib/time";
import { useRefreshOnResume } from "@/lib/useRefreshOnResume";

const ACCENT = "#22d39b";
const DANGER = "#ff5c7a";
const WARN = "#f5b84a";
const GRID = "#1a2631";
const TEXT_DIM = "#6b7d8a";

export default function AnalyticsPage() {
  const [data, setData] = useState<Analytics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastTick, setLastTick] = useState<Date | null>(null);

  const load = useCallback(
    () =>
      api
        .analytics()
        .then((d) => {
          setData(d);
          setLastTick(new Date());
          setError(null);
        })
        .catch((e) => setError(String(e))),
    [],
  );

  useEffect(() => {
    load();
    const i = setInterval(load, 15_000);
    return () => clearInterval(i);
  }, [load]);

  useRefreshOnResume(load);

  if (error)
    return (
      <div className="frame p-4 text-danger text-sm">
        <span className="text-text-faint">[err]</span> {error}
      </div>
    );
  if (!data)
    return (
      <div className="text-text-dim text-sm">
        <span className="blink text-accent">▊</span> loading analytics...
      </div>
    );

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold flex items-center gap-2">
          <span className="text-accent">▸</span>
          analytics
          <span className="text-text-faint text-xs">{"// performance"}</span>
        </h1>
        <p className="text-xs text-text-faint mt-1">
          last sync: {lastTick ? `${fmtTime(lastTick)} PT` : "—"}
          <span className="mx-2">·</span>
          derived from closed trades + decision log
        </p>
      </header>

      <HeroKpis data={data} />

      <EquityCurve data={data} />

      <DrawdownChart data={data} />

      <div className="grid md:grid-cols-2 gap-4">
        <DailyPnlChart data={data} />
        <RollingWinRateChart data={data} />
      </div>

      <TradeLeaderboard data={data} />

      <PnlBySymbolChart data={data} />

      <HourOfDayChart data={data} />

      <AiQualityPanel data={data} />

      <div className="grid md:grid-cols-2 gap-4">
        <HoldTimeChart data={data} />
        <LlmCostVsPnlChart data={data} />
      </div>

      <DecisionsStrip data={data} />
    </div>
  );
}

function HeroKpis({ data }: { data: Analytics }) {
  const wr = data.win_rate;
  const perf = data.performance;
  const cumPnl =
    data.equity_curve.length > 0
      ? data.equity_curve[data.equity_curve.length - 1].cumulative_pnl
      : 0;

  const pfStr =
    perf.profit_factor === null || perf.profit_factor === undefined
      ? "∞"
      : perf.profit_factor.toFixed(2);
  const pfTone: "pos" | "neg" | undefined =
    perf.profit_factor === null || perf.profit_factor === undefined
      ? "pos"
      : perf.profit_factor >= 1
        ? "pos"
        : "neg";

  const streakAbs = Math.abs(perf.current_streak);
  const streakStr =
    perf.current_streak === 0
      ? "—"
      : perf.current_streak > 0
        ? `${streakAbs}W`
        : `${streakAbs}L`;
  const streakTone: "pos" | "neg" | undefined =
    perf.current_streak === 0
      ? undefined
      : perf.current_streak > 0
        ? "pos"
        : "neg";

  return (
    <section className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-3">
      <Metric
        label="CUM_PNL"
        value={`${cumPnl >= 0 ? "+" : ""}$${cumPnl.toFixed(2)}`}
        tone={cumPnl >= 0 ? "pos" : cumPnl < 0 ? "neg" : undefined}
      />
      <Metric
        label="WIN_RATE"
        value={`${wr.win_rate_pct.toFixed(1)}%`}
        sub={`${wr.wins}W / ${wr.losses}L / ${wr.breakeven}E`}
      />
      <Metric
        label="MAX_DD"
        value={`$${perf.max_drawdown_usd.toFixed(2)}`}
        sub={`${perf.max_drawdown_pct.toFixed(2)}% of budget`}
        tone={perf.max_drawdown_usd < 0 ? "neg" : undefined}
      />
      <Metric
        label="PROFIT_FACTOR"
        value={pfStr}
        sub="Σwins / |Σlosses|"
        tone={pfTone}
      />
      <Metric
        label="EXPECTANCY"
        value={`${perf.expectancy_usd >= 0 ? "+" : ""}$${perf.expectancy_usd.toFixed(2)}`}
        sub="per trade"
        tone={
          perf.expectancy_usd > 0
            ? "pos"
            : perf.expectancy_usd < 0
              ? "neg"
              : undefined
        }
      />
      <Metric
        label="AVG_WIN"
        value={`$${wr.avg_win_usd.toFixed(2)}`}
        tone="pos"
      />
      <Metric
        label="AVG_LOSS"
        value={`$${wr.avg_loss_usd.toFixed(2)}`}
        tone="neg"
      />
      <Metric
        label="STREAK"
        value={streakStr}
        sub={`max ${perf.longest_win_streak}W / ${perf.longest_loss_streak}L`}
        tone={streakTone}
      />
    </section>
  );
}

function DecisionsStrip({ data }: { data: Analytics }) {
  const ds = data.decision_stats;
  const approveRate = ds.total > 0 ? (ds.approved / ds.total) * 100 : 0;
  const executeRate = ds.approved > 0 ? (ds.executed / ds.approved) * 100 : 0;
  return (
    <section className="frame p-3 text-xs tabular text-text-dim flex flex-wrap gap-x-6 gap-y-1">
      <span>
        <span className="text-text-faint">decisions:</span>{" "}
        <span className="text-text">{ds.total}</span>
      </span>
      <span>
        <span className="text-text-faint">approved:</span>{" "}
        <span className="text-text">{ds.approved}</span>{" "}
        <span className="text-text-faint">
          ({approveRate.toFixed(0)}%)
        </span>
      </span>
      <span>
        <span className="text-text-faint">executed:</span>{" "}
        <span className="text-text">{ds.executed}</span>{" "}
        <span className="text-text-faint">
          ({executeRate.toFixed(0)}%)
        </span>
      </span>
      <span>
        <span className="text-text-faint">rejected:</span>{" "}
        <span className="text-danger">{ds.rejected}</span>
      </span>
    </section>
  );
}

const COMPARE_COLORS = ["#8aa9ff", "#f5b84a", "#c084fc", "#60e3d3", "#ff8f6b"];

function EquityCurve({ data }: { data: Analytics }) {
  const [compare, setCompare] = useState<string[]>(["SPY"]);
  const [bench, setBench] = useState<Benchmark | null>(null);
  const [benchErr, setBenchErr] = useState<string | null>(null);
  const [adding, setAdding] = useState("");

  const daily = data.equity_curve_daily;
  const budget = data.budget_cap_usd || 0;
  const hasCurve = daily.length > 0;

  // Always stretch the window to at least 14 calendar days, even if the
  // portfolio only has 1-2 days of history — otherwise SPY gets squashed
  // into a flat stub and the y-range collapses around a single point.
  const MIN_WINDOW_DAYS = 14;
  const today = useMemo(() => new Date(), []);
  const range = useMemo(() => {
    if (!hasCurve) return null;
    const first = new Date(`${daily[0].day}T00:00:00Z`);
    const last = new Date(`${daily[daily.length - 1].day}T00:00:00Z`);
    const spanMs = last.getTime() - first.getTime();
    const minSpan = MIN_WINDOW_DAYS * 24 * 3600 * 1000;
    const extra = Math.max(0, minSpan - spanMs);
    const start = new Date(first.getTime() - Math.max(extra, 3 * 24 * 3600 * 1000));
    const end = last.getTime() < today.getTime() ? today : last;
    return {
      start: start.toISOString().slice(0, 10),
      end: end.toISOString().slice(0, 10),
      startMs: start.getTime(),
      endMs: end.getTime(),
    };
  }, [daily, hasCurve, today]);

  useEffect(() => {
    if (!range || compare.length === 0) return;
    let cancelled = false;
    api
      .benchmark(compare, range.start, range.end)
      .then((b) => {
        if (!cancelled) {
          setBench(b);
          setBenchErr(null);
        }
      })
      .catch((e) => !cancelled && setBenchErr(String(e)));
    return () => {
      cancelled = true;
    };
  }, [range, compare]);

  const activeBench =
    !range || compare.length === 0
      ? null
      : bench && bench.series.every((s) => compare.includes(s.symbol))
        ? bench
        : null;

  // Hide SPY-as-dollars when we don't have a budget (no meaningful basis)
  // or when the portfolio is younger than a full trading day — the
  // benchmark line is misleading context over a 2-hour-old portfolio.
  const benchesAllowed = budget > 0 && daily.length >= 2;

  // Benchmark series as DOLLAR P&L: if you'd parked ``budget`` into this
  // ticker at baseline close, how much would you be up/down today?
  const benchmarkSeries = useMemo(() => {
    if (!activeBench || !benchesAllowed || !range) return [];
    return activeBench.series.map((s, idx) => {
      if (s.bars.length === 0) {
        return {
          symbol: s.symbol,
          color: COMPARE_COLORS[idx % COMPARE_COLORS.length],
          error: s.error,
          points: [] as { ts: number; usd: number }[],
        };
      }
      const baseClose = s.bars[0].close;
      const points = s.bars.map((b) => {
        const ts = new Date(`${b.day}T20:00:00Z`).getTime(); // ~NY close
        const ret = baseClose > 0 ? (b.close - baseClose) / baseClose : 0;
        return { ts, usd: ret * budget };
      });
      return {
        symbol: s.symbol,
        color: COMPARE_COLORS[idx % COMPARE_COLORS.length],
        error: s.error,
        points,
      };
    });
  }, [activeBench, benchesAllowed, budget, range]);

  // Build the merged chart data by unioning all ts across series. The
  // portfolio is step-forward filled (equity stays flat between closes)
  // so the line reads as "equity at this moment" rather than "equity
  // only at close events". Benchmark series are plain daily points.
  const merged = useMemo(() => {
    type Row = { ts: number; portfolio?: number } & Record<string, number | undefined>;
    const rows = new Map<number, Row>();

    for (const p of daily) {
      const ts = new Date(`${p.day}T20:00:00Z`).getTime();
      rows.set(ts, { ts, portfolio: p.cumulative_pnl });
    }
    for (const s of benchmarkSeries) {
      for (const pt of s.points) {
        const row = rows.get(pt.ts) ?? { ts: pt.ts };
        (row as Row)[s.symbol] = pt.usd;
        rows.set(pt.ts, row);
      }
    }

    // Anchor rows: start-of-window at 0 for the portfolio so the line
    // begins at zero rather than jumping in mid-chart. Also anchor
    // today so a brand-new portfolio doesn't look like a single dot.
    if (range) {
      if (!rows.has(range.startMs)) {
        rows.set(range.startMs, { ts: range.startMs, portfolio: 0 });
      }
      if (!rows.has(range.endMs) && daily.length > 0) {
        const last = daily[daily.length - 1].cumulative_pnl;
        rows.set(range.endMs, { ts: range.endMs, portfolio: last });
      }
    }

    return Array.from(rows.values()).sort((a, b) => a.ts - b.ts);
  }, [daily, benchmarkSeries, range]);

  const addTicker = (raw: string) => {
    const t = raw.trim().toUpperCase();
    if (!t || !/^[A-Z]{1,5}$/.test(t)) return;
    if (compare.includes(t)) return;
    if (compare.length >= 5) return;
    setCompare([...compare, t]);
    setAdding("");
  };
  const removeTicker = (t: string) =>
    setCompare(compare.filter((x) => x !== t));

  const yFmt = (v: number) => `${v >= 0 ? "+" : "-"}$${Math.abs(v).toFixed(0)}`;

  return (
    <section className="frame p-4">
      <div className="flex items-baseline justify-between gap-2 flex-wrap mb-1">
        <ChartHeader title="equity_curve" count={daily.length} />
        <div className="flex items-center gap-1 flex-wrap text-[10px]">
          <span className="text-text-faint mr-1">vs</span>
          {compare.map((t, idx) => (
            <span
              key={t}
              className="inline-flex items-center gap-1 border border-border px-1.5 py-0.5"
              style={{
                color: COMPARE_COLORS[idx % COMPARE_COLORS.length],
              }}
            >
              {t}
              <button
                type="button"
                onClick={() => removeTicker(t)}
                className="text-text-faint hover:text-danger"
                title="remove"
              >
                ×
              </button>
            </span>
          ))}
          {compare.length < 5 && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                addTicker(adding);
              }}
              className="inline-flex"
            >
              <input
                value={adding}
                onChange={(e) => setAdding(e.target.value.toUpperCase())}
                placeholder="+ ticker"
                maxLength={5}
                className="bg-bg-panel border border-border px-1.5 py-0.5 w-[70px] text-text placeholder:text-text-faint"
              />
            </form>
          )}
        </div>
      </div>
      <p className="text-[10px] text-text-faint mb-1">
        dollar P&L of your portfolio vs. if the same{" "}
        <span className="text-text-dim tabular">
          ${budget.toLocaleString()}
        </span>{" "}
        budget had been parked in the benchmark
        {!benchesAllowed && (
          <>
            {" "}
            <span className="text-warn">
              · benchmark hidden (need budget_cap + ≥2 days of data)
            </span>
          </>
        )}
      </p>
      {benchErr && (
        <p className="text-[10px] text-danger mb-1">
          benchmark fetch failed: {benchErr}
        </p>
      )}
      {!hasCurve ? (
        <EmptyChart message="no closed trades yet — curve will appear after first fill closes" />
      ) : (
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={merged} margin={{ top: 12, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={GRID} strokeDasharray="2 4" />
            <XAxis
              dataKey="ts"
              type="number"
              scale="time"
              domain={range ? [range.startMs, range.endMs] : ["auto", "auto"]}
              tickFormatter={(v) => fmtDate(v)}
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
            />
            <YAxis
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              tickFormatter={yFmt}
            />
            <ReferenceLine y={0} stroke={TEXT_DIM} strokeDasharray="2 2" />
            <Tooltip
              contentStyle={tooltipStyle}
              labelFormatter={(v) => fmtDate(v as number)}
              formatter={(v, k) => {
                const n = Number(v ?? 0);
                const sign = n >= 0 ? "+" : "-";
                return [`${sign}$${Math.abs(n).toFixed(2)}`, String(k ?? "")];
              }}
            />
            <Legend wrapperStyle={{ fontSize: 10, color: TEXT_DIM }} />
            <Line
              type="stepAfter"
              dataKey="portfolio"
              name="portfolio"
              stroke={ACCENT}
              strokeWidth={2}
              dot={false}
              connectNulls
              animationDuration={400}
            />
            {benchmarkSeries.map((s) => (
              <Line
                key={s.symbol}
                type="monotone"
                dataKey={s.symbol}
                name={s.symbol}
                stroke={s.color}
                strokeWidth={1.5}
                strokeDasharray="4 2"
                dot={false}
                connectNulls
                animationDuration={400}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

function DrawdownChart({ data }: { data: Analytics }) {
  const points = data.drawdown_curve.map((p) => ({
    ts: new Date(p.ts).getTime(),
    dd: p.drawdown_usd,
  }));
  const trough =
    points.length > 0 ? Math.min(...points.map((p) => p.dd)) : 0;

  return (
    <section className="frame p-4">
      <div className="flex items-baseline justify-between mb-1 flex-wrap gap-2">
        <ChartHeader title="drawdown" count={points.length} />
        <span className="text-[10px] text-text-faint tabular">
          trough:{" "}
          <span className="text-danger">${trough.toFixed(2)}</span>
        </span>
      </div>
      {points.length === 0 ? (
        <EmptyChart message="drawdown curve appears after first closed trade" />
      ) : (
        <ResponsiveContainer width="100%" height={180}>
          <ComposedChart
            data={points}
            margin={{ top: 8, right: 16, left: 0, bottom: 0 }}
          >
            <defs>
              <linearGradient id="dd-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={DANGER} stopOpacity={0} />
                <stop offset="100%" stopColor={DANGER} stopOpacity={0.55} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke={GRID} strokeDasharray="2 4" />
            <XAxis
              dataKey="ts"
              type="number"
              scale="time"
              domain={["auto", "auto"]}
              tickFormatter={(v) => fmtDate(v)}
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
            />
            <YAxis
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              tickFormatter={(v) => `$${v}`}
            />
            <ReferenceLine y={0} stroke={TEXT_DIM} />
            <Tooltip
              contentStyle={tooltipStyle}
              labelFormatter={(v) => fmtDateTime(v as number)}
              formatter={(v) => [`$${Number(v ?? 0).toFixed(2)}`, "drawdown"]}
            />
            <Line
              type="monotone"
              dataKey="dd"
              stroke={DANGER}
              strokeWidth={1.5}
              dot={false}
              fill="url(#dd-fill)"
              fillOpacity={1}
              animationDuration={400}
            />
          </ComposedChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

function TradeLeaderboard({ data }: { data: Analytics }) {
  const sorted = [...data.trade_outcomes].sort(
    (a, b) => b.realized_pnl_usd - a.realized_pnl_usd,
  );
  const best = sorted.slice(0, 5);
  const worst = sorted
    .filter((t) => t.realized_pnl_usd < 0)
    .slice(-5)
    .reverse();

  return (
    <section className="grid md:grid-cols-2 gap-4">
      <LeaderboardColumn
        title="best_trades"
        rows={best}
        accent={ACCENT}
        emptyMsg="no winning trades yet"
      />
      <LeaderboardColumn
        title="worst_trades"
        rows={worst}
        accent={DANGER}
        emptyMsg="no losing trades yet"
      />
    </section>
  );
}

function LeaderboardColumn({
  title,
  rows,
  accent,
  emptyMsg,
}: {
  title: string;
  rows: Analytics["trade_outcomes"];
  accent: string;
  emptyMsg: string;
}) {
  return (
    <div className="frame p-4">
      <ChartHeader title={title} count={rows.length} />
      {rows.length === 0 ? (
        <EmptyChart message={emptyMsg} />
      ) : (
        <table className="w-full text-xs tabular">
          <thead className="text-text-faint text-[10px] uppercase tracking-widest">
            <tr>
              <th className="text-left py-1">symbol</th>
              <th className="text-right py-1">size</th>
              <th className="text-right py-1">pnl</th>
              <th className="text-right py-1">closed</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((t) => (
              <tr key={t.id} className="border-t border-border/40">
                <td className="py-1 text-text">{t.symbol}</td>
                <td className="py-1 text-right text-text-dim">
                  ${t.size_usd.toFixed(0)}
                </td>
                <td
                  className="py-1 text-right font-semibold"
                  style={{ color: accent }}
                >
                  {t.realized_pnl_usd >= 0 ? "+" : ""}$
                  {t.realized_pnl_usd.toFixed(2)}
                </td>
                <td className="py-1 text-right text-text-faint">
                  {fmtDate(t.closed_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function DailyPnlChart({ data }: { data: Analytics }) {
  const bars = data.daily_pnl.map((p) => ({
    day: p.day,
    pnl: p.realized_pnl,
    trades: p.trade_count,
  }));

  return (
    <section className="frame p-4">
      <ChartHeader title="daily_pnl" count={bars.length} />
      {bars.length === 0 ? (
        <EmptyChart message="no daily P&L yet" />
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={bars} margin={{ top: 12, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={GRID} strokeDasharray="2 4" />
            <XAxis dataKey="day" stroke={TEXT_DIM} tick={{ fontSize: 10 }} />
            <YAxis
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              tickFormatter={(v) => `$${v}`}
            />
            <ReferenceLine y={0} stroke={TEXT_DIM} />
            <Tooltip
              contentStyle={tooltipStyle}
              formatter={(v, k) =>
                k === "pnl"
                  ? [`$${Number(v ?? 0).toFixed(2)}`, "realized_pnl"]
                  : [String(v ?? ""), String(k ?? "")]
              }
            />
            <Bar dataKey="pnl" radius={[2, 2, 0, 0]}>
              {bars.map((b) => (
                <Cell
                  key={b.day}
                  fill={b.pnl >= 0 ? ACCENT : DANGER}
                  fillOpacity={0.85}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

function RollingWinRateChart({ data }: { data: Analytics }) {
  const points = data.rolling_win_rate.map((p) => ({
    idx: p.trade_index,
    rate: p.win_rate_pct,
    window: p.window_size,
  }));

  return (
    <section className="frame p-4">
      <ChartHeader title="rolling_win_rate" count={points.length} />
      {points.length === 0 ? (
        <EmptyChart message="rolling win rate appears after the first trade closes" />
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={points} margin={{ top: 12, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={GRID} strokeDasharray="2 4" />
            <XAxis
              dataKey="idx"
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              tickFormatter={(v) => `#${v}`}
            />
            <YAxis
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              domain={[0, 100]}
              tickFormatter={(v) => `${v}%`}
            />
            <ReferenceLine y={50} stroke={TEXT_DIM} strokeDasharray="2 2" />
            <Tooltip
              contentStyle={tooltipStyle}
              labelFormatter={(v) => `trade #${v}`}
              formatter={(v, k) =>
                k === "rate"
                  ? [`${Number(v ?? 0).toFixed(1)}%`, "win_rate"]
                  : [String(v ?? ""), String(k ?? "")]
              }
            />
            <Line
              type="monotone"
              dataKey="rate"
              stroke={ACCENT}
              strokeWidth={2}
              dot={false}
              animationDuration={400}
            />
          </LineChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

function LlmCostVsPnlChart({ data }: { data: Analytics }) {
  const rows = data.llm_cost_vs_pnl.map((p) => ({
    day: p.day,
    cost: p.llm_cost_usd,
    pnl: p.realized_pnl_usd,
  }));

  return (
    <section className="frame p-4">
      <ChartHeader title="llm_cost_vs_pnl" count={rows.length} />
      {rows.length === 0 ? (
        <EmptyChart message="no LLM costs logged yet" />
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <ComposedChart data={rows} margin={{ top: 12, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={GRID} strokeDasharray="2 4" />
            <XAxis dataKey="day" stroke={TEXT_DIM} tick={{ fontSize: 10 }} />
            <YAxis
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              tickFormatter={(v) => `$${v}`}
            />
            <ReferenceLine y={0} stroke={TEXT_DIM} />
            <Tooltip
              contentStyle={tooltipStyle}
              formatter={(v, k) => [
                `$${Number(v ?? 0).toFixed(k === "cost" ? 4 : 2)}`,
                String(k ?? ""),
              ]}
            />
            <Legend
              wrapperStyle={{ fontSize: 10, color: TEXT_DIM }}
              iconType="square"
            />
            <Bar dataKey="cost" fill={WARN} fillOpacity={0.8} />
            <Line
              type="monotone"
              dataKey="pnl"
              stroke={ACCENT}
              strokeWidth={2}
              dot={false}
            />
          </ComposedChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

function HoldTimeChart({ data }: { data: Analytics }) {
  const rows = data.hold_time_distribution.map((b) => ({
    bucket: b.bucket,
    wins: b.wins,
    losses: b.losses,
  }));

  const total = rows.reduce((acc, r) => acc + r.wins + r.losses, 0);

  return (
    <section className="frame p-4">
      <ChartHeader title="hold_time_distribution" count={total} />
      {total === 0 ? (
        <EmptyChart message="no closed trades yet — hold times will populate after first fill closes" />
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <BarChart data={rows} margin={{ top: 12, right: 8, left: 0, bottom: 0 }}>
            <CartesianGrid stroke={GRID} strokeDasharray="2 4" />
            <XAxis dataKey="bucket" stroke={TEXT_DIM} tick={{ fontSize: 10 }} />
            <YAxis
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              allowDecimals={false}
            />
            <Tooltip contentStyle={tooltipStyle} />
            <Legend
              wrapperStyle={{ fontSize: 10, color: TEXT_DIM }}
              iconType="square"
            />
            <Bar dataKey="wins" stackId="h" fill={ACCENT} fillOpacity={0.85} />
            <Bar dataKey="losses" stackId="h" fill={DANGER} fillOpacity={0.65} />
          </BarChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

function PnlBySymbolChart({ data }: { data: Analytics }) {
  const rows = data.pnl_by_symbol.map((b) => ({
    symbol: b.symbol,
    pnl: b.realized_pnl_usd,
    trades: b.trade_count,
    wins: b.wins,
    losses: b.losses,
  }));
  const height = Math.max(220, rows.length * 28 + 60);

  return (
    <section className="frame p-4">
      <ChartHeader title="pnl_by_symbol" count={rows.length} />
      {rows.length === 0 ? (
        <EmptyChart message="no closed trades yet — per-symbol P&L will populate after first fill closes" />
      ) : (
        <ResponsiveContainer width="100%" height={height}>
          <BarChart
            layout="vertical"
            data={rows}
            margin={{ top: 12, right: 24, left: 16, bottom: 0 }}
          >
            <CartesianGrid stroke={GRID} strokeDasharray="2 4" />
            <XAxis
              type="number"
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              tickFormatter={(v) => `$${v}`}
            />
            <YAxis
              type="category"
              dataKey="symbol"
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              width={64}
            />
            <ReferenceLine x={0} stroke={TEXT_DIM} />
            <Tooltip
              contentStyle={tooltipStyle}
              formatter={(v, k) => {
                if (k === "pnl") return [`$${Number(v ?? 0).toFixed(2)}`, "pnl"];
                return [String(v ?? ""), String(k ?? "")];
              }}
            />
            <Bar dataKey="pnl" radius={[0, 2, 2, 0]}>
              {rows.map((r) => (
                <Cell
                  key={r.symbol}
                  fill={r.pnl >= 0 ? ACCENT : DANGER}
                  fillOpacity={0.8}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

function HourOfDayChart({ data }: { data: Analytics }) {
  // Only render market hours (9-16 ET) plus the pre/after 30min buffers,
  // hiding dead-of-night zero bars that would dominate the axis.
  const visible = data.hour_of_day_distribution.filter(
    (h) => h.hour >= 8 && h.hour <= 17,
  );
  const total = visible.reduce((acc, r) => acc + r.wins + r.losses, 0);
  const rows = visible.map((h) => ({
    hour: `${h.hour.toString().padStart(2, "0")}:00`,
    wins: h.wins,
    losses: -h.losses, // plot losses below zero for a divergent bar
    pnl: h.realized_pnl_usd,
  }));
  return (
    <section className="frame p-4">
      <div className="flex items-baseline justify-between mb-1 flex-wrap gap-2">
        <ChartHeader title="pnl_by_hour_of_day" count={total} />
        <span className="text-[10px] text-text-faint">
          NY local · wins above 0 / losses below
        </span>
      </div>
      {total === 0 ? (
        <EmptyChart message="hour-of-day breakdown appears after the first fill closes" />
      ) : (
        <ResponsiveContainer width="100%" height={240}>
          <ComposedChart
            data={rows}
            margin={{ top: 12, right: 16, left: 0, bottom: 0 }}
          >
            <CartesianGrid stroke={GRID} strokeDasharray="2 4" />
            <XAxis dataKey="hour" stroke={TEXT_DIM} tick={{ fontSize: 10 }} />
            <YAxis
              yAxisId="count"
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              allowDecimals={false}
            />
            <YAxis
              yAxisId="pnl"
              orientation="right"
              stroke={TEXT_DIM}
              tick={{ fontSize: 10 }}
              tickFormatter={(v) => `$${v}`}
            />
            <ReferenceLine yAxisId="count" y={0} stroke={TEXT_DIM} />
            <Tooltip
              contentStyle={tooltipStyle}
              formatter={(v, k) => {
                const n = Number(v ?? 0);
                if (k === "wins") return [Math.abs(n).toFixed(0), "wins"];
                if (k === "losses") return [Math.abs(n).toFixed(0), "losses"];
                if (k === "pnl")
                  return [`${n >= 0 ? "+" : "-"}$${Math.abs(n).toFixed(2)}`, "pnl"];
                return [String(v ?? ""), String(k ?? "")];
              }}
            />
            <Legend
              wrapperStyle={{ fontSize: 10, color: TEXT_DIM }}
              iconType="square"
            />
            <Bar
              yAxisId="count"
              dataKey="wins"
              fill={ACCENT}
              fillOpacity={0.85}
              radius={[2, 2, 0, 0]}
            />
            <Bar
              yAxisId="count"
              dataKey="losses"
              fill={DANGER}
              fillOpacity={0.7}
              radius={[0, 0, 2, 2]}
            />
            <Line
              yAxisId="pnl"
              type="monotone"
              dataKey="pnl"
              stroke={WARN}
              strokeWidth={1.5}
              dot={{ r: 2, fill: WARN }}
              name="pnl"
            />
          </ComposedChart>
        </ResponsiveContainer>
      )}
    </section>
  );
}

function AiQualityPanel({ data }: { data: Analytics }) {
  const q = data.ai_quality;
  const gap =
    q.avg_confidence_wins !== null && q.avg_confidence_losses !== null
      ? q.avg_confidence_wins - q.avg_confidence_losses
      : null;

  const fmtConf = (v: number | null) =>
    v === null ? "—" : (v * 100).toFixed(1) + "%";
  const fmtLatency = (v: number | null) => {
    if (v === null) return "—";
    if (v < 60) return `${v.toFixed(1)}s`;
    return `${(v / 60).toFixed(1)}m`;
  };

  const costRatioStr =
    q.cost_per_dollar_pnl === null
      ? "—"
      : `$${q.cost_per_dollar_pnl.toFixed(3)}`;
  const costRatioTone: "pos" | "neg" | undefined =
    q.cost_per_dollar_pnl === null
      ? undefined
      : q.cost_per_dollar_pnl < 0.1
        ? "pos"
        : q.cost_per_dollar_pnl > 0.5
          ? "neg"
          : undefined;

  const gapStr =
    gap === null ? "—" : `${gap >= 0 ? "+" : ""}${(gap * 100).toFixed(1)}pp`;
  const gapTone: "pos" | "neg" | undefined =
    gap === null ? undefined : gap > 0.05 ? "pos" : gap < 0 ? "neg" : undefined;

  return (
    <section className="frame p-4">
      <div className="flex items-baseline justify-between mb-3 flex-wrap gap-2">
        <h2 className="text-sm uppercase tracking-widest text-text-dim">
          <span className="text-accent">▸</span> ai_quality
        </h2>
        <span className="text-[10px] text-text-faint">
          is the AI actually good, not just profitable?
        </span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
        <Metric
          label="CONF_ON_WINS"
          value={fmtConf(q.avg_confidence_wins)}
          sub="avg declared"
        />
        <Metric
          label="CONF_ON_LOSSES"
          value={fmtConf(q.avg_confidence_losses)}
          sub="avg declared"
        />
        <Metric
          label="CONF_GAP"
          value={gapStr}
          sub="wins − losses"
          tone={gapTone}
        />
        <Metric
          label="EXEC_LATENCY"
          value={fmtLatency(q.median_exec_latency_sec)}
          sub="median decision → order"
        />
        <Metric
          label="LLM_SPEND"
          value={`$${q.total_llm_spend_usd.toFixed(2)}`}
          sub={
            q.cost_per_executed_decision_usd === null
              ? "total"
              : `$${q.cost_per_executed_decision_usd.toFixed(3)} / executed`
          }
        />
        <Metric
          label="COST_PER_$PNL"
          value={costRatioStr}
          sub="llm spend per $ of |P&L|"
          tone={costRatioTone}
        />
      </div>
      {gap !== null && gap < 0.05 && (
        <p className="text-[10px] text-warn mt-3 tabular">
          ⚠ confidence gap is thin — the model&apos;s declared confidence
          isn&apos;t strongly separating winners from losers yet.
        </p>
      )}
    </section>
  );
}

function ChartHeader({ title, count }: { title: string; count: number }) {
  return (
    <div className="flex items-center justify-between mb-3">
      <h2 className="text-sm uppercase tracking-widest text-text-dim">
        <span className="text-accent">▸</span> {title}
      </h2>
      <span className="text-xs text-text-faint tabular">
        [{count.toString().padStart(3, "0")}]
      </span>
    </div>
  );
}

function EmptyChart({ message }: { message: string }) {
  return (
    <div className="text-text-faint text-xs py-12 text-center border border-dashed border-border">
      <span className="text-text-dim">$</span> {message}
    </div>
  );
}

function Metric({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: "pos" | "neg";
}) {
  const color =
    tone === "pos" ? "text-accent" : tone === "neg" ? "text-danger" : "text-text";
  return (
    <div className="frame p-3 tabular">
      <div className="text-[10px] uppercase tracking-widest text-text-dim">
        {label}
      </div>
      <div className={`text-lg font-semibold mt-1 ${color}`}>{value}</div>
      {sub && <div className="text-[10px] text-text-faint mt-1">{sub}</div>}
    </div>
  );
}

const tooltipStyle: React.CSSProperties = {
  background: "#0a0f14",
  border: "1px solid #2a3e4f",
  fontSize: 11,
  borderRadius: 0,
  color: "#d3dde4",
};
