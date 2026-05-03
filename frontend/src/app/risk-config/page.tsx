"use client";

import { useEffect, useState } from "react";
import {
  api,
  ConstraintViolation,
  GeneratedRiskConfig,
  RiskConfig,
  RiskTier,
} from "@/lib/api";

const TIERS: {
  value: RiskTier;
  label: string;
  hint: string;
}[] = [
  {
    value: "conservative",
    label: "conservative",
    hint: "stock + covered calls + cash-secured puts only. no directional options.",
  },
  {
    value: "moderate",
    label: "moderate",
    hint: "adds long calls/puts + vertical spreads (debit + credit).",
  },
  {
    value: "aggressive",
    label: "aggressive",
    hint: "adds iron condors. still defined-risk — no naked shorts at any tier.",
  },
];

const FIELDS: {
  key: keyof RiskConfig;
  label: string;
  hint?: string;
  pct?: boolean;
}[] = [
  {
    key: "budget_cap",
    label: "budget_cap",
    hint: "Max total $ the system can deploy across all positions.",
  },
  {
    key: "max_position_pct",
    label: "max_position_pct",
    pct: true,
    hint: "Largest single trade as fraction of budget (0.05 = 5%).",
  },
  {
    key: "max_concurrent_positions",
    label: "max_concurrent_positions",
    hint: "Hard cap on simultaneously open positions.",
  },
  {
    key: "max_daily_trades",
    label: "max_daily_trades",
    hint: "Reject new trades after this many in a calendar day.",
  },
  {
    key: "pdt_day_trade_count_5bd",
    label: "pdt_day_trade_count_5bd",
    hint: "FINRA Pattern Day Trader cap: same-day round trips allowed per rolling 5 business days. 3 is the sub-$25k legal limit; raise to 99 only after equity is comfortably above $25k.",
  },
  {
    key: "daily_loss_cap_pct",
    label: "daily_loss_cap_pct",
    pct: true,
    hint: "Halt for the day when realized P&L falls this far.",
  },
  {
    key: "max_drawdown_pct",
    label: "max_drawdown_pct",
    pct: true,
    hint: "Halt until manually unpaused at this cumulative loss.",
  },
  {
    key: "default_stop_loss_pct",
    label: "default_stop_loss_pct",
    pct: true,
    hint: "Applied when AI omits a stop-loss.",
  },
  {
    key: "max_stop_loss_pct",
    label: "max_stop_loss_pct",
    pct: true,
    hint: "Hard ceiling on any stop-loss — wider proposals are rejected.",
  },
  {
    key: "default_take_profit_pct",
    label: "default_take_profit_pct",
    pct: true,
    hint: "Applied when AI omits a take-profit.",
  },
  {
    key: "min_trade_size_usd",
    label: "min_trade_size_usd",
    hint: "Reject trades below this notional.",
  },
  {
    key: "paper_cost_bps",
    label: "paper_cost_bps",
    hint: "Simulated round-trip cost (bps) subtracted from paper-mode P&L. 5 = 0.05% — typical liquid-stock spread+slippage.",
  },
  {
    key: "max_option_loss_per_spread_pct",
    label: "max_option_loss_per_spread_pct",
    pct: true,
    hint: "Cap on max_loss for each defined-risk option spread (× budget_cap).",
  },
  {
    key: "earnings_blackout_days",
    label: "earnings_blackout_days",
    hint: "Reject new option opens within N days of underlying earnings.",
  },
  {
    key: "min_open_confidence",
    label: "min_open_confidence",
    hint: "Reject opens below this LLM confidence (0–1). 0.65 is the default floor; 0 disables. Losing-streak audit showed wins vs losses differ by only ~4.5pp of confidence, so anything below the floor is noise.",
  },
  {
    key: "min_reward_risk_ratio",
    label: "min_reward_risk_ratio",
    hint: "Reject opens where take_profit_pct / stop_loss_pct is below this ratio. 1.5 = winner must be at least 1.5x the loser. 0 disables.",
  },
];

const SEVERITY_ORDER: Record<ConstraintViolation["severity"], number> = {
  error: 0,
  warn: 1,
  info: 2,
};

const SEVERITY_STYLES: Record<
  ConstraintViolation["severity"],
  { box: string; title: string; tag: string; tagLabel: string }
