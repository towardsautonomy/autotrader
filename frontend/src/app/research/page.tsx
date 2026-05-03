"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import {
  API_CONFIG,
  ResearchConversationSummary,
  ResearchMessageRow,
  api,
  researchChatUrl,
  researchStreamUrl,
} from "@/lib/api";
import { confirmDialog } from "@/components/Dialog";
import { ToolResultCard } from "@/components/research/ToolResultCards";
import { fmtTimeHM } from "@/lib/time";

type StreamStatus =
  | { state: "idle" }
  | { state: "thinking"; round: number }
  | { state: "running_tools"; round: number; count: number }
  | { state: "finalizing"; round: number };

export default function ResearchPage() {
  const [conversations, setConversations] = useState<
    ResearchConversationSummary[]
  >([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [messages, setMessages] = useState<ResearchMessageRow[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [status, setStatus] = useState<StreamStatus>({ state: "idle" });
  const [input, setInput] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pendingIdRef = useRef(-1);
  // Per-conversation highest event seq we've processed. The backend
  // assigns monotonic seq numbers to each run's events; we dedupe on
  // seq so replayed history (from GET /stream after reconnect) doesn't
  // double-render anything the live stream already delivered.
  const lastSeqRef = useRef<Map<number, number>>(new Map());

  const refreshList = useCallback(async () => {
    try {
      const list = await api.researchConversations();
      setConversations(list);
    } catch (e) {
      setError(String(e));
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    api
      .researchConversations()
      .then((list) => {
        if (!cancelled) setConversations(list);
      })
      .catch((e) => {
        if (!cancelled) setError(String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages, status.state]);

  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  // Parse one SSE chunk (already split on the `\n\n` frame separator)
  // into (event, data-object). Returns null for comment frames.
  const parseSseChunk = useCallback(
    (raw: string): { evt: string; parsed: Record<string, unknown> } | null => {
      if (raw.startsWith(":")) return null; // keepalive / comment
      const lines = raw.split("\n");
      let evt = "message";
      let data = "";
      for (const l of lines) {
        if (l.startsWith("event:")) evt = l.slice(6).trim();
        else if (l.startsWith("data:")) data += l.slice(5).trim();
      }
      if (!data) return null;
      try {
        return { evt, parsed: JSON.parse(data) };
      } catch {
        return null;
      }
    },
    [],
  );

  // Apply a single parsed event to local state. Idempotent on `_seq`:
  // if we've already processed this event for this conversation we
  // skip it, so replay on resume doesn't double-render anything.
  const applyEvent = useCallback(
    (evt: string, parsed: Record<string, unknown>, convId: number | null) => {
      const seq = typeof parsed._seq === "number" ? (parsed._seq as number) : null;
      if (seq != null && convId != null) {
        const last = lastSeqRef.current.get(convId) ?? 0;
        if (seq <= last) return;
        lastSeqRef.current.set(convId, seq);
      }
      switch (evt) {
        case "conversation": {
          const id = parsed.id as number | undefined;
          if (id != null) {
            setActiveId((prev) => (prev == null ? id : prev));
          }
          break;
        }
        case "status": {
          const state = parsed.state as string;
          const round = (parsed.round as number) || 1;
          if (state === "thinking") {
            setStatus({ state: "thinking", round });
          } else if (state === "running_tools") {
            setStatus({
              state: "running_tools",
              round,
              count: (parsed.count as number) || 0,
            });
          } else if (state === "finalizing") {
            setStatus({ state: "finalizing", round });
          }
          break;
        }
        case "tool_call": {
          const row: ResearchMessageRow = {
            id: pendingIdRef.current--,
            role: "tool_call",
            content: "",
            tool_name: (parsed.name as string) || "tool",
            tool_payload: {
              id: parsed.id ?? null,
              arguments: parsed.arguments ?? {},
            },
            created_at: new Date().toISOString(),
          };
          setMessages((prev) => [...prev, row]);
          break;
        }
        case "tool_result": {
          const row: ResearchMessageRow = {
            id: pendingIdRef.current--,
            role: "tool_result",
            content: (parsed.preview as string) || "",
            tool_name: (parsed.name as string) || "tool",
            tool_payload: (parsed.payload as Record<string, unknown>) ?? null,
            created_at: new Date().toISOString(),
          };
          setMessages((prev) => [...prev, row]);
          break;
        }
        case "text": {
          if (parsed.partial) break;
          const content = (parsed.content as string) || "";
          const row: ResearchMessageRow = {
            id: pendingIdRef.current--,
            role: "assistant",
            content,
            tool_name: null,
            tool_payload: null,
            created_at: new Date().toISOString(),
          };
          setMessages((prev) => [...prev, row]);
          break;
        }
        case "error": {
          setError(String(parsed.error ?? "agent error"));
          break;
        }
        case "message_saved": {
          // The backend emits this for persisted user + assistant turns.
          // User turns are the interesting case here: on reconnect (GET
          // /stream), this is how we rebuild "what question did the user
          // ask?" without a separate DB roundtrip. Assistant turns also
          // arrive as "text" events which already add the row, so skip
          // them here to avoid duplicates.
          //
          // When we also optimistically appended the user row in
          // handleSend, this event would double-render it. Dedupe by
          // looking at the tail of messages: if the most recent user
          // row already matches this content, skip.
          const role = parsed.role as string | undefined;
          const content = (parsed.content as string) || "";
          if (role === "user" && content) {
            setMessages((prev) => {
              for (let i = prev.length - 1; i >= 0; i--) {
                const m = prev[i];
                if (m.role === "user") {
                  if (m.content === content) return prev;
                  break;
                }
              }
              return [
                ...prev,
                {
                  id: pendingIdRef.current--,
                  role: "user",
                  content,
                  tool_name: null,
                  tool_payload: null,
                  created_at: new Date().toISOString(),
                },
              ];
            });
          }
          break;
        }
        case "done":
        default:
          break;
      }
    },
    [],
  );

  // Drain an SSE response body, feeding frames to applyEvent. Returns
  // when the server closes the stream or the signal aborts. Throws on
  // network errors so the caller can decide whether to reconnect.
  const consumeStream = useCallback(
    async (res: Response, convIdHint: number | null) => {
      if (!res.body) throw new Error("no stream body");
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let convId = convIdHint;
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let sep = buf.indexOf("\n\n");
        while (sep >= 0) {
          const chunk = buf.slice(0, sep);
          buf = buf.slice(sep + 2);
          const frame = parseSseChunk(chunk);
          if (frame) {
            if (frame.evt === "conversation" && convId == null) {
              const id = frame.parsed.id as number | undefined;
              if (id != null) convId = id;
            }
            applyEvent(frame.evt, frame.parsed, convId);
          }
          sep = buf.indexOf("\n\n");
        }
      }
    },
    [parseSseChunk, applyEvent],
  );

  const handleSend = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming) return;
    setInput("");
    setError(null);
    setStreaming(true);
    setStatus({ state: "thinking", round: 1 });

    // Optimistically append the user message so it shows up immediately,
    // before the backend round-trip lands. The stream's message_saved
    // event would render it too, but there's a small window where the
    // socket hasn't started delivering yet — we fix that gap here and
    // rely on the post-stream DB reload to reconcile.
    const userRow: ResearchMessageRow = {
      id: pendingIdRef.current--,
      role: "user",
      content: text,
      tool_name: null,
      tool_payload: null,
      created_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, userRow]);

    abortRef.current?.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const convIdAtSend: number | null = activeId;

    try {
      const res = await fetch(researchChatUrl(), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conversation_id: convIdAtSend,
          message: text,
        }),
        signal: ctrl.signal,
      });
      if (!res.ok) throw new Error(`stream failed: ${res.status}`);
      await consumeStream(res, convIdAtSend);
    } catch (e) {
      if ((e as Error).name === "AbortError") {
        // Intentional cancel — swallow.
      } else {
        // Network drop mid-stream: the background run is still alive on
        // the server. Try to resume by tailing GET /stream from the
        // last seq we processed. If that also fails, surface the error.
        const resumed = await tryResume(activeId, ctrl.signal);
        if (!resumed) setError(String(e));
      }
    } finally {
      setStreaming(false);
      setStatus({ state: "idle" });
      if (abortRef.current === ctrl) abortRef.current = null;
      refreshList();
      if (activeId != null) {
        try {
          const fresh = await api.researchConversation(activeId);
          setMessages(fresh.messages);
        } catch {
          // keep optimistic state
        }
      }
    }

    async function tryResume(
      convId: number | null,
      signal: AbortSignal,
    ): Promise<boolean> {
      if (convId == null) return false;
      const afterSeq = lastSeqRef.current.get(convId) ?? 0;
      try {
        const res = await fetch(researchStreamUrl(convId, afterSeq), {
          signal,
        });
        if (res.status === 404) return false; // no active run; give up
        if (!res.ok) return false;
        await consumeStream(res, convId);
        return true;
      } catch (err) {
        if ((err as Error).name === "AbortError") return true;
        return false;
      }
    }
  }, [input, streaming, activeId, refreshList, consumeStream]);

  // When the user opens an existing thread (or reloads the page), try
  // to re-attach to an in-flight run first; fall back to static DB
  // messages only if no run is active. This handles:
  //   • laptop went to sleep mid-research → run kept going server-side
  //   • user reloaded the tab while research was working
  //   • user switched threads and came back before it finished
  // Streams under an active run may replay events we haven't rendered
  // yet; the seq-dedupe inside applyEvent keeps re-renders idempotent.
  useEffect(() => {
    if (activeId == null) return;
    // Don't interrupt an in-progress send or the user's own stream.
    if (abortRef.current != null) return;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    let cancelled = false;
    // Reset the seq cursor when entering a new thread so the next run's
    // events don't get suppressed by a stale high-water mark.
    if (!lastSeqRef.current.has(activeId)) {
      lastSeqRef.current.set(activeId, 0);
    }

    (async () => {
      const afterSeq = lastSeqRef.current.get(activeId) ?? 0;
      let tailed = false;
      try {
        const res = await fetch(researchStreamUrl(activeId, afterSeq), {
          signal: ctrl.signal,
        });
        if (cancelled) return;
        if (res.ok) {
          tailed = true;
          // Fresh replay covers everything from seq > afterSeq. If we're
          // entering the thread cold (no prior render), start from an
          // empty slate so the replay builds the canonical view.
          if (afterSeq === 0) setMessages([]);
          setStreaming(true);
          setStatus({ state: "thinking", round: 1 });
          await consumeStream(res, activeId);
        }
      } catch (err) {
        if ((err as Error).name === "AbortError") return;
        // Fall through to DB load below.
      } finally {
        // Only clear our streaming UI if we still own the slot. If
        // handleSend stole it, it has already set streaming=true for
        // its own run and we must not flip it back off.
        if (!cancelled && tailed && abortRef.current === ctrl) {
          setStreaming(false);
          setStatus({ state: "idle" });
        }
      }

      if (cancelled) return;
      // If handleSend (or a thread switch) stole the slot while we were
      // streaming, skip the DB reload — the new owner will do its own,
      // and racing it here can overwrite partial stream state.
      if (abortRef.current !== ctrl) return;
      // Always refresh from DB after tailing (or as the fallback): the
      // DB is the canonical record, and stream replay uses in-memory
      // history that can be trimmed under load.
      try {
        const fresh = await api.researchConversation(activeId);
        if (!cancelled && abortRef.current === ctrl) {
          setMessages(fresh.messages);
        }
      } catch (err) {
        if (!cancelled && abortRef.current === ctrl) setError(String(err));
      } finally {
        if (abortRef.current === ctrl) abortRef.current = null;
      }
    })();

    return () => {
      cancelled = true;
      ctrl.abort();
      if (abortRef.current === ctrl) abortRef.current = null;
    };
  }, [activeId, consumeStream]);

  const handleNewChat = useCallback(() => {
    setActiveId(null);
    setMessages([]);
    setError(null);
    setStatus({ state: "idle" });
    setSidebarOpen(false);
  }, []);

  const handleDelete = useCallback(
    async (id: number) => {
      const ok = await confirmDialog({
        title: "delete thread",
        message: "This will permanently delete the conversation and all its messages.",
        confirmLabel: "delete",
        tone: "danger",
      });
      if (!ok) return;
      try {
        await api.deleteResearchConversation(id);
        if (activeId === id) handleNewChat();
        await refreshList();
      } catch (e) {
        setError(String(e));
      }
    },
    [activeId, handleNewChat, refreshList],
  );

  const keyMissing = !API_CONFIG.key;

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between flex-wrap gap-2">
        <h1 className="text-sm uppercase tracking-widest text-text-dim flex items-center gap-2">
          <span className="text-accent">▸</span>
          researcher
          <span className="text-text-faint normal-case tracking-normal text-xs">
            {"// ask anything, watch it dig"}
          </span>
        </h1>
        <button
          type="button"
          onClick={() => setSidebarOpen((v) => !v)}
          className="md:hidden border border-border text-text-dim hover:text-accent hover:border-accent/40 px-3 py-1.5 uppercase tracking-widest text-[11px]"
        >
          {sidebarOpen ? "× threads" : "≡ threads"}
        </button>
      </header>

      {keyMissing && (
        <div className="frame p-3 text-xs text-warn">
          <span className="text-text-faint">[warn]</span> NEXT_PUBLIC_API_KEY
          not set — chat endpoint will reject requests.
        </div>
      )}

      {error && (
        <div className="frame p-3 text-xs text-danger flex items-start justify-between gap-3">
          <div>
            <span className="text-text-faint">[err]</span> {error}
          </div>
          <button
            type="button"
            onClick={() => setError(null)}
            className="text-text-dim hover:text-text"
          >
            ×
          </button>
        </div>
      )}

      <div className="grid md:grid-cols-[260px_1fr] gap-4">
        <aside
          className={
            "space-y-2 " + (sidebarOpen ? "block" : "hidden md:block")
          }
        >
          <button
            type="button"
            onClick={handleNewChat}
            className="w-full border border-accent/40 text-accent hover:bg-accent/10 px-3 py-2 text-sm uppercase tracking-widest"
          >
            + new chat
          </button>
          <div className="frame max-h-[60vh] md:max-h-[calc(100vh-220px)] overflow-y-auto">
            {conversations.length === 0 ? (
              <p className="p-3 text-text-faint text-xs">
                no threads yet. ask a question below.
              </p>
            ) : (
              <ul>
                {conversations.map((c) => (
                  <li
                    key={c.id}
                    className={
                      "border-b border-border last:border-b-0 " +
                      (activeId === c.id ? "bg-accent/5" : "")
                    }
                  >
                    <div className="flex items-start">
                      <button
                        type="button"
                        onClick={() => {
                          setActiveId(c.id);
                          setSidebarOpen(false);
                        }}
                        className="flex-1 text-left px-3 py-2 min-w-0"
                      >
                        <div
                          className={
                            "text-sm truncate " +
                            (activeId === c.id
                              ? "text-accent"
                              : "text-text")
                          }
                        >
                          {activeId === c.id && (
                            <span className="mr-1">●</span>
                          )}
                          {c.title || `thread #${c.id}`}
                        </div>
                        <div className="text-[10px] text-text-faint mt-0.5 tabular">
                          {c.message_count} msg · {fmtTimeHM(c.created_at)}
                        </div>
                      </button>
                      <button
                        type="button"
                        onClick={() => handleDelete(c.id)}
                        className="text-text-faint hover:text-danger px-2 py-2 text-xs"
                        aria-label={`delete thread ${c.id}`}
                      >
                        ×
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </aside>

        <section className="frame flex flex-col min-h-[60vh] md:min-h-[calc(100vh-220px)] min-w-0">
          <div
            ref={scrollRef}
            className="flex-1 overflow-y-auto p-4 space-y-4 min-w-0"
          >
            {messages.length === 0 && !streaming ? (
              <EmptyState onPick={(q) => setInput(q)} />
            ) : (
              renderMessages(messages)
            )}
            {streaming && <StatusBlock status={status} />}
          </div>

          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleSend();
            }}
            className="border-t border-border p-3 flex gap-2"
          >
            <textarea
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                }
              }}
              rows={2}
              placeholder={
                streaming
                  ? "agent is working..."
                  : "ask about a ticker, a setup, or a recent headline..."
              }
              disabled={streaming}
              className="flex-1 bg-bg-raised border border-border text-text text-sm px-3 py-2 resize-none font-mono disabled:opacity-60"
            />
            <button
              type="submit"
              disabled={streaming || !input.trim()}
              className="border border-accent/40 text-accent hover:bg-accent/10 disabled:opacity-40 disabled:hover:bg-transparent px-4 py-2 text-sm uppercase tracking-widest self-stretch"
            >
              {streaming ? "…" : "send"}
            </button>
          </form>
        </section>
      </div>
    </div>
  );
}

