"""Stock-researcher chat agent.

An agentic chat loop that delegates its tool belt to
``ResearchToolbelt``. The user asks a question about one or more
tickers; the agent can fan out across the full research toolkit —
profile / news / financials / analyst / earnings / insiders / filings
/ peers / price history / technicals / deep_dive — and synthesise.

Output is streamed as events so the UI can show the agent working in
real time. The loop persists each message so the conversation is
resumable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.llm_provider import LLMProvider
from app.ai.research import UrlFetchClient, WebSearchClient
from app.ai.research_toolbelt import (
    ResearchToolbelt,
    cache_signature,
    is_cacheable,
)
from app.ai.usage import log_usage
from app.market_data.finnhub import FinnhubClient
from app.models import ResearchMessage

logger = logging.getLogger(__name__)


# Soft cap on any single tool-result message before it enters `messages`.
# Picked to keep a deep_dive (biggest single-tool payload) at ~2k tokens.
# Tool cards in the UI still render from the structured payload, so this
# only affects what the LLM sees — not what the user sees.
_TOOL_RESULT_HARD_CAP = 8000

_CONTEXT_EXCEEDED_MARKERS = (
    "context size",
    "context length",
    "context_length_exceeded",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "request too large",
)


def _is_context_exceeded(exc: BaseException) -> bool:
    s = str(exc).lower()
    return any(m in s for m in _CONTEXT_EXCEEDED_MARKERS)


def _truncate_tool_result(text: str, cap: int = _TOOL_RESULT_HARD_CAP) -> str:
    """Cap an individual tool_result payload before it enters message history.

    Most tools already return compact JSON, but `deep_dive`, `read_filing`,
    and raw news bundles can produce tens of KB. We keep the head (where
    structure + summary usually sit) and stamp a truncation marker so the
    model knows the result was abbreviated and can re-request narrower
    data if it actually needs the tail.
    """
    if len(text) <= cap:
        return text
    # Snip on a JSON-ish boundary if one is nearby to keep it parseable-ish.
    head = text[: cap - 120]
    boundary = max(head.rfind("},"), head.rfind("\n"))
    if boundary > cap - 600:
        head = head[: boundary + 1]
    return (
        head
        + f"\n…[truncated: {len(text) - len(head):,} chars of {len(text):,} "
        f"total; call the tool again with narrower args if you need the rest]"
    )


def _shrink_messages_for_context(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Drop the oldest tool-result pairs to fit under the provider's context.

    Keeps: system prompt, original user question, the most recent assistant
    turn + its tool results. Returns None if nothing more can reasonably be
    dropped (all we have left is the essentials).
    """
    if len(messages) <= 3:
        return None

    # Find the first user message (the question). Keep everything before
    # it (system prompt + any prior-conversation history the caller
    # seeded) as the "prefix" — we'll trim the middle, not the edges.
    first_user_idx = next(
        (i for i, m in enumerate(messages) if m.get("role") == "user"), 0
    )
    prefix = messages[: first_user_idx + 1]

    # Walk backwards from the end collecting "recent" context. We want the
    # latest assistant + its tool_call results intact; drop the middle.
    tail: list[dict[str, Any]] = []
    kept_assistants = 0
    for msg in reversed(messages[first_user_idx + 1:]):
        tail.insert(0, msg)
        if msg.get("role") == "assistant":
            kept_assistants += 1
        if kept_assistants >= 1 and len(tail) >= 4:
            break

    if len(prefix) + len(tail) >= len(messages):
        return None  # Nothing to drop.

    dropped = len(messages) - (len(prefix) + len(tail))
    notice = {
        "role": "user",
        "content": (
            f"[Context trimmed: {dropped} older tool-result message(s) "
            f"were dropped to fit the model's context window. Work from "
            f"the evidence still visible below. If you need something "
            f"that was dropped, re-request it with narrower args.]"
        ),
    }
    return [*prefix, notice, *tail]


