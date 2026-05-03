# Configuration

Every knob below lives in `backend/.env` (or as a Settings default in
`app/config.py` if you don't override). Defaults are tuned for a single
dev on one machine in paper mode; adjust when scaling up.

The frontend has a much smaller env surface — see [SETUP.md](SETUP.md).

---

## Ports cheatsheet

| Service       | Default URL                | Config                                  |
|---------------|----------------------------|-----------------------------------------|
| Backend API   | `http://127.0.0.1:3003`    | `APP_HOST`, `APP_PORT` in `backend/.env`|
| Frontend dev  | `http://127.0.0.1:3010`    | `next dev -p 3010` (package.json)       |
| LM Studio API | `http://127.0.0.1:1234`    | `LMSTUDIO_BASE_URL` in `backend/.env`   |

---

## Trading cadence

| Env var                            | Default | What it controls                                                |
|------------------------------------|---------|-----------------------------------------------------------------|
| `STOCK_DECISION_INTERVAL_MIN`      | 5       | Minutes between decision-loop ticks                             |
| `POLYMARKET_DECISION_INTERVAL_MIN` | 15      | Minutes between Polymarket decisions (not yet wired end-to-end) |
| `RUNTIME_MONITOR_INTERVAL_SEC`     | 30      | Seconds between stop-loss / EOD / DTE checks                    |
| `SCOUT_INTERVAL_MIN`               | 2       | Minutes between scout scans                                     |
| `POSITION_REVIEW_INTERVAL_SEC`     | 90      | Seconds between LLM position reviews                            |

The bracket reconciler (every 45 s), pending-order reconciler
(every 30 s), and post-mortem loop (every 120 s) also run on the same
scheduler. Their intervals are fixed in `SchedulerRunner` — tune in
`app/scheduler/runner.py` if you need to change them.

---

## Agent toggles

| Env var                  | Default | What it does                                                             |
|--------------------------|---------|--------------------------------------------------------------------------|
| `SCOUT_ENABLED`          | true    | Gates the scout loop                                                     |
| `SCOUT_LLM_ENABLED`      | false   | Adds a small LLM refinement step to the scout (~1 tiny call/tick)        |
| `POSITION_REVIEW_ENABLED`| true    | Gates the position-review loop                                           |
| `POST_MORTEM_ENABLED`    | true    | LLM writes a structured lesson for every closed trade                    |
| `MACRO_REGIME_ENABLED`   | true    | One LLM call per session open, caches a risk-on / off label              |
| `RESEARCH_ENABLED`       | true    | Lets the decision agent call web_search / fetch_url before proposing     |
| `MULTI_AGENT_ENABLED`    | false   | Fan out per-symbol research agents in parallel before the decision agent |
| `RESPECT_MARKET_HOURS`   | true    | When on, decision + scout skip ticks while market is closed              |

---

## Budget / safety

| Env var                              | Default | What it does                                                              |
|--------------------------------------|---------|---------------------------------------------------------------------------|
| `DAILY_LLM_BUDGET_USD`               | 0.0     | When >0, agents skip ticks once today's LLM spend crosses this            |
| `CIRCUIT_BREAKER_CONSECUTIVE_LOSSES` | 3       | Auto-pause agents after N consecutive losing closes in a day (0 disables) |
| `OPTION_DTE_WATCHDOG_DAYS`           | 1       | Auto-close option positions when nearest leg DTE ≤ this (0 disables)      |

---

## Screener / scout

| Env var                    | Default | What it does                                  |
|----------------------------|---------|-----------------------------------------------|
| `SCREENER_TOP_K`           | 10      | Top results returned per screener run         |
| `SCREENER_MIN_PRICE`       | 5.0     | Drop sub-$5 names from the screener universe  |
| `SCREENER_MIN_PREV_VOLUME` | 500 000 | Drop illiquid names                           |
| `SCOUT_QUEUE_MAX_SIZE`     | 50      | Max candidates the scout queue holds at once  |
| `SCOUT_QUEUE_TTL_SEC`      | 600     | Candidates drop off the queue after this      |
| `SCOUT_PER_BUCKET`         | 5       | Per-bucket (gainers / losers / active) pick size |

---

## Research belt

| Env var                            | Default | What it does                                                                                                                |
|------------------------------------|---------|-----------------------------------------------------------------------------------------------------------------------------|
| `RESEARCH_MAX_TOOL_CALLS`          | 6       | Max tool calls per decision-round research pass                                                                             |
| `RESEARCH_MAX_ROUNDS`              | 8       | Max conversation rounds in a single research pass                                                                           |
| `RESEARCH_TOOL_RESULT_CHARS`       | 65536   | Per-tool-result cap (chars) before it enters the LLM's message history. Head-trimmed beyond this. ~4 chars ≈ 1 token.       |
| `MULTI_AGENT_FOCUS_COUNT`          | 3       | When multi-agent on: how many candidates fan out                                                                            |
| `MULTI_AGENT_PER_AGENT_TOOL_CALLS` | 6       | Per-agent tool-call cap                                                                                                     |
| `MULTI_AGENT_PER_AGENT_ROUNDS`     | 8       | Per-agent round cap                                                                                                         |

**Sizing `RESEARCH_TOOL_RESULT_CHARS` to your loaded context**:

| Loaded context | Suggested value | Why                                          |
|----------------|-----------------|----------------------------------------------|
|  32 K          |  8000           | ~2 K tokens/result, ~8 KV results per prompt |
|  64 K          | 16000           | ~4 K tokens/result                           |
| 128 K          | 65536 (default) | ~16 K tokens/result, fits history comfortably|
| 256 K          | 131072          | ~32 K tokens/result, near-lossless payloads  |

If the combined prompt overflows regardless, the researcher loop still
catches context-exceeded errors and auto-shrinks the middle of message
history (keeping system + original question + latest turn). You'll see
a `[Context trimmed: N older messages dropped]` marker — that's expected
when pushing limits.

---

## Watchlist + strategy prompt

```
STOCK_WATCHLIST=SPY,QQQ,AAPL,MSFT,NVDA,AMD,TSLA,META,GOOGL,AMZN
STOCK_STRATEGY_NOTE=Momentum day-trading on liquid US equities.
```

`STOCK_STRATEGY_NOTE` is injected verbatim into the decision prompt. Use
it to tell the AI what kind of trader you want it to be (momentum /
mean-reversion / news-driven / neutral-options-only / etc.).
