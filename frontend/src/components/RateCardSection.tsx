"use client";

import { useEffect, useState } from "react";
import { api, LlmRateCard, LlmRateEntry, LlmUsageSummary } from "@/lib/api";
import { promptDialog } from "@/components/Dialog";

const FALLBACK_KEYS = [
  "openrouter::anthropic/claude-sonnet-4.5",
  "openrouter::anthropic/claude-opus-4",
  "openrouter::anthropic/claude-haiku-4.5",
  "lmstudio::local-model",
];

export default function RateCardSection({ hours }: { hours: number }) {
  const [card, setCard] = useState<LlmRateCard | null>(null);
  const [usage, setUsage] = useState<LlmUsageSummary | null>(null);
  const [rates, setRates] = useState<Record<string, LlmRateEntry>>({});
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [msgTone, setMsgTone] = useState<"accent" | "danger">("accent");

  useEffect(() => {
    api
      .llmRateCard()
      .then((c) => {
        setCard(c);
        setRates(c.rates || {});
      })
      .catch((e) => {
        setMsg(String(e));
        setMsgTone("danger");
      });
  }, []);

  useEffect(() => {
    const load = () => api.llmUsage(hours).then(setUsage).catch(() => {});
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [hours]);

  const keys = Array.from(
    new Set([...Object.keys(rates), ...FALLBACK_KEYS]),
  );

  const setRate = (
    key: string,
    field: keyof LlmRateEntry,
    value: number,
  ) => {
    setRates((prev) => ({
      ...prev,
      [key]: {
        prompt_per_1k_usd: prev[key]?.prompt_per_1k_usd ?? 0,
        completion_per_1k_usd: prev[key]?.completion_per_1k_usd ?? 0,
        [field]: value,
      },
    }));
  };

  const addRow = async () => {
    const key = await promptDialog({
      title: "add_rate_row",
      message: "Enter the provider::model key to price.",
      placeholder: "openrouter::anthropic/claude-haiku-4.5",
    });
    if (!key || rates[key]) return;
    setRates({
      ...rates,
      [key]: { prompt_per_1k_usd: 0, completion_per_1k_usd: 0 },
    });
  };

  const removeRow = (key: string) => {
    const next = { ...rates };
    delete next[key];
    setRates(next);
  };

  const save = async () => {
    setSaving(true);
    setMsg(null);
    try {
      const updated = await api.updateLlmRateCard(rates);
      setCard(updated);
      setRates(updated.rates || {});
      setMsg("rate card committed.");
      setMsgTone("accent");
    } catch (e) {
      setMsg(String(e));
      setMsgTone("danger");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      {usage && usage.by_model.length > 0 && (
        <div className="frame p-4 space-y-3">
          <div className="flex items-baseline justify-between gap-3 flex-wrap">
            <h2 className="text-sm uppercase tracking-widest text-text-dim">
              <span className="text-accent">▸</span> spend_by_model
              <span className="text-text-faint text-[10px] ml-2">
                // last {hours}h
              </span>
            </h2>
            <div className="flex gap-5 text-xs tabular">
              <div>
                <div className="text-[10px] text-text-faint uppercase">
                  calls
                </div>
                <div className="text-accent">{usage.total_calls}</div>
              </div>
              <div>
                <div className="text-[10px] text-text-faint uppercase">
                  tokens
                </div>
                <div className="text-accent">
                  {usage.total_tokens.toLocaleString()}
                </div>
              </div>
              <div>
                <div className="text-[10px] text-text-faint uppercase">
                  cost
                </div>
                <div className="text-accent">
                  ${usage.total_cost_usd.toFixed(4)}
                </div>
              </div>
            </div>
          </div>
          <div className="overflow-x-auto -mx-4 px-4">
            <table className="w-full min-w-[480px] text-xs tabular">
              <thead>
                <tr className="text-text-faint border-b border-border">
                  <th className="text-left py-1 font-normal">
                    provider::model
                  </th>
                  <th className="text-right py-1 font-normal">calls</th>
                  <th className="text-right py-1 font-normal">tokens</th>
                  <th className="text-right py-1 font-normal">cost</th>
                </tr>
              </thead>
              <tbody>
                {usage.by_model.map((b) => (
                  <tr key={b.key} className="border-b border-border/40">
                    <td className="py-1 text-text break-all">{b.key}</td>
                    <td className="py-1 text-right">{b.calls}</td>
                    <td className="py-1 text-right">
                      {b.total_tokens.toLocaleString()}
                    </td>
                    <td className="py-1 text-right text-accent">
                      ${b.cost_usd.toFixed(4)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="frame p-4 space-y-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <h2 className="text-sm uppercase tracking-widest text-text-dim">
            <span className="text-accent">▸</span> rate_card
            {card && (
              <span className="text-text-faint ml-2 text-[10px]">
                #{card.id}
              </span>
            )}
            <span className="text-text-faint text-[10px] ml-2">
              // $/1k tokens — local models stay at 0
            </span>
          </h2>
          <button
            onClick={addRow}
            className="px-2 py-1 text-[11px] border border-border hover:border-accent/30 text-text-dim"
          >
            + add row
          </button>
        </div>

        <div className="space-y-2">
          <div className="grid grid-cols-12 gap-2 text-[10px] text-text-faint uppercase tracking-widest">
            <div className="col-span-6">provider::model</div>
            <div className="col-span-2 text-right">$/1k prompt</div>
            <div className="col-span-2 text-right">$/1k completion</div>
            <div className="col-span-2" />
          </div>
          {keys.map((k) => {
            const r = rates[k] || {
              prompt_per_1k_usd: 0,
              completion_per_1k_usd: 0,
            };
            return (
              <div
                key={k}
                className="grid grid-cols-12 gap-2 items-center text-xs"
              >
                <div className="col-span-6 text-text tabular break-all">
                  {k}
                </div>
                <input
                  type="number"
                  step="0.0001"
                  value={r.prompt_per_1k_usd}
                  onChange={(e) =>
                    setRate(k, "prompt_per_1k_usd", Number(e.target.value))
                  }
                  className="col-span-2 bg-bg-raised border border-border px-2 py-1 text-right tabular"
                />
                <input
                  type="number"
                  step="0.0001"
                  value={r.completion_per_1k_usd}
                  onChange={(e) =>
                    setRate(
                      k,
                      "completion_per_1k_usd",
                      Number(e.target.value),
                    )
                  }
                  className="col-span-2 bg-bg-raised border border-border px-2 py-1 text-right tabular"
                />
                <button
                  onClick={() => removeRow(k)}
                  className="col-span-2 text-text-faint hover:text-danger text-[11px]"
                >
                  remove
                </button>
              </div>
            );
          })}
        </div>

        <div className="flex items-center gap-4 pt-2 border-t border-border">
          <button
            onClick={save}
            disabled={saving}
            className="border border-accent/60 bg-accent/10 hover:bg-accent/20 text-accent px-4 py-1.5 text-xs font-semibold uppercase tracking-widest disabled:opacity-50"
          >
            {saving ? "committing..." : "▸ commit_rates"}
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
    </div>
  );
}