_THINK_TAG_NAMES = r"(?:think(?:ing)?|reasoning|scratchpad|scratch|analysis)"
# Backreference `(?P=tag)` forces the closer to be the SAME tag name as the
# opener. Without it, `<think>...<analysis>...</analysis>...</think>` would
# lazy-match `<think>...</analysis>` and leave a stray `</think>` behind —
# exactly the leakage Qwen3 produces when it nests analysis inside think.
_THINK_CLOSED_RE = re.compile(
    rf"<\s*(?P<tag>{_THINK_TAG_NAMES})\s*>.*?<\s*/\s*(?P=tag)\s*>\s*",
    flags=re.DOTALL | re.IGNORECASE,
)
# Permissive pair — matches when the model opens with one tag name and closes
# with a different one (Qwen3 sometimes does `<thinking>...</think>`).
_THINK_PAIRED_ANY_RE = re.compile(
    rf"<\s*{_THINK_TAG_NAMES}\s*>.*?<\s*/\s*{_THINK_TAG_NAMES}\s*>\s*",
    flags=re.DOTALL | re.IGNORECASE,
)
_THINK_UNCLOSED_RE = re.compile(
    rf"<\s*{_THINK_TAG_NAMES}\s*>.*$",
    flags=re.DOTALL | re.IGNORECASE,
)
# Orphan closer `</think>` with no matching opener — the opener got stripped
# by the streaming layer or the model never emitted one. Everything before
# the closer was reasoning, so cut up to and including it.
_THINK_ORPHAN_CLOSER_RE = re.compile(
    rf"\A.*?<\s*/\s*{_THINK_TAG_NAMES}\s*>\s*",
    flags=re.DOTALL | re.IGNORECASE,
)
# Bare tags left over after everything else (e.g. self-closing weirdness).
_ORPHAN_TAG_RE = re.compile(
    rf"<\s*/?\s*{_THINK_TAG_NAMES}\s*>",
    flags=re.IGNORECASE,
)
_BOTTOM_LINE_RE = re.compile(r"\*\*\s*BOTTOM\s*LINE\s*:\s*\*\*", flags=re.IGNORECASE)


def _strip_think(text: str) -> str:
    """Remove reasoning/scratchpad text some models leak into content.

    Handles four leakage modes:
    1. Fully enclosed <think>...</think> (DeepSeek R1, Qwen3, etc.), with
       proper tag-name matching so nested `<analysis>` inside `<think>`
       doesn't break the pairing.
    2. Unclosed opener — model ran out of tokens mid-thought; drop from
       the opener to end-of-string.
    3. Orphan `</think>` / `<analysis>` tags left over from nesting.
    4. Preamble before ``**BOTTOM LINE:**`` — when the answer is formatted
       that way, anything before it is narration.
    """
    # Iterate to convergence so nested same-name tags unwind.
    prev = None
    out = text
    while out != prev:
        prev = out
        out = _THINK_CLOSED_RE.sub("", out)
    # Mismatched pair (<thinking>...</think>): sweep permissively after the
    # strict pass so we don't chew across correctly-nested blocks.
    prev = None
    while out != prev:
        prev = out
        out = _THINK_PAIRED_ANY_RE.sub("", out)
    out = _THINK_UNCLOSED_RE.sub("", out)
    # Orphan closer with a preamble: everything up to it was reasoning.
    prev = None
    while out != prev:
        prev = out
        out = _THINK_ORPHAN_CLOSER_RE.sub("", out)
    out = _ORPHAN_TAG_RE.sub("", out)
    m = _BOTTOM_LINE_RE.search(out)
    if m and m.start() > 0:
        out = out[m.start():]
    return out.strip()