function EmptyState({ onPick }: { onPick: (q: string) => void }) {
  const starters = [
    "What's the bull case for NVDA this quarter given current valuations?",
    "Pull the latest news on TSLA and flag anything tradable.",
    "Compare AAPL vs MSFT momentum — which has the better setup right now?",
    "Any macro prints this week I should be aware of for SPY?",
  ];
  return (
    <div className="space-y-3">
      <p className="text-text-dim text-sm">
        <span className="blink text-accent">▊</span> ready. ask a question or
        pick a starter below.
      </p>
      <div className="grid sm:grid-cols-2 gap-2">
        {starters.map((s) => (
          <button
            key={s}
            type="button"
            onClick={() => onPick(s)}
            className="frame p-3 text-left text-xs text-text-dim hover:text-text hover:border-accent/40 transition-colors"
          >
            <span className="text-accent mr-1">▸</span>
            {s}
          </button>
        ))}
      </div>
    </div>
  );
}

function StatusBlock({ status }: { status: StreamStatus }) {
  if (status.state === "idle") return null;
  const label =
    status.state === "thinking"
      ? `thinking`
      : status.state === "finalizing"
        ? `tool budget reached · forcing final answer`
        : `running ${status.count} tool${status.count === 1 ? "" : "s"}`;
  return (
    <div className="text-xs text-text-dim flex items-center gap-2">
      <span className="blink text-accent">▊</span>
      <span>{label}</span>
    </div>
  );
}