> = {
  error: {
    box: "border-danger/60 bg-danger/10",
    title: "text-danger",
    tag: "border-danger/60 text-danger bg-danger/10",
    tagLabel: "ERROR",
  },
  warn: {
    box: "border-warn/60 bg-warn/10",
    title: "text-warn",
    tag: "border-warn/60 text-warn bg-warn/10",
    tagLabel: "WARN",
  },
  info: {
    box: "border-accent/40 bg-accent/5",
    title: "text-accent",
    tag: "border-accent/60 text-accent bg-accent/10",
    tagLabel: "INFO",
  },
};

export default function RiskConfigPage() {
  const [cfg, setCfg] = useState<RiskConfig | null>(null);
  const [blacklistRaw, setBlacklistRaw] = useState("");
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [msgTone, setMsgTone] = useState<"accent" | "danger">("accent");
  const [violations, setViolations] = useState<ConstraintViolation[]>([]);

  useEffect(() => {
    api
      .riskConfig()
      .then((c) => {
        setCfg(c);
        setBlacklistRaw(c.blacklist.join(", "));
      })
      .catch((e) => {
        setMsg(String(e));
        setMsgTone("danger");
      });
    api
      .riskConfigWarnings()
      .then((w) => setViolations(w.violations))
      .catch(() => {
        // constraint warnings are non-critical; ignore fetch errors
      });
  }, []);

  useEffect(() => {
    if (!cfg) return;
    const timer = setTimeout(() => {
      const blacklist = blacklistRaw
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);
      api
        .evaluateRiskConfig({ ...cfg, blacklist })
        .then((r) => setViolations(r.violations))
        .catch(() => {
          // keep last successful violations on transient errors
        });
    }, 300);
    return () => clearTimeout(timer);
  }, [cfg, blacklistRaw]);

  // Only surface actionable violations — info-level notes are noise when
  // nothing is actually wrong with the config.
  const sortedViolations = [...violations]
    .filter((v) => v.severity === "error" || v.severity === "warn")
    .sort((a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]);

  if (!cfg)
    return (
      <div className="text-text-dim text-sm">
        <span className="blink text-accent">▊</span> {msg || "loading config..."}
      </div>
    );

  const update = <K extends keyof RiskConfig>(k: K, v: RiskConfig[K]) => {
    setCfg({ ...cfg, [k]: v });
  };

  const applyGenerated = (g: GeneratedRiskConfig) => {
    if (!cfg) return;
    setCfg({
      ...cfg,
      budget_cap: g.budget_cap,
      max_position_pct: g.max_position_pct,
      max_concurrent_positions: g.max_concurrent_positions,
      max_daily_trades: g.max_daily_trades,
      daily_loss_cap_pct: g.daily_loss_cap_pct,
      max_drawdown_pct: g.max_drawdown_pct,
      default_stop_loss_pct: g.default_stop_loss_pct,
      default_take_profit_pct: g.default_take_profit_pct,
      max_stop_loss_pct: g.max_stop_loss_pct,
      min_trade_size_usd: g.min_trade_size_usd,
      max_option_loss_per_spread_pct: g.max_option_loss_per_spread_pct,
      earnings_blackout_days: g.earnings_blackout_days,
      paper_cost_bps: g.paper_cost_bps,
      pdt_day_trade_count_5bd: g.pdt_day_trade_count_5bd,
      risk_tier: g.risk_tier,
      blacklist: g.blacklist,
    });
    setBlacklistRaw(g.blacklist.join(", "));
    setMsg("preview loaded — review values, then commit to save.");
    setMsgTone("accent");
  };

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const blacklist = blacklistRaw
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean);
      const updated = await api.updateRiskConfig({ ...cfg, blacklist });
      setCfg(updated);
      setBlacklistRaw(updated.blacklist.join(", "));
      setMsg("configuration committed.");
      setMsgTone("accent");
    } catch (e) {
      setMsg(String(e));
      setMsgTone("danger");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold flex items-center gap-2">
          <span className="text-accent">▸</span>
          risk_config
          <span className="text-text-faint text-xs">// guardrails</span>
        </h1>
        <p className="text-xs text-text-dim mt-1 leading-relaxed">
          These limits are enforced <span className="text-accent">before</span>{" "}
          any trade reaches a broker. Changes take effect on the next decision
          cycle. Active config id: <span className="text-accent">#{cfg.id}</span>
        </p>
      </header>

      <GeneratorPanel
        initialBudget={cfg.budget_cap}
        onApply={applyGenerated}
      />

      {sortedViolations.length > 0 && (
        <div className="space-y-2">
          {sortedViolations.map((v) => {
            const s = SEVERITY_STYLES[v.severity];
            return (
              <div
                key={v.key}
                className={`border px-4 py-3 text-xs leading-relaxed ${s.box}`}
              >
                <div className="flex items-center gap-2 mb-1.5">
                  <span
                    className={`border px-1.5 py-0.5 text-[10px] uppercase tracking-widest ${s.tag}`}
                  >
                    {s.tagLabel}
                  </span>
                  <span
                    className={`text-xs uppercase tracking-widest ${s.title}`}
                  >
                    {v.title}
                  </span>
                </div>
                <p className="text-text-dim">{v.description}</p>
                <p className="text-text mt-1.5">
                  <span className="text-text-faint">remedy: </span>
                  {v.remedy}
                </p>
              </div>
            );
          })}
        </div>
      )}

      <div className="frame p-5 space-y-3">
        {FIELDS.map((f) => (
          <div key={f.key} className="grid grid-cols-1 md:grid-cols-3 gap-2 items-start py-1">
            <label className="text-xs uppercase tracking-widest text-text-dim pt-2">
              <span className="text-accent">$</span> {f.label}
            </label>
            <div className="md:col-span-2 space-y-1">
              <input
                type="number"
                step="any"
                className="w-full bg-bg-raised border border-border px-3 py-2 text-sm text-text tabular"
                value={cfg[f.key] as number}
                onChange={(e) =>
                  update(f.key, Number(e.target.value) as never)
                }
              />
              <div className="flex gap-3 text-[11px] text-text-faint">
                {f.pct && (
                  <span className="text-accent-dim tabular">
                    = {(Number(cfg[f.key]) * 100).toFixed(2)}%
                  </span>
                )}
                {f.hint && <span>{f.hint}</span>}
              </div>
            </div>
          </div>
        ))}

        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 items-start py-1 border-t border-border pt-3">
          <label className="text-xs uppercase tracking-widest text-text-dim pt-2">
            <span className="text-accent">$</span> risk_tier
          </label>
          <div className="md:col-span-2 space-y-2">
            <div className="flex flex-wrap gap-2">
              {TIERS.map((t) => (
                <button
                  key={t.value}
                  type="button"
                  onClick={() => update("risk_tier", t.value)}
                  className={
                    "px-3 py-1.5 text-xs uppercase tracking-widest border " +
                    (cfg.risk_tier === t.value
                      ? "border-accent/60 bg-accent/10 text-accent"
                      : "border-border bg-bg-panel/40 text-text-dim hover:border-accent/30")
                  }
                >
                  {t.label}
                </button>
              ))}
            </div>
            <div className="text-[11px] text-text-faint leading-snug">
              {TIERS.find((t) => t.value === cfg.risk_tier)?.hint}
            </div>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 items-start py-1 border-t border-border pt-3">
          <label className="text-xs uppercase tracking-widest text-text-dim pt-2">
            <span className="text-accent">$</span> blacklist
          </label>
          <div className="md:col-span-2 space-y-1">
            <input
              type="text"
              placeholder="TSLA, GME, PENNY..."
              className="w-full bg-bg-raised border border-border px-3 py-2 text-sm text-text"
              value={blacklistRaw}
              onChange={(e) => setBlacklistRaw(e.target.value)}
            />
            <div className="text-[11px] text-text-faint">
              Comma-separated symbols the system will never trade.
              Parsed on commit.
            </div>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-4">
        <button
          onClick={save}
          disabled={saving}
          className="border border-accent/60 bg-accent/10 hover:bg-accent/20 text-accent px-5 py-2 text-sm font-semibold uppercase tracking-widest disabled:opacity-50"
        >
          {saving ? "committing..." : "▸ commit_config"}
        </button>
        {msg && (
          <span
            className={`text-xs ${
              msgTone === "accent" ? "text-accent" : "text-danger"
            }`}
          >
            {msgTone === "accent" ? "✓ " : "! "}
            {msg}
          </span>
        )}
      </div>
    </div>
  );
}