SYSTEM_PROMPT = """\
You are the STOCK RESEARCHER agent. Your job: deliver a complete,
evidence-backed research report with clear bull + bear cases and an
actionable recommendation. Every claim must be grounded in a tool
result, not memory.

TOOL BELT:

Ticker resolution (use FIRST when the user gives a company name)
- search_tickers — name-to-ticker lookup. Pass the human company name
  (NOT a ticker guess). Returns candidate symbols + descriptions so you
  can pick the right one before pulling data. If the user types
  "Katapult", "Palantir", "the EV startup", etc., call this first.
  NEVER guess a ticker from memory — a wrong guess wastes every
  downstream tool call.

One-shot dossier (use this FIRST for any unfamiliar ticker — AFTER you
have the ticker)
- deep_dive — profile, quote, financials, analyst consensus, next
  earnings, insiders, top news, top filings, peers (with industry
  verification) and sector-scoped web searches in ONE call

Identification & fundamentals
- get_company_profile — name, industry, exchange, market cap
- get_basic_financials — 52w range, P/E, beta, margins, returns
- get_peers — competitor list enriched with each peer's industry label
  plus a name+industry web search for real competitors

Price & technicals
- get_quote — live quote + intraday OHLC
- get_price_history — daily bars (UI renders these as a chart; always
  pull ≥90 days for any ticker that is the subject)
- get_intraday_history — 5m/15m/1h bars for day-trade setups
- get_technicals — computed SMA/RSI/MACD/ATR

Catalysts & sentiment
- get_company_news — recent headlines
- get_analyst_ratings — buy/hold/sell counts + consensus price target
- get_earnings — next earnings date + historical beats/misses
- get_insider_transactions — Form-4 buys/sells with 30d/90d/365d net
  flows, top-insider rollups, and the largest notable transactions
- get_ownership — top 10 institutional + fund shareholders with shares
  held, filing-over-filing share change, and estimated USD value
- get_market_context — SPY/QQQ/VIX/DXY/TLT snapshot for regime context

First-hand sources
- get_sec_filings — list of 10-K, 10-Q, 8-K, S-1, S-4, etc.
- search_sec — full-text EDGAR search
- read_filing — distilled highlights (8-K items, risk factors, MD&A,
  notable $ figures, share actions, pct changes) from a filing URL
- fetch_url — generic URL fetch
- web_search — general web (unreliable on this host; if it returns
  "unavailable", pivot to the tools above instead of retrying)

Our own context
- get_recent_trades, get_recent_decisions — history on this ticker

WORK PROCESS:

0. If the user message contains a company NAME instead of a ticker
   (e.g. "Katapult", "Palantir Technologies", "what about Rivian?"),
   call `search_tickers` with the name FIRST. Pick the best match from
   its `matches` list — prefer common stock on NASDAQ/NYSE, prefer an
   exact or prefix description match — then use that ticker for every
   subsequent tool call. Do NOT call downstream tools with a guessed
   ticker; downstream tools reject unknown symbols and waste the
   tool-call budget.
1. For any unfamiliar ticker, call deep_dive first. One call returns
   the dossier that would otherwise take 6+ single-purpose calls.
2. Always pull get_price_history (≥90 days) and get_technicals so the
   UI can chart it.
3. Pull get_insider_transactions AND get_ownership — together they
   show insider conviction + smart-money crowding.
4. For any 8-K or recent filing of interest, call read_filing to get
   the extracted highlights (NOT the raw body — the tool distills it).
5. For a ticker you already know well, skip deep_dive and fan out.
6. Never conclude "I can't find anything" after one or two tools.
   The SEC + Finnhub stack is reliable even when web_search is not.
7. On peers: the peers payload flags industry-mismatched entries.
   Drop them silently and fill the peer set with real competitors
   from the sector web search or from the 10-K/S-1 description.
8. STOP when you have enough evidence. Don't keep calling tools for
   incremental polish.

OUTPUT — write ONLY the final research report, no process narration.

For trade-evaluation and analysis questions ("is X a buy?", "tell me
about Y", "should I trade Z?"), produce this EXACT structure with
these EXACT markdown headings. Do not skip sections — if evidence is
thin in a section, say so explicitly inside it.

**BOTTOM LINE:** <one sentence: buy / hold / avoid / watch + the key reason>

## Summary
One paragraph: what the company does, size, where it trades, current
price, and the single most important fact for a trader right now.

## Price & Technicals
Current price, 52w range, trend vs SMA-50/SMA-200, RSI, MACD, ATR,
recent % move. Note distance from 52w high/low. Call out any setup
(breakout, pullback, base, breakdown).

## Fundamentals
P/E (or why it's N/M), margins, revenue growth, profitability. How
it compares to peers on the numbers that matter for this business.

## Catalysts & Sentiment
Cover ALL of the following in short paragraphs or a table:
- Recent news headlines (last 30d) with dates + URLs
- Next earnings date + recent beat/miss history
- Analyst consensus (buy/hold/sell count) + median price target + implied % move
- Insider activity: net $ flow over 30d/90d/365d, notable named buyers/sellers
- Institutional ownership: top holders, concentration, recent share-count changes
- Recent 8-K items or material filings

## Peer Comparison
Markdown table: ticker | name | market cap | P/E | YTD % | notable.
Only real industry peers — drop the industry-mismatched ones.

## Bull Case
3-5 concrete, evidence-linked bullets. Each bullet cites a specific
number, event, or filing. "AI tailwind" is not a bull point; "Q3
compute revenue +94% YoY per 10-Q filed 2026-02-14" is.

## Bear Case
3-5 concrete bullets with the same evidence standard. Include
valuation risk, competitive threats, insider selling if present,
debt/liquidity issues, regulatory overhangs, earnings risk.

## Risks to Monitor
Specific trigger events that would change the thesis (macro prints,
earnings, court rulings, product launches, etc.).

## Recommendation
State ONE clear directional call — exactly one of:
- `LONG`
- `SHORT`
- `NO-TRADE`
- `WAIT-FOR-<EVENT>` — where `<EVENT>` MUST be replaced with the
  concrete catalyst and its date (e.g. `WAIT-FOR-Q2-EARNINGS-2026-05-02`,
  `WAIT-FOR-FDA-PDUFA-2026-06-14`). **Never output the literal
  placeholder `<EVENT>` or the literal letter `X`** — substitute the
  real event, or pick a different verdict.

**Default to LONG, SHORT, or NO-TRADE.** These cover the overwhelming
majority of cases. Take a side when the evidence leans — you don't need
a slam-dunk to call LONG or SHORT, just a real edge.

**`WAIT-FOR-<EVENT>` is reserved for narrow situations where a specific
binary event lands within ~2 weeks and would materially reprice the
stock:**
- Earnings report in the next 5–10 trading days
- Scheduled FDA / regulatory / court ruling with a known date
- Known M&A / deal closing date
- Confirmed product launch or data readout with a date
- Scheduled macro print that is the dominant driver (CPI, FOMC) for a
  macro-sensitive name

If the event is "wait for more clarity", "wait for confirmation of
trend", "wait for a better entry", "wait for the market to calm down",
or any other vague qualifier — that is NOT a valid `WAIT-FOR-<EVENT>`.
Use NO-TRADE (if conviction is absent) or commit to LONG/SHORT with a
stop-loss (if conviction is directional).

If LONG or SHORT, include: entry zone, stop-loss, price target,
expected holding period. If NO-TRADE or `WAIT-FOR-<EVENT>`, state
exactly what would need to change for you to take a position — and for
`WAIT-FOR-<EVENT>`, name the event and its date.

For purely informational questions ("when is earnings?", "what does X
do?"), answer directly in prose — no BOTTOM LINE heading, no full
report.

For comparison questions ("A vs B", "compare X and Y", "which is
better: X or Y"), treat EVERY subject as a first-class ticker:

1. **Per-symbol data pulls are mandatory, not optional.** For EACH
   ticker in the comparison, call at minimum:
     - `get_price_history` (≥90 days) so the UI renders a chart for
       every subject side-by-side
     - `get_technicals`
     - `get_basic_financials`
     - `get_company_profile`
   Fan these out in parallel — the runtime executes tool calls
   concurrently within a single round, so one round gets every
   ticker's data. Do NOT pull data for just one side and infer the
   other from memory.
2. Also pull shared/comparative tools once: `get_market_context`,
   and peer/news tools per subject as relevant.
3. **Output layout for comparisons**:
   - Lead with `**BOTTOM LINE:**` naming the winner (or tie) and the
     decisive factor in one sentence.
   - Then a head-to-head markdown table with rows for the dimensions
     that matter (price, 52w range, P/E, margins, YoY revenue, YTD %,
     RSI, next earnings, analyst target, etc.) and ONE COLUMN PER
     TICKER.
   - Then per-ticker micro-sections using `### TICKER` headings — one
     paragraph each covering that ticker's setup, bull, bear.
   - Finish with a concrete `Recommendation` section giving the call
     (`LONG <ticker>`, `SHORT <ticker>`, `PAIR: long A / short B`,
     `NO-TRADE`, or `WAIT-FOR-<EVENT>`).
4. The UI renders per-symbol charts side-by-side automatically
   whenever `get_price_history` is called for multiple tickers in
   the same turn — you don't need to ask for a chart, just make the
   calls.

Universal rules:
- Synthesize; do not regurgitate JSON.
- Always include the price-history chart tool result and technicals
  for EVERY subject (not just the first one).
- Tables for any list of comparable items.
- Cite URLs from tool results. Never invent URLs.
- Insights > data. If evidence is thin, say "inconclusive" rather
  than manufacture conviction.
- No `<think>`, `<reasoning>`, or scratchpad tags.
- No meta-commentary about tools or your process. The user wants
  the answer, not a play-by-play.
"""


