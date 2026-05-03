"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  activityStreamUrl,
  api,
  ActivityEvent,
  ActivityRow,
  ActivitySeverity,
} from "@/lib/api";
import { PagerControls } from "@/components/Pager";
import { fmtTime } from "@/lib/time";

const ACTIVITY_PER_PAGE = 50;
const ACTIVITY_MAX_PAGES = 10;

const SEV_COLOR: Record<ActivitySeverity, string> = {
  debug: "text-text-faint",
  info: "text-text-dim",
  warn: "text-warn",
  error: "text-danger",
  success: "text-accent",
};

const SEV_GLYPH: Record<ActivitySeverity, string> = {
  debug: "·",
  info: "›",
  warn: "!",
  error: "✗",
  success: "✓",
};

interface LogLine {
  id: string;
  ts: string;
  type: string;
  severity: ActivitySeverity;
  message: string;
  data?: Record<string, unknown> | null;
}

function rowToLine(r: ActivityRow): LogLine {
  return {
    id: `row-${r.id}`,
    ts: r.created_at,
    type: r.type,
    severity: r.severity,
    message: r.message,
    data: r.data,
  };
}

function eventToLine(e: ActivityEvent): LogLine {
  return {
    id: `sse-${e.id}-${e.ts}`,
    ts: e.ts,
    type: e.type,
    severity: e.severity,
    message: e.message,
    data: e.data,
  };
}

