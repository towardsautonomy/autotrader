"use client";

import { useEffect, useState } from "react";
import { api, AIStatus, GPUInfo } from "@/lib/api";
import { fmtTime } from "@/lib/time";

export default function AIStatusPanel() {
  const [status, setStatus] = useState<AIStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = () =>
      api
        .aiStatus()
        .then((s) => {
          setStatus(s);
          setError(null);
        })
        .catch((e) => setError(String(e)));
    load();
    const i = setInterval(load, 5_000);
    return () => clearInterval(i);
  }, []);

  if (error && !status) {
    return (
      <div className="frame p-4 text-danger text-xs">
        <span className="text-text-faint">[err]</span> ai/status: {error}
      </div>
    );
  }
  if (!status) {
    return (
      <div className="frame p-4 text-text-dim text-xs">
        <span className="blink text-accent">▊</span> querying provider...
      </div>
    );
  }

  const healthy =
    status.reachable &&
    (status.provider !== "lmstudio" ||
      status.configured_model_state === "loaded");

  return (
    <section className="frame p-4 space-y-3">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-sm uppercase tracking-widest text-text-dim flex items-center gap-2">
          <span className="text-accent">▸</span> ai_provider
          <StatusPill ok={healthy} reachable={status.reachable} />
        </h2>
        <span className="text-[10px] text-text-faint tabular">
          last_check {fmtTime(status.checked_at)} PT
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
        <Field label="provider" value={status.provider} />
        <Field
          label="configured_model"
          value={status.model_configured}
          className="col-span-1 md:col-span-2 truncate"
          title={status.model_configured}
        />
        <Field
          label="state"
          value={status.configured_model_state ?? (status.provider === "lmstudio" ? "not_found" : "n/a")}
          tone={
            status.configured_model_state === "loaded"
              ? "pos"
              : status.configured_model_state === "not-loaded" || status.configured_model_state === "not_found"
              ? "warn"
              : undefined
          }
        />
      </div>

      {status.reachable_error && (
        <div className="text-danger text-xs break-words">
          <span className="text-text-faint">[err]</span> {status.reachable_error}
        </div>
      )}

      {status.provider === "lmstudio" && status.models.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-widest text-text-dim mb-1">
            local_models [{status.models.length.toString().padStart(2, "0")}]
          </div>
          <div className="border border-border divide-y divide-border/50 tabular text-xs">
            {status.models.map((m) => (
              <div
                key={m.id}
                className={
                  "flex items-center gap-2 px-2 py-1.5 " +
                  (m.id === status.model_configured ? "bg-accent/5" : "")
                }
              >
                <span
                  className={
                    "w-2 h-2 rounded-full shrink-0 " +
                    (m.state === "loaded" ? "bg-accent" : "bg-text-faint")
                  }
                />
                <span className="truncate flex-1" title={m.id}>
                  {m.id}
                </span>
                <span className="text-text-faint text-[10px] shrink-0">
                  {m.quantization ?? "—"}
                </span>
                <span className="text-text-faint text-[10px] w-24 text-right tabular shrink-0">
                  {m.state === "loaded" && m.loaded_context_length
                    ? `ctx ${(m.loaded_context_length / 1024).toFixed(0)}k`
                    : m.max_context_length
                    ? `max ${(m.max_context_length / 1024).toFixed(0)}k`
                    : ""}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}

      {status.gpus.length > 0 && (
        <div>
          <div className="text-[10px] uppercase tracking-widest text-text-dim mb-1">
            gpu
          </div>
          <div className="space-y-1.5">
            {status.gpus.map((g) => (
              <GPURow key={g.index} gpu={g} />
            ))}
          </div>
        </div>
      )}
    </section>
  );
}

function StatusPill({ ok, reachable }: { ok: boolean; reachable: boolean }) {
  const label = !reachable ? "offline" : ok ? "healthy" : "degraded";
  const tone = !reachable
    ? "border-danger/60 text-danger bg-danger/5"
    : ok
    ? "border-accent/60 text-accent bg-accent/5"
    : "border-warn/60 text-warn bg-warn/10";
  return (
    <span
      className={
        "text-[10px] uppercase tracking-widest border px-1.5 py-0.5 " + tone
      }
    >
      {reachable ? "●" : "○"} {label}
    </span>
  );
}

function Field({
  label,
  value,
  tone,
  className = "",
  title,
}: {
  label: string;
  value: string;
  tone?: "pos" | "warn";
  className?: string;
  title?: string;
}) {
  const color =
    tone === "pos"
      ? "text-accent"
      : tone === "warn"
      ? "text-warn"
      : "text-text";
  return (
    <div
      title={title}
      className={"border border-border px-2 py-1.5 bg-bg-panel/50 " + className}
    >
      <div className="text-[10px] uppercase tracking-widest text-text-dim">
        {label}
      </div>
      <div className={`mt-0.5 text-xs tabular ${color}`}>{value}</div>
    </div>
  );
}

function GPURow({ gpu }: { gpu: GPUInfo }) {
  const pct = gpu.total_mb > 0 ? (gpu.used_mb / gpu.total_mb) * 100 : 0;
  const tone =
    pct > 90 ? "bg-danger" : pct > 75 ? "bg-warn" : "bg-accent";
  return (
    <div className="text-xs tabular">
      <div className="flex justify-between text-[10px] text-text-faint mb-0.5">
        <span className="truncate">
          gpu{gpu.index} · {gpu.name}
        </span>
        <span>
          {(gpu.used_mb / 1024).toFixed(1)} / {(gpu.total_mb / 1024).toFixed(0)} GiB
          {gpu.utilization_pct !== null ? ` · ${gpu.utilization_pct}%` : ""}
        </span>
      </div>
      <div className="h-1.5 bg-bg-raised border border-border overflow-hidden">
        <div className={tone + " h-full"} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}