@dataclass
class ResearchEvent:
    type: str  # status | text | tool_call | tool_result | message_saved | done | error
    data: dict[str, Any]


async def _return_cached(
    prior: tuple[str, str, dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    return prior


class ResearcherAgent:
    """Streaming researcher. ``stream()`` yields ``ResearchEvent`` objects.

    The tool belt is shared with other agents via ``ResearchToolbelt``;
    the agent here owns only the conversation loop, streaming, per-turn
    caching, and message persistence.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        session_factory: async_sessionmaker,
        toolbelt: ResearchToolbelt | None = None,
        finnhub: FinnhubClient | None = None,
        search: WebSearchClient | None = None,
        fetch: UrlFetchClient | None = None,
        alpaca_api_key: str | None = None,
        alpaca_api_secret: str | None = None,
        alpaca_data_url: str = "https://data.alpaca.markets",
        max_rounds: int = 20,
        max_tokens: int = 8192,
        tool_result_chars: int = _TOOL_RESULT_HARD_CAP,
    ) -> None:
        self._provider = provider
        self._session_factory = session_factory
        self._toolbelt = toolbelt or ResearchToolbelt(
            finnhub=finnhub,
            search=search,
            fetch=fetch,
            alpaca_api_key=alpaca_api_key,
            alpaca_api_secret=alpaca_api_secret,
            alpaca_data_url=alpaca_data_url,
            session_factory=session_factory,
        )
        self._tools = self._toolbelt.all_schemas
        self._max_rounds = max_rounds
        self._max_tokens = max_tokens
        self._tool_result_chars = max(1000, int(tool_result_chars))

    async def stream(
        self,
        *,
        conversation_id: int,
        prior_messages: list[dict[str, Any]],
        user_message: str,
    ) -> AsyncIterator[ResearchEvent]:
        """Run one user turn. Yields events as the agent works."""
        await self._persist_message(
            conversation_id=conversation_id, role="user", content=user_message
        )
        yield ResearchEvent(
            type="message_saved", data={"role": "user", "content": user_message}
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            *prior_messages,
            {"role": "user", "content": user_message},
        ]

        # Per-turn result cache keyed by (tool_name, canonical_args). Kills
        # the common failure mode where the agent re-issues the same tool
        # across rounds instead of reading prior results. Time-sensitive
        # tools (quotes / intraday) opt out via is_cacheable.
        call_cache: dict[str, tuple[str, str, dict[str, Any]]] = {}

        # Track the most recent non-empty prose the model emitted so we
        # have something to fall back to if finalization returns empty.
        last_interim_text = ""
        empty_rounds = 0

        for round_idx in range(self._max_rounds):
            yield ResearchEvent(
                type="status",
                data={"round": round_idx + 1, "state": "thinking"},
            )
            try:
                response, messages = await self._call_with_context_retry(
                    messages=messages,
                    tool_choice="auto",
                )
            except Exception as exc:
                logger.exception("researcher LLM call failed")
                yield ResearchEvent(type="error", data={"error": str(exc)})
                return

            call_id: int | None = None
            try:
                row = await log_usage(
                    self._session_factory,
                    response,
                    purpose="research_chat",
                    agent_id="researcher",
                    round_idx=round_idx,
                    prompt_messages=list(messages),
                )
                call_id = row.id
            except Exception:
                logger.exception("failed to persist researcher usage")

            choice = response.raw_response.get("choices", [{}])[0]
            msg = choice.get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            raw_text = msg.get("content") or ""
            text = _strip_think(raw_text)

            messages.append({
                "role": "assistant",
                "content": raw_text,
                "tool_calls": tool_calls or None,
            })

            if text:
                last_interim_text = text

            if text and not tool_calls:
                await self._persist_message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=text,
                    call_id=call_id,
                )
                yield ResearchEvent(
                    type="text",
                    data={"content": text, "round": round_idx + 1},
                )
                yield ResearchEvent(
                    type="done",
                    data={"rounds": round_idx + 1, "call_id": call_id},
                )
                return

            if not tool_calls:
                empty_rounds += 1
                if empty_rounds >= 2 or round_idx >= self._max_rounds - 1:
                    # Model keeps returning empty content without tools —
                    # break out and force finalization rather than burning
                    # the whole round budget.
                    break
                messages.append({
                    "role": "user",
                    "content": "Continue — write your answer or call a tool.",
                })
                continue
            empty_rounds = 0

            yield ResearchEvent(
                type="status",
                data={"round": round_idx + 1, "state": "running_tools", "count": len(tool_calls)},
            )
            coros = []
            call_sigs: list[str | None] = []
            for call in tool_calls:
                name = (call.get("function") or {}).get("name") or ""
                args_raw = (call.get("function") or {}).get("arguments") or "{}"
                try:
                    args = (
                        json.loads(args_raw)
                        if isinstance(args_raw, str)
                        else dict(args_raw)
                    )
                except Exception:
                    args = {}
                sig = cache_signature(name, args) if is_cacheable(name) else None
                call_sigs.append(sig)
                yield ResearchEvent(
                    type="tool_call",
                    data={
                        "id": call.get("id"),
                        "name": name,
                        "arguments": args,
                    },
                )
                if sig is not None and sig in call_cache:
                    coros.append(_return_cached(call_cache[sig]))
                else:
                    coros.append(self._toolbelt.dispatch(name, args))
                await self._persist_message(
                    conversation_id=conversation_id,
                    role="tool_call",
                    content="",
                    tool_name=name,
                    tool_payload={"id": call.get("id"), "arguments": args},
                )

            results = await asyncio.gather(*coros, return_exceptions=True)

            for sig, outcome in zip(call_sigs, results, strict=True):
                if sig is None or isinstance(outcome, Exception):
                    continue
                if sig not in call_cache:
                    call_cache[sig] = outcome  # type: ignore[assignment]

            for call, outcome in zip(tool_calls, results, strict=True):
                name = (call.get("function") or {}).get("name") or ""
                if isinstance(outcome, Exception):
                    logger.warning("tool %s raised", name, exc_info=outcome)
                    payload = {"error": f"{type(outcome).__name__}: {outcome}"}
                    result_text = json.dumps(payload)
                    preview = payload["error"]
                else:
                    result_text, preview, payload = outcome
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{round_idx}",
                    "name": name,
                    "content": _truncate_tool_result(
                        result_text, cap=self._tool_result_chars
                    ),
                })
                await self._persist_message(
                    conversation_id=conversation_id,
                    role="tool_result",
                    content=preview,
                    tool_name=name,
                    tool_payload=payload if isinstance(payload, dict) else None,
                )
                yield ResearchEvent(
                    type="tool_result",
                    data={
                        "id": call.get("id"),
                        "name": name,
                        "preview": preview,
                        "payload": payload if isinstance(payload, dict) else None,
                    },
                )

        # Budget exhausted without a final answer. Force one last
        # completion with tools disabled so the user isn't left staring
        # at 20 tool calls and no verdict.
        final_round = self._max_rounds + 1
        messages.append({
            "role": "user",
            "content": (
                "You have reached the tool-call budget. Do NOT call any "
                "more tools. Write your final answer now using only the "
                "evidence already in this conversation. Follow the "
                "OUTPUT FORMAT — start with `**BOTTOM LINE:**`. If the "
                "evidence is inconclusive, say so explicitly."
            ),
        })
        yield ResearchEvent(
            type="status",
            data={"round": final_round, "state": "finalizing"},
        )
        try:
            response, messages = await self._call_with_context_retry(
                messages=messages,
                tool_choice="none",
            )
        except Exception as exc:
            logger.exception("researcher finalization failed")
            yield ResearchEvent(
                type="error",
                data={
                    "error": (
                        f"loop exceeded {self._max_rounds} rounds and "
                        f"finalization failed: {exc}"
                    )
                },
            )
            return

        final_call_id: int | None = None
        try:
            row = await log_usage(
                self._session_factory,
                response,
                purpose="research_chat",
                agent_id="researcher",
                round_idx=final_round,
                prompt_messages=list(messages),
            )
            final_call_id = row.id
        except Exception:
            logger.exception("failed to persist finalization usage")

        choice = response.raw_response.get("choices", [{}])[0]
        msg = choice.get("message") or {}
        raw_text = msg.get("content") or ""
        text = _strip_think(raw_text)
        if not text and last_interim_text:
            # Model refused to produce a fresh final answer. Fall back to
            # the most recent prose it did emit during the tool rounds so
            # the user always sees the research synthesis.
            text = last_interim_text
        if not text:
            yield ResearchEvent(
                type="error",
                data={
                    "error": (
                        f"loop exceeded {self._max_rounds} rounds; "
                        "model produced no final answer"
                    )
                },
            )
            return

        notice = (
            "_Note: agent hit the tool-call budget; this answer is "
            "synthesized from evidence gathered so far._\n\n"
        )
        final_text = notice + text
        await self._persist_message(
            conversation_id=conversation_id,
            role="assistant",
            content=final_text,
            call_id=final_call_id,
        )
        yield ResearchEvent(
            type="text",
            data={"content": final_text, "round": final_round},
        )
        yield ResearchEvent(
            type="done",
            data={"rounds": final_round, "call_id": final_call_id, "forced": True},
        )

    async def _call_with_context_retry(
        self,
        *,
        messages: list[dict[str, Any]],
        tool_choice: str,
    ) -> tuple[Any, list[dict[str, Any]]]:
        """Call the provider; shrink history and retry on context-exceeded.

        Returns the response plus the (possibly shrunk) messages list so
        the caller can continue from the trimmed state instead of the
        original oversized one.
        """
        attempt = 0
        max_attempts = 4
        while True:
            try:
                response = await self._provider.raw_completion(
                    messages=messages,
                    tools=self._tools,
                    max_tokens=self._max_tokens,
                    tool_choice=tool_choice,
                )
                return response, messages
            except Exception as exc:
                if not _is_context_exceeded(exc) or attempt >= max_attempts:
                    raise
                shrunk = _shrink_messages_for_context(messages)
                if shrunk is None or len(shrunk) >= len(messages):
                    # Can't shrink further — propagate so the caller emits
                    # a clean error event.
                    raise
                logger.warning(
                    "context exceeded (attempt %d); trimming %d → %d messages",
                    attempt + 1, len(messages), len(shrunk),
                )
                messages = shrunk
                attempt += 1

    async def _persist_message(
        self,
        *,
        conversation_id: int,
        role: str,
        content: str,
        tool_name: str | None = None,
        tool_payload: dict | None = None,
        call_id: int | None = None,
    ) -> int:
        async with self._session_factory() as session:
            row = ResearchMessage(
                conversation_id=conversation_id,
                role=role,
                content=content,
                tool_name=tool_name,
                tool_payload=tool_payload,
                call_id=call_id,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row.id


# Backwards-compat export for any module that imported RESEARCHER_TOOLS.
RESEARCHER_TOOLS = ResearchToolbelt().all_schemas


__all__ = ["ResearcherAgent", "ResearchEvent", "RESEARCHER_TOOLS", "SYSTEM_PROMPT"]