function renderMessages(messages: ResearchMessageRow[]) {
  const out: React.ReactNode[] = [];
  let toolBuffer: ToolItem[] = [];

  const pushTools = (keyBase: string | number) => {
    if (!toolBuffer.length) return;
    out.push(<ToolGroup key={`tg-${keyBase}`} items={toolBuffer.slice()} />);
    toolBuffer = [];
  };

  for (const m of messages) {
    if (m.role === "tool_call") {
      const args =
        ((m.tool_payload?.arguments as Record<string, unknown>) ?? {}) || {};
      toolBuffer.push({
        key: m.id,
        name: m.tool_name ?? "tool",
        args,
        result: null,
        payload: null,
      });
    } else if (m.role === "tool_result") {
      const target = toolBuffer
        .slice()
        .reverse()
        .find(
          (t) =>
            t.result === null &&
            t.name === (m.tool_name ?? t.name),
        );
      if (target) {
        target.result = m.content || "(empty)";
        target.payload = m.tool_payload;
      } else {
        toolBuffer.push({
          key: m.id,
          name: m.tool_name ?? "tool",
          args: {},
          result: m.content || "(empty)",
          payload: m.tool_payload,
        });
      }
    } else if (m.role === "assistant") {
      // Order within an assistant turn: charts / tables / ownership /
      // insider activity go on TOP (the visual payoff), then the written
      // report, then the collapsed tool trace at the bottom.
      const tools = toolBuffer.slice();
      toolBuffer = [];
      if (tools.length > 0) {
        out.push(<FindingsPanel key={`fp-${m.id}`} items={tools} />);
      }
      out.push(<UserOrAssistantBlock key={m.id} m={m} />);
      if (tools.length > 0) {
        out.push(<ToolGroup key={`tg-${m.id}`} items={tools} />);
      }
    } else {
      // New user turn — any un-terminated tool trace is from the previous
      // turn (e.g., streaming didn't yield assistant text). Render inline
      // before moving on.
      pushTools(`orphan-${m.id}`);
      out.push(<UserOrAssistantBlock key={m.id} m={m} />);
    }
  }
  // Trailing tools: streaming in progress with no final text yet.
  pushTools("live");
  return out;
}

