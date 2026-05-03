# autotrader — backend

FastAPI + SQLAlchemy + APScheduler. Async end-to-end on SQLite by default.

For project-wide setup, see the top-level [README](../README.md).
For the agent mesh deep-dive, see [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md).

---

## Dev

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

cp .env.example .env
# fill in JWT_SECRET, OPENROUTER_API_KEY or LMSTUDIO_MODEL,
# ALPACA_API_KEY, ALPACA_API_SECRET; everything else can default.

python -m app.main               # → http://127.0.0.1:3003
```

DB schema is auto-created on first boot (`init_db()`), no migration step.
SQLite file lives at `backend/autotrader.sqlite` (git-ignored).

---

## Tests

```bash
pytest                           # full suite
pytest tests/test_risk_engine.py -v   # the critical one
pytest --cov=app --cov-report=term-missing
```

Risk-engine tests are non-negotiable — every guardrail has at least one
explicit test. Any change under `app/risk/` needs a matching test case.

Lint + typecheck:

```bash
ruff check .
ruff format .
mypy app
```

---

## Directory map

```
app/
├── activity/           # in-process event bus + SSE sink; persisted by main.py hook
├── ai/
│   ├── llm_provider.py          # OpenAI-compatible wrapper for OpenRouter + LM Studio
│   ├── research.py              # WebSearchClient (Tavily/Brave/Serper/DDG), UrlFetchClient
│   ├── research_toolbelt.py     # The shared tool belt — every agent pulls tools from here
│   ├── researcher.py            # Streaming researcher chat agent
│   ├── research_loop.py         # Per-symbol ResearchAgent (for multi-agent mode)
│   ├── orchestrator.py          # Fans out per-symbol research agents, aggregates
│   ├── scout_agent.py           # Optional LLM-refined scout
│   ├── position_review_agent.py # 90-s cadence position review
│   ├── post_mortem_agent.py     # Per-close structured lesson writer
│   ├── macro_agent.py           # Session-open regime label
│   ├── trace.py                 # LLM call tracing (prompt, response, tokens, cost)
│   └── usage.py                 # Rolling spend accounting
├── api/
│   ├── routes.py                # Main REST surface
│   ├── research_chat.py         # Researcher chat endpoints + SSE stream
│   ├── intel.py                 # /api/intel aggregator
│   ├── analytics.py             # /api/analytics time series
│   ├── deps.py                  # require_api_key dependency
│   └── schemas.py               # Pydantic I/O models
├── brokers/
│   ├── base.py                  # BrokerAdapter ABC
│   ├── alpaca.py                # Alpaca paper + live
│   ├── polymarket.py            # Polymarket stub (adapter in place; loop TODO)
│   └── null.py                  # No-op broker (boot without creds)
├── market_data/
│   ├── finnhub.py               # News + quotes + company profile
│   ├── movers.py                # Top gainers / losers / most active
│   ├── universe.py              # Tradable stock list
│   ├── screener.py              # Full-universe joint-condition screen
│   └── options.py               # Options chain
├── models/                      # SQLAlchemy row models (one file per table)
├── risk/
│   └── engine.py                # RiskEngine — pre-trade validation + runtime guardrails
├── scheduler/
│   ├── runner.py                # SchedulerRunner owns AsyncIOScheduler
│   ├── trading_loop.py          # Decision tick
│   ├── scout_loop.py            # Scout tick
│   ├── runtime_monitor.py       # Stop-loss / EOD / DTE checker
│   ├── position_review_loop.py  # Per-position LLM review
│   ├── post_mortem_loop.py      # Per-close post-mortem
│   ├── safety_monitor.py        # Circuit breaker + DTE watchdog
│   └── candidate_queue.py       # TTL-bounded scout → decision hand-off
├── strategies/
│   └── claude_stock_strategy.py # Prompt composition + proposal parsing
├── clock.py                     # US-equity session clock
├── config.py                    # pydantic-settings
├── db.py                        # async session factory + init_db
├── runtime.py                   # process-wide singletons (candidate queue)
└── main.py                      # FastAPI app + scheduler wiring
```

---

## Adding a tool to the research belt

Every agent that does research pulls tools from
`app/ai/research_toolbelt.py`. The shape:

1. Add a JSON schema entry in the `schemas()` dict.
2. Implement `_tool_<name>(self, args) -> tuple[str, str, dict]`
   returning `(full_json_text, preview_line, structured_payload)`.
3. Wire it into the `_DISPATCH` table.
4. (Frontend) add a rendering card to
   `frontend/src/components/research/ToolResultCards.tsx` if the payload
   deserves visual treatment; otherwise the generic JSON renderer kicks in.

The dispatcher has a defense-in-depth safety net: malformed tool calls,
ticker-shaped tool names, alias arg keys (`symbols=` vs `symbol=`), and
camelCase typos are normalized before dispatch. See `_coerce_args`,
`_normalize_tool_args`, `_fuzzy_match_tool`, and `_infer_tool_from_args`.

---

## Adding a risk guardrail

1. Add a field to `RiskConfig` (dataclass) in `app/risk/engine.py`.
2. Check it in `RiskEngine.validate_proposal()` or the runtime path.
3. Add a test in `tests/test_risk_engine.py` that explicitly proves the
   guardrail fires — both the allow-path and the deny-path.
4. Surface the knob in `app/models/risk_config.py` + the frontend
   `/risk-config` page.

---

## Configuration

Every knob is a `Settings` field in `app/config.py`, overridable via
`.env`. The top-level README has a full table of all knobs; the short
version is that every `scout_*`, `research_*`, `multi_agent_*`,
`position_review_*`, `post_mortem_*`, and `macro_regime_*` flag
corresponds to an agent loop you can toggle independently.

Secrets guard: `settings.assert_secrets_configured()` runs before the
scheduler boots and raises if any required secret still reads
`replace_me`.

---

## Logging

```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
)
```

External-API failures (Finnhub 403/404, web search empty, SEC filing not
found) are logged as single-line INFO — no stack traces for expected
failures. Unexpected exceptions still raise with full tracebacks.