export default function ActivityLog({
  max = 500,
  compact = false,
}: {
  max?: number;
  compact?: boolean;
}) {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<ActivitySeverity | "all">("all");
  const [paused, setPaused] = useState(false);
  const [page, setPage] = useState(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // Backfill recent events from the REST endpoint first so the panel
  // is non-empty immediately on page load.
  useEffect(() => {
    api
      .activity(compact ? 25 : 150)
      .then((rows) => {
        setLines(rows.map(rowToLine).reverse());
      })
      .catch((e) => setError(String(e)));
  }, [compact]);

  // Live stream via SSE. Re-attaches on URL change only.
  useEffect(() => {
    const url = activityStreamUrl();
    const es = new EventSource(url);

    es.onopen = () => {
      setConnected(true);
      setError(null);
    };
    es.onerror = () => {
      setConnected(false);
    };
    es.onmessage = (ev) => {
      try {
        const data = JSON.parse(ev.data) as ActivityEvent;
        if (data.type === "stream.connected") return;
        setLines((prev) => {
          const next = [...prev, eventToLine(data)];
          return next.length > max ? next.slice(next.length - max) : next;
        });
      } catch {
        /* ignore malformed */
      }
    };

    return () => es.close();
  }, [max]);

  // Auto-scroll to bottom unless user paused.
  useEffect(() => {
    if (paused) return;
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [lines, paused]);

  const filtered = useMemo(
    () => (filter === "all" ? lines : lines.filter((l) => l.severity === filter)),
    [lines, filter]
  );

  // Paused → paginate the buffer (newest first so page 1 is most recent).
  // Live → just show everything with auto-scroll; the container is a
  // fixed-height scroll box so it won't grow unbounded.
  const showPager = paused && !compact;
  const paginated = useMemo(() => {
    if (!showPager) return filtered;
    const reversed = [...filtered].reverse();
    const cap = ACTIVITY_PER_PAGE * ACTIVITY_MAX_PAGES;
    const kept = reversed.slice(0, cap);
    const pages = Math.max(1, Math.ceil(kept.length / ACTIVITY_PER_PAGE));
    const safe = Math.min(page, pages - 1);
    const start = safe * ACTIVITY_PER_PAGE;
    return kept.slice(start, start + ACTIVITY_PER_PAGE);
  }, [showPager, filtered, page]);
  const pageMeta = useMemo(() => {
    const reversed = filtered.length;
    const cap = ACTIVITY_PER_PAGE * ACTIVITY_MAX_PAGES;
    const kept = Math.min(reversed, cap);
    const pages = Math.max(1, Math.ceil(kept / ACTIVITY_PER_PAGE));
    const safe = Math.min(page, pages - 1);
    return {
      totalPages: pages,
      safePage: safe,
      truncated: reversed > cap,
    };
  }, [filtered.length, page]);

  return (
    <div className={`frame ${compact ? "p-3" : "p-4"} space-y-2`}>
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2 text-xs">
          <span className="text-accent">▸</span>
          <span className="uppercase tracking-widest text-text-dim">
            live_activity
          </span>
          <span
            className={
              "text-[10px] uppercase tracking-widest border px-1.5 py-0.5 " +
              (connected
                ? "border-accent/60 text-accent bg-accent/5"
                : "border-danger/60 text-danger bg-danger/5")
            }
          >
            {connected ? "● stream" : "○ offline"}
          </span>
          <span className="text-text-faint tabular text-[10px]">
            [{filtered.length.toString().padStart(4, "0")}]
          </span>
        </div>

        {!compact && (
          <div className="flex items-center gap-2 text-[10px]">
            <select
              value={filter}
              onChange={(e) =>
                setFilter(e.target.value as ActivitySeverity | "all")
              }
              className="border border-border bg-bg-raised px-2 py-1 text-text"
            >
              <option value="all">all</option>
              <option value="info">info</option>
              <option value="success">success</option>
              <option value="warn">warn</option>
              <option value="error">error</option>
              <option value="debug">debug</option>
            </select>
            <button
              onClick={() => setPaused((p) => !p)}
              className={
                "border px-2 py-1 uppercase tracking-widest " +
                (paused
                  ? "border-warn/60 text-warn bg-warn/10"
                  : "border-border text-text-dim hover:text-text")
              }
            >
              {paused ? "▶ resume" : "❚❚ pause"}
            </button>
            <button
              onClick={() => setLines([])}
              className="border border-border px-2 py-1 uppercase tracking-widest text-text-dim hover:text-text"
            >
              ▬ clear
            </button>
          </div>
        )}
      </div>

      {error && (
        <div className="text-danger text-xs">
          <span className="text-text-faint">[err]</span> {error}
        </div>
      )}

      <div
        ref={scrollRef}
        onMouseEnter={() => setPaused(true)}
        onMouseLeave={() => setPaused(false)}
        className={
          "bg-bg p-2 border border-border overflow-auto font-mono text-[11px] leading-snug " +
          (compact ? "h-48" : "h-[60vh]")
        }
      >
        {filtered.length === 0 ? (
          <div className="text-text-faint">
            <span className="blink text-accent">▊</span> awaiting events...
          </div>
        ) : (
          paginated.map((l) => <LogRow key={l.id} line={l} />)
        )}
      </div>

      {showPager && pageMeta.totalPages > 1 && (
        <PagerControls
          page={pageMeta.safePage}
          totalPages={pageMeta.totalPages}
          onPage={setPage}
          truncated={pageMeta.truncated}
          maxPages={ACTIVITY_MAX_PAGES}
        />
      )}

      {!compact && (
        <div className="text-[10px] text-text-faint">
          tip: hover over the log to pause auto-scroll
          {showPager ? " · paused — page through history with the controls above" : ""}
          .
        </div>
      )}
    </div>
  );
}

function LogRow({ line }: { line: LogLine }) {
  const [open, setOpen] = useState(false);
  const hasData = line.data && Object.keys(line.data).length > 0;
  const time = fmtTime(line.ts);
  const color = SEV_COLOR[line.severity] ?? "text-text-dim";
  const glyph = SEV_GLYPH[line.severity] ?? "·";

  return (
    <div className="py-0.5 border-b border-border/20 last:border-0">
      <div
        onClick={() => hasData && setOpen((o) => !o)}
        className={
          "flex items-start gap-2 tabular " +
          (hasData ? "cursor-pointer hover:bg-accent/5" : "")
        }
      >
        <span className="text-text-faint w-16 shrink-0">{time}</span>
        <span className={`${color} w-4 shrink-0 text-center`}>{glyph}</span>
        <span className="text-accent-dim w-36 shrink-0 truncate">
          {line.type}
        </span>
        <span className={`${color} flex-1 break-words`}>{line.message}</span>
        {hasData && (
          <span className="text-text-faint shrink-0">{open ? "▾" : "▸"}</span>
        )}
      </div>
      {open && hasData && (
        <pre className="ml-[5.5rem] mt-1 text-[10px] text-text-dim bg-bg-panel border border-border/50 p-2 overflow-auto">
          {JSON.stringify(line.data, null, 2)}
        </pre>
      )}
    </div>
  );
}