function UserOrAssistantBlock({ m }: { m: ResearchMessageRow }) {
  if (m.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] border border-accent/40 bg-accent/5 text-text text-sm px-3 py-2 whitespace-pre-wrap">
          {m.content}
        </div>
      </div>
    );
  }
  return (
    <div className="text-sm leading-relaxed markdown-body">
      <MarkdownContent content={m.content} />
    </div>
  );
}

interface ToolItem {
  key: number;
  name: string;
  args: Record<string, unknown>;
  result: string | null;
  payload: Record<string, unknown> | null;
}

function ToolGroup({ items }: { items: ToolItem[] }) {
  const [open, setOpen] = useState(false);
  if (items.length === 0) return null;
  // Collapsed view surfaces only the CURRENT step — the in-flight call if
  // any, otherwise the most recent one. The line updates in place as new
  // tool calls stream in, so the chat doesn't scroll itself into oblivion
  // during a long research loop. The caret expands to the full trail.
  const latest = items.find((it) => it.result === null) ?? items[items.length - 1];
  const total = items.length;
  const stepNum = items.indexOf(latest) + 1;
  return (
    <div className="text-[11px] text-text-faint">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="hover:text-text-dim flex items-baseline gap-2 w-full text-left min-w-0 tabular"
        aria-expanded={open}
        title={open ? "collapse tool trail" : "show full tool trail"}
      >
        <span className="text-accent-dim shrink-0">{open ? "▾" : "▸"}</span>
        {open ? (
          <span className="text-text-dim shrink-0">
            {total} tool call{total === 1 ? "" : "s"}
          </span>
        ) : (
          <>
            <span className="text-text-faint shrink-0">
              step {stepNum}/{total}
            </span>
            <ToolStatusLine item={latest} />
          </>
        )}
      </button>
      {open && (
        <div className="mt-1 space-y-1 pl-3 border-l border-border">
          {items.map((it) => (
            <ToolResultCard
              key={it.key}
              name={it.name}
              args={it.args}
              payload={it.payload}
              fallback={it.result ?? "(running…)"}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// Inline status line — rendered as a sequence of `<span>`s (no block
// elements) so it can live inside a `<button>` without breaking HTML
// semantics or React hydration.
function ToolStatusLine({ item }: { item: ToolItem }) {
  const err =
    (item.payload &&
      typeof item.payload.error === "string" &&
      (item.payload.error as string)) ||
    null;
  const running = item.result === null;
  const icon = running ? "◌" : err ? "×" : "✓";
  const iconColor = running
    ? "text-text-faint"
    : err
      ? "text-warn"
      : "text-accent-dim";
  const argSummary = summarizeArgs(item.args);
  return (
    <>
      <span className={`${iconColor} shrink-0`}>{icon}</span>
      <span className="text-text-dim shrink-0">{item.name}</span>
      <span className="text-text-faint min-w-0 flex-1 truncate">
        {argSummary}
        {err ? ` — ${err}` : ""}
      </span>
    </>
  );
}

// Tools whose payloads carry visual value — we surface them into the
// findings panel after the assistant answer. Others (web_search,
// fetch_url, get_recent_*) stay inside the collapsed tool group.
//
// Order below is the render order in the findings panel: chart first,
// then the numbers that move the needle, then context.
const FINDINGS_ORDER: readonly string[] = [
  "get_price_history",
  "get_intraday_history",
  "get_technicals",
  "get_basic_financials",
  "get_ownership",
  "get_insider_transactions",
  "get_analyst_ratings",
  "get_earnings",
  "read_filing",
  "get_sec_filings",
  "get_company_news",
  "get_peers",
  "get_market_context",
  "get_company_profile",
  "deep_dive",
];
const FINDINGS_TOOLS: ReadonlySet<string> = new Set(FINDINGS_ORDER);
// Tools we surface even if the payload is empty / returned an error,
// because their absence is itself informative ("no insider activity",
// "no major institutional holders").
const ALWAYS_SURFACE: ReadonlySet<string> = new Set([
  "get_insider_transactions",
  "get_ownership",
  "get_analyst_ratings",
  // Surface profile even on failure so a wrong-ticker lookup doesn't
  // silently disappear when the user is comparing multiple companies.
  "get_company_profile",
]);

// Tool names where the same tool called for multiple symbols produces
// genuinely different results we want to surface side-by-side (e.g.
// comparing AAPL vs MSFT charts). Keyed by symbol so every company gets
// its own card.
const PER_SYMBOL_TOOLS: ReadonlySet<string> = new Set([
  "get_price_history",
  "get_intraday_history",
  "get_technicals",
  "get_basic_financials",
  "get_ownership",
  "get_insider_transactions",
  "get_analyst_ratings",
  "get_earnings",
  "get_company_news",
  "get_company_profile",
]);

function argSymbol(args: Record<string, unknown>): string {
  const v = args.symbol ?? args.ticker ?? args.symbols;
  return typeof v === "string" ? v.toUpperCase() : "";
}

function FindingsPanel({ items }: { items: ToolItem[] }) {
  // Dedupe by (tool name + symbol) when the tool is per-symbol — keeps
  // every company's chart/financials when comparing. Otherwise dedupe
  // by name so repeat calls collapse to the most recent.
  const byKey = new Map<string, ToolItem>();
  for (const it of items) {
    if (!FINDINGS_TOOLS.has(it.name)) continue;
    if (!it.payload && !ALWAYS_SURFACE.has(it.name)) continue;
    if (
      it.payload &&
      typeof it.payload.error === "string" &&
      !ALWAYS_SURFACE.has(it.name)
    ) {
      continue;
    }
    const sym = PER_SYMBOL_TOOLS.has(it.name) ? argSymbol(it.args) : "";
    const k = sym ? `${it.name}::${sym}` : it.name;
    byKey.set(k, it);
  }
  if (byKey.size === 0) return null;
  // Group by tool name so per-symbol matches can lay out side-by-side
  // instead of stacking vertically. E.g. AAPL vs MSFT price charts land
  // in the same row; non-per-symbol tools stay as single-column blocks.
  const groups: { name: string; items: ToolItem[] }[] = [];
  for (const n of FINDINGS_ORDER) {
    const matches = [...byKey.entries()]
      .filter(([k]) => k === n || k.startsWith(`${n}::`))
      .map(([, v]) => v);
    if (matches.length === 0) continue;
    matches.sort((a, b) => argSymbol(a.args).localeCompare(argSymbol(b.args)));
    groups.push({ name: n, items: matches });
  }
  return (
    <div className="mt-1 space-y-3">
      <div className="text-[10px] uppercase tracking-widest text-text-faint border-b border-border pb-1">
        research findings
      </div>
      {groups.map((g) => {
        const label = g.name.replace(/^get_/, "").replace(/_/g, " ");
        const multi = g.items.length > 1 && PER_SYMBOL_TOOLS.has(g.name);
        return (
          <div key={g.name}>
            <div className="text-[10px] uppercase tracking-widest text-accent-dim mb-1">
              {label}
              {multi ? (
                <span className="text-text-faint ml-2">
                  ({g.items.length}-up)
                </span>
              ) : null}
            </div>
            <div
              className={
                multi
                  ? g.items.length >= 3
                    ? "grid gap-3 grid-cols-1 md:grid-cols-2 xl:grid-cols-3"
                    : "grid gap-3 grid-cols-1 md:grid-cols-2"
                  : ""
              }
            >
              {g.items.map((it) => {
                const sym = PER_SYMBOL_TOOLS.has(it.name)
                  ? argSymbol(it.args)
                  : "";
                return (
                  <div key={it.key} className={multi ? "min-w-0" : ""}>
                    {multi && sym ? (
                      <div className="text-[10px] uppercase tracking-widest text-accent mb-1">
                        {sym}
                      </div>
                    ) : null}
                    <ToolResultCard
                      name={it.name}
                      args={it.args}
                      payload={it.payload}
                      fallback={it.result ?? ""}
                    />
                  </div>
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function summarizeArgs(args: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(args)) {
    if (v == null || v === "") continue;
    let s = typeof v === "string" ? v : String(v);
    if (/^https?:\/\//i.test(s)) {
      try {
        const u = new URL(s);
        const tail = u.pathname.split("/").filter(Boolean).slice(-1)[0] || "";
        s = `${u.hostname}${tail ? "/…/" + tail : ""}`;
      } catch {
        s = s.slice(0, 30) + "…";
      }
    } else if (s.length > 30) {
      s = s.slice(0, 30) + "…";
    }
    parts.push(`${k}=${s}`);
    if (parts.length >= 3) break;
  }
  return parts.join(" · ");
}

// Local models sometimes emit bullets inline — `"...profitability. * Summary:
// Compare the two. * Price & Technicals: * KDK: ..."` — which ReactMarkdown
// renders as one paragraph with literal `*` text. This pre-pass re-breaks
// likely inline bullets onto their own lines so GFM can parse them as lists.
// Only triggers on single `*`/`-` with surrounding whitespace (never inside
// `**bold**` or hyphenated words), and requires a sentence-ending punctuation
// before the break to avoid touching normal prose.
function normalizeMarkdown(src: string): string {
  let out = src;
  out = out.replace(/([.!?:;)])[ \t]+\*[ \t]+(?=\S)/g, "$1\n\n* ");
  out = out.replace(/([.!?:;)])[ \t]+-[ \t]+(?=[A-Z])/g, "$1\n\n- ");
  out = out.replace(/([.!?:;)])[ \t]+(#{1,6}[ \t])/g, "$1\n\n$2");
  return out;
}

// Let a minimal set of inline HTML tags pass through ReactMarkdown.
// Motivation: GFM tables have no markdown syntax for a line break inside
// a cell — the convention is `<br>`. React-markdown escapes unknown HTML
// by default, so `<br>` was rendering as literal text and cells with
// multi-line content were breaking the table layout.
// Security: LLM output can contain attacker-supplied content echoed from
// fetched pages, so we whitelist only presentational tags and rely on
// rehype-sanitize's default allow-list for attributes.
const _markdownSanitizeSchema = {
  ...defaultSchema,
  tagNames: [
    ...(defaultSchema.tagNames ?? []),
    "br",
    "sub",
    "sup",
    "mark",
    "u",
  ],
};

function MarkdownContent({ content }: { content: string }) {
  const components = useMemo(
    () => ({
      a: (props: React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
        <a
          {...props}
          target="_blank"
          rel="noreferrer"
          className="text-accent underline decoration-accent/40 hover:decoration-accent break-all"
        />
      ),
      code: (props: React.HTMLAttributes<HTMLElement>) => (
        <code
          {...props}
          className="bg-bg-raised border border-border px-1 py-0.5 text-[12px]"
        />
      ),
      pre: (props: React.HTMLAttributes<HTMLPreElement>) => (
        <pre
          {...props}
          className="bg-bg-raised border border-border p-2 overflow-x-auto text-[12px]"
        />
      ),
      table: (props: React.TableHTMLAttributes<HTMLTableElement>) => (
        <div className="overflow-x-auto my-2">
          <table
            {...props}
            className="min-w-full text-xs border border-border"
          />
        </div>
      ),
      th: (props: React.ThHTMLAttributes<HTMLTableCellElement>) => (
        <th
          {...props}
          className="border border-border px-2 py-1 text-left bg-bg-raised text-text-dim uppercase tracking-widest text-[10px]"
        />
      ),
      td: (props: React.TdHTMLAttributes<HTMLTableCellElement>) => (
        <td
          {...props}
          className="border border-border px-2 py-1 align-top break-words [overflow-wrap:anywhere]"
        />
      ),
      ul: (props: React.HTMLAttributes<HTMLUListElement>) => (
        <ul {...props} className="list-disc ml-5 my-1 space-y-0.5" />
      ),
      ol: (props: React.OlHTMLAttributes<HTMLOListElement>) => (
        <ol {...props} className="list-decimal ml-5 my-1 space-y-0.5" />
      ),
      h1: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
        <h1 {...props} className="text-base font-semibold text-accent mt-3 mb-1" />
      ),
      h2: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
        <h2 {...props} className="text-sm font-semibold text-accent mt-3 mb-1" />
      ),
      h3: (props: React.HTMLAttributes<HTMLHeadingElement>) => (
        <h3 {...props} className="text-sm font-semibold text-text mt-2 mb-1" />
      ),
      p: (props: React.HTMLAttributes<HTMLParagraphElement>) => (
        <p {...props} className="my-1.5" />
      ),
      blockquote: (props: React.BlockquoteHTMLAttributes<HTMLQuoteElement>) => (
        <blockquote
          {...props}
          className="border-l-2 border-accent/40 pl-3 text-text-dim italic my-2"
        />
      ),
    }),
    [],
  );
  const normalized = useMemo(() => normalizeMarkdown(content), [content]);
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeRaw, [rehypeSanitize, _markdownSanitizeSchema]]}
      components={components}
    >
      {normalized}
    </ReactMarkdown>
  );
}
