# Troubleshooting

## Backend port in use (`address already in use`)

```bash
ss -ltnp | grep :3003              # shows pid
ls -l /proc/<pid>/cwd              # confirm which project
kill <pid>                         # or kill -9 if it ignores TERM
```

## `.env` changes not picked up

pydantic-settings caches at import. Restart the backend process fully —
`uvicorn --reload` reloads code but the settings cache survives, so for
env changes stop the process (Ctrl-C) and start again.

## `Refusing to start — secrets still contain 'replace_me'`

The startup guard trips when a required secret hasn't been filled in.
The error names the specific vars — fill them in `backend/.env` and
restart.

## LM Studio `lms load` fails with `(X) CAUSE Failed to load model`

Almost always an outdated runtime. `lms runtime update --yes` and
retry. If it still fails:

1. `lms ls` — confirm the model is actually indexed.
2. Try a smaller context (`--context-length 32768`) to rule out OOM.
3. Enable K/V cache quantization (Q8 or Q4) in the LM Studio desktop
   app for large-context loads — halves KV VRAM for a ~256 K context.
4. `lms log` — inspect the LM Studio daemon log for the real cause.

## Research chat errors with `Context size has been exceeded`

The researcher already auto-shrinks and retries — you'll usually see a
`[Context trimmed: N older messages dropped]` marker rather than a raw
error. If the raw error does escape:

1. `lms ps` — confirm `loaded_context_length` is what you expect. If
   it reloaded at a smaller size, re-run `lms load --context-length 262144`.
2. Lower `RESEARCH_TOOL_RESULT_CHARS` in `backend/.env` (try `8000`)
   and restart the backend — prevents single large payloads from
   crowding the window.
3. Start a fresh research conversation. Old ones accumulate a lot of
   tool-call history; the shrink path is a last resort, not a substitute
   for starting fresh on a new topic.

## Research chat: tool calls show `unknown tool: <company name>` or `sentence=<ticker>`

Local models occasionally hallucinate a tool name or arg name. The
researcher auto-recovers in three ways:

- **Nameless call + args** → infers tool from the args shape
  (e.g. `{"query": "Kodiak AI"}` → `search_tickers`).
- **Ticker as tool name** (`"AAPL"` with `{"days": 120}`) → routes to
  `get_price_history` with the ticker promoted to `symbol`.
- **Comparison phrase as tool name** (`"X vs Y"`) → runs
  `search_tickers` on each side and returns candidate tickers.

You'll see the recovered result, not the error. If a call still shows
as `×`, the error message includes a copy-pasteable JSON hint for the
model to use on its next round.

## Agent never emits a trade proposal on local AI

The loaded model doesn't support tool calling. Confirm with `lms ps`
that the model tag implies tools (Qwen3, Llama-3.3 Instruct, etc.) and
not a pure "chat" checkpoint.

## Frontend says "OFFLINE // backend not reachable"

Check `NEXT_PUBLIC_API_URL` matches the backend's actual port (default
`3003`). The frontend bakes the env into the bundle at build time, so
restart `npm run dev` after editing `.env.local`.

## Frontend stuck on "awaiting backend..." over LAN

Two env vars need the LAN host added:

1. `backend/.env` → `CORS_ORIGINS=...,http://<LAN-IP>:3010`
2. `frontend/.env.local` → `NEXT_DEV_ORIGINS=<LAN-IP>`

Restart both processes after editing. See [SETUP.md](SETUP.md) for the
full env layout.

## `invalid api key` from any endpoint

`NEXT_PUBLIC_API_KEY` must equal backend `JWT_SECRET` exactly. One
space or newline will break it. Rotate both together.

## Scheduler not running even though backend is up

The scheduler refuses to start if Alpaca / AI provider credentials are
still placeholders. Check backend logs for
`scheduler.skipped — fill in broker/AI credentials to enable trading`.

## `web_search` keeps failing / returns nothing

You're on the DuckDuckGo fallback, which is rate-limited and often
blocked. Get a free Tavily / Brave / Serper key (any one is enough)
and put it in `backend/.env`, then restart.