const PREFERENCE_PRESETS: { value: string; label: string; hint: string }[] = [
  {
    value: "conservative",
    label: "conservative",
    hint: "tighter stops, smaller daily cap. prioritize capital preservation.",
  },
  {
    value: "balanced",
    label: "balanced",
    hint: "default. typical 2%/day loss cap, 5% position sizing.",
  },
  {
    value: "aggressive",
    label: "aggressive",
    hint: "wider stops, higher reward:risk, allows iron condors.",
  },
];


function GeneratorPanel({
  initialBudget,
  onApply,
}: {
  initialBudget: number;
  onApply: (g: GeneratedRiskConfig) => void;
}) {
  const [budget, setBudget] = useState(initialBudget);
  const [preference, setPreference] = useState("balanced");
  const [loading, setLoading] = useState(false);
  const [preview, setPreview] = useState<GeneratedRiskConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.generateRiskConfig(budget, preference);
      setPreview(result);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="frame p-5 space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-accent">▸</span>
        <span className="text-xs uppercase tracking-widest text-text">
          generate with AI
        </span>
        <span className="text-text-faint text-[11px]">
          // budget → full config tailored to PDT, spread, options math
        </span>
      </div>

      <div className="grid grid-cols-3 gap-2 items-end">
        <div className="space-y-1 col-span-2">
          <label className="text-[11px] uppercase tracking-widest text-text-dim">
            budget ($)
          </label>
          <input
            type="number"
            step="any"
            min="1"
            value={budget}
            onChange={(e) => setBudget(Number(e.target.value))}
            className="w-full bg-bg-raised border border-border px-3 py-2 text-sm text-text tabular"
          />
        </div>
        <button
          type="button"
          onClick={run}
          disabled={loading || budget <= 0}
          className="border border-accent/60 bg-accent/10 hover:bg-accent/20 text-accent px-4 py-2 text-xs font-semibold uppercase tracking-widest disabled:opacity-50 w-full"
        >
          {loading ? "generating..." : "▸ generate"}
        </button>
      </div>

      <div className="space-y-1">
        <label className="text-[11px] uppercase tracking-widest text-text-dim">
          preference
        </label>
        <div className="grid grid-cols-3 gap-2">
          {PREFERENCE_PRESETS.map((p) => (
            <button
              key={p.value}
              type="button"
              onClick={() => setPreference(p.value)}
              className={
                "px-3 py-2 text-[11px] uppercase tracking-widest border text-center " +
                (preference === p.value
                  ? "border-accent/60 bg-accent/10 text-accent"
                  : "border-border bg-bg-panel/40 text-text-dim hover:border-accent/30")
              }
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="text-[11px] text-text-faint leading-snug pt-1">
          {PREFERENCE_PRESETS.find((p) => p.value === preference)?.hint}
        </div>
      </div>

      {error && (
        <div className="text-xs text-danger">
          ! {error}
        </div>
      )}

      {preview && (
        <div className="border border-accent/40 bg-accent/5 p-3 space-y-2">
          <div className="text-[11px] uppercase tracking-widest text-accent">
            preview — ${preview.budget_cap.toLocaleString()} /{" "}
            {preview.risk_tier}
          </div>
          <p className="text-xs text-text leading-relaxed">
            {preview.rationale}
          </p>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-1 text-[11px] tabular">
            <Kv k="max_position" v={pct(preview.max_position_pct)} />
            <Kv k="daily_loss_cap" v={pct(preview.daily_loss_cap_pct)} />
            <Kv k="max_drawdown" v={pct(preview.max_drawdown_pct)} />
            <Kv k="default_stop" v={pct(preview.default_stop_loss_pct)} />
            <Kv k="max_stop" v={pct(preview.max_stop_loss_pct)} />
            <Kv k="default_tp" v={pct(preview.default_take_profit_pct)} />
            <Kv k="max_concurrent" v={String(preview.max_concurrent_positions)} />
            <Kv k="max_daily_trades" v={String(preview.max_daily_trades)} />
          </div>
          <button
            type="button"
            onClick={() => onApply(preview)}
            className="border border-accent/60 bg-accent/10 hover:bg-accent/20 text-accent px-3 py-1.5 text-[11px] uppercase tracking-widest"
          >
            ▸ apply to form
          </button>
          <p className="text-[10px] text-text-faint leading-snug">
            Applied values fill the form below — review, then click commit_config to save.
          </p>
        </div>
      )}
    </div>
  );
}

function Kv({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-text-faint">{k}</span>
      <span className="text-text">{v}</span>
    </div>
  );
}

function pct(n: number): string {
  return `${(n * 100).toFixed(2)}%`;
}
