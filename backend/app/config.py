from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Mode
    paper_mode: bool = True

    # Server
    app_host: str = "127.0.0.1"
    app_port: int = 8010
    database_url: str = "sqlite+aiosqlite:///./autotrader.sqlite"
    jwt_secret: str = "replace_me_with_openssl_rand_hex_32"
    jwt_expires_minutes: int = 1440

    # CORS — comma-separated list of origins the frontend may call from.
    # Defaults to localhost dev ports. Add your LAN IP (e.g.
    # ``http://192.168.1.10:3010``) when running the dashboard from
    # another device on the same network.
    cors_origins: str = "http://localhost:3010,http://127.0.0.1:3010"

    # AI — provider switch. "openrouter" or "lmstudio".
    ai_provider: str = "openrouter"

    # OpenRouter (OpenAI-compatible API, serves Claude/GPT/Gemini/etc)
    openrouter_api_key: str = "replace_me"
    claude_model: str = "anthropic/claude-sonnet-4.5"

    # LM Studio — local OpenAI-compatible server (no key required).
    lmstudio_base_url: str = "http://localhost:1234/v1"
    lmstudio_model: str = "local-model"

    # Alpaca
    alpaca_api_key: str = "replace_me"
    alpaca_api_secret: str = "replace_me"
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    alpaca_data_url: str = "https://data.alpaca.markets"

    # Polymarket — adapter is EXPERIMENTAL. Set to true ONLY after you
    # have read the warning in README and understand that the on-chain
    # path has not been validated end-to-end. Default-off so a stale
    # POLYMARKET_PRIVATE_KEY in your .env can't accidentally start a
    # real adapter.
    polymarket_enabled: bool = False
    polymarket_private_key: str = "replace_me"
    polymarket_clob_api_key: str = ""
    polymarket_clob_secret: str = ""
    polymarket_clob_passphrase: str = ""
    polymarket_clob_url: str = "https://clob.polymarket.com"
    polymarket_chain_id: int = 80002

    # News
    finnhub_api_key: str = ""
    polygon_api_key: str = ""

    # Web search — optional API-keyed backends, tried in order before
    # falling back to keyless DuckDuckGo HTML scraping (which is rate-
    # limited and often blocked). Get any one of these and the research
    # agent's web_search becomes reliable.
    #   Tavily:  https://tavily.com (1000 free searches/mo, LLM-tuned)
    #   Brave:   https://brave.com/search/api (2000 free/mo)
    #   Serper:  https://serper.dev (2500 free queries, Google results)
    tavily_api_key: str = ""
    brave_search_api_key: str = ""
    serper_api_key: str = ""

    # Loop cadence
    stock_decision_interval_min: int = 5
    polymarket_decision_interval_min: int = 15
    runtime_monitor_interval_sec: int = 30

    # Scout loop — fast-cadence discovery that pushes candidates onto a
    # shared queue; the decision loop consumes from it on its own cadence.
    scout_enabled: bool = True
    scout_interval_min: int = 2
    scout_queue_ttl_sec: int = 600
    scout_queue_max_size: int = 50
    scout_per_bucket: int = 5
    # Optional LLM-driven refinement of the scout's raw scan. Off by
    # default; costs ~1 tiny call per scout tick when enabled.
    scout_llm_enabled: bool = False

    # Daily LLM spend ceiling. Scout and decision loops skip ticks once
    # today's total cost_usd crosses this. 0 disables the gate.
    daily_llm_budget_usd: float = 0.0

    # When true, scout + decision + monitor loops skip their tick if the
    # US equity market is closed (weekday + 9:30–16:00 ET; brokers that
    # expose `is_market_open()` use the authoritative clock). Turn off
    # for debugging or 24/7 dry runs.
    respect_market_hours: bool = True

    stock_strategy_note: str = (
        "Short-term quick money, any-direction. Trade bullish, bearish, or "
        "neutral setups using the tier-allowed structure that best fits IV "
        "regime and catalyst. No long-term investments."
    )

    # Screener — full-universe needle-in-haystack scan.
    screener_top_k: int = 10
    screener_min_price: float = 5.0
    screener_min_prev_volume: int = 500_000

    # Research agent — lets the AI call web_search / fetch_url before
    # committing to propose_trade. Adds latency + token spend; toggle off
    # for runs where prompt context is already sufficient.
    research_enabled: bool = True
    research_max_tool_calls: int = 6
    research_max_rounds: int = 8
    # Per-tool-result cap in chars (~4 chars/token) before it enters the
    # LLM's message history. Default 65536 ≈ 16k tokens, sized for 128 K+
    # loaded contexts. Smaller LM Studio loads just trigger the researcher's
    # auto-shrink-and-retry path. Raise to 131072 for a 256 K loaded ctx.
    # See README "Context-size guide" for the full sizing table.
    research_tool_result_chars: int = 65536

    # Multi-agent orchestrator — fan out per-symbol research agents in
    # parallel, then feed their findings to the decision agent.
    multi_agent_enabled: bool = False
    multi_agent_focus_count: int = 3
    multi_agent_per_agent_tool_calls: int = 6
    multi_agent_per_agent_rounds: int = 8

    # Position-review agent — fast-cadence LLM scanner that looks at each
    # open position plus fresh news and decides hold/close/tighten_stop
    # without waiting for the slow decision loop. Runs in parallel tool
    # calls, one round per tick, so N positions cost one LLM call.
    position_review_enabled: bool = True
    position_review_interval_sec: int = 90

    # Circuit-breaker — auto-pause agents after N consecutive losing
    # closes within the current Pacific day. 0 disables (default). Only
    # explicit user pause or the pause-when-market-closed toggle should
    # stop agents; a losing streak alone is a market regime, not an
    # emergency.
    circuit_breaker_consecutive_losses: int = 0

    # DTE watchdog — auto-close option positions when the nearest leg's
    # expiry is <= this many days away. 0 disables.
    option_dte_watchdog_days: int = 1

    # Post-mortem agent — LLM review of every closed trade. Saves a
    # structured lesson to the DB; decision prompts can surface it. Costs
    # one small LLM call per close.
    post_mortem_enabled: bool = True

    # Macro/regime agent — runs once per US-equity session open, caches
    # a compact risk-on/risk-off/volatile/ranging regime label that
    # stocks prompts inject. One LLM call per day.
    macro_regime_enabled: bool = True

    @field_validator(
        "openrouter_api_key",
        "alpaca_api_key",
        "alpaca_api_secret",
        "polymarket_private_key",
        "jwt_secret",
    )
    @classmethod
    def reject_placeholder(cls, v: str, info) -> str:
        # Allow placeholders during import-time but the CLI entrypoint checks
        # via assert_secrets_configured() before starting live trading.
        return v

    def assert_secrets_configured(self, *, require_polymarket: bool = False) -> None:
        """Fail fast before starting the scheduler if required secrets
        are still placeholders."""
        missing = []
        if self.ai_provider.lower() == "openrouter" and "replace_me" in self.openrouter_api_key:
            missing.append("OPENROUTER_API_KEY")
        if "replace_me" in self.alpaca_api_key or "replace_me" in self.alpaca_api_secret:
            missing.append("ALPACA_API_KEY / ALPACA_API_SECRET")
        if "replace_me" in self.jwt_secret:
            missing.append("JWT_SECRET (run: openssl rand -hex 32)")
        if require_polymarket and "replace_me" in self.polymarket_private_key:
            missing.append("POLYMARKET_PRIVATE_KEY")
        if missing:
            raise RuntimeError(
                "Refusing to start — secrets still contain 'replace_me': "
                + ", ".join(missing)
                + ". Update backend/.env and try again."
            )

    @property
    def mode_label(self) -> str:
        return "PAPER" if self.paper_mode else "LIVE"


@lru_cache
def get_settings() -> Settings:
    return Settings()
