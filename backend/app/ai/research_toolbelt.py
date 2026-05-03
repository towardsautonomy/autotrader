"""Shared research tool belt.

Single source of truth for the information-gathering tools every agent
can use (researcher chat, per-symbol research agents, decision loop).

Each tool has two halves:

  · a JSON schema that goes into the model's ``tools`` array
  · a dispatcher method that executes the tool and returns
    ``(full_json_text, preview, structured_payload)``

The class takes the backend dependencies (Finnhub, search/fetch, Alpaca
creds, DB session factory) in its constructor and keeps them bound so
individual tools don't need to be wired separately.

Agents opt in to the tools they want via ``schemas(include=...)`` and
dispatch by name via ``dispatch(name, args)``. Keeping everything in
one place means a new tool shows up for every agent for free instead
of being duplicated per-file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.ai.research import UrlFetchClient, WebSearchClient
from app.market_data.finnhub import FinnhubClient
from app.models import Decision, Trade

logger = logging.getLogger(__name__)

_SEC_HEADERS = {
    "User-Agent": "autotrader-researcher contact@autotrader.local",
    "Accept": "application/json",
}

_SEC_TICKER_CACHE: dict[str, Any] = {"fetched_at": 0.0, "by_ticker": {}}
_SEC_TICKER_TTL_SEC = 24 * 3600
_SEC_TICKER_LOCK = asyncio.Lock()


async def _load_sec_tickers() -> dict[str, dict[str, Any]]:
    now = time.time()
    cached = _SEC_TICKER_CACHE["by_ticker"]
    if cached and (now - _SEC_TICKER_CACHE["fetched_at"]) < _SEC_TICKER_TTL_SEC:
        return cached
    async with _SEC_TICKER_LOCK:
        cached = _SEC_TICKER_CACHE["by_ticker"]
        if cached and (now - _SEC_TICKER_CACHE["fetched_at"]) < _SEC_TICKER_TTL_SEC:
            return cached
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=_SEC_HEADERS) as c:
                resp = await c.get("https://www.sec.gov/files/company_tickers.json")
                resp.raise_for_status()
                raw = resp.json()
        except Exception:
            logger.warning("SEC ticker map fetch failed", exc_info=True)
            return cached or {}
        by_ticker: dict[str, dict[str, Any]] = {}
        for row in raw.values():
            t = str(row.get("ticker") or "").upper().strip()
            if not t:
                continue
            by_ticker[t] = {
                "cik_str": str(row.get("cik_str") or ""),
                "name": row.get("title"),
            }
        _SEC_TICKER_CACHE["by_ticker"] = by_ticker
        _SEC_TICKER_CACHE["fetched_at"] = time.time()
        return by_ticker


async def _sec_cik_for_ticker(symbol: str) -> str | None:
    m = await _load_sec_tickers()
    entry = m.get(symbol.upper().strip())
    return entry.get("cik_str") if entry else None


async def _sec_ticker_lookup(symbol: str) -> dict[str, Any] | None:
    m = await _load_sec_tickers()
    return m.get(symbol.upper().strip())


_TOOL_NAME_PREFIX_RE = re.compile(r"^[^A-Za-z_]+")
# Ticker-like: 1–5 uppercase letters, optional `.XX` country suffix
# (e.g. AAPL, BRK.B, 3665.TW). We match that so we can recover when the
# model accidentally used the ticker as the tool name.
_TICKER_LIKE_RE = re.compile(r"^[A-Z]{1,5}(?:[.\-][A-Z0-9]{1,3})?$")

_SEC_ARCHIVE_PATH_RE = re.compile(
    r"^(?P<base>https?://www\.sec\.gov/Archives/edgar/data/\d+/)"
    r"(?P<acc>[0-9][0-9A-Za-z\-]+?)"
    r"(?P<tail>/.*)?$"
)


def _sec_url_candidates(url: str) -> list[str]:
    """Generate fallback forms for a SEC EDGAR archive URL.

    The LLM sometimes builds URLs with dashes in the accession number
    (``0001628280-26-016910``) when EDGAR requires the dash-stripped form
    (``000162828026016910``). We only generate dash-stripped / dash-added
    variants here — deeper recovery (finding the real primary doc when
    the filename is wrong) is handled by ``_sec_discover_primary_doc``.
    """
    out = [url]
    m = _SEC_ARCHIVE_PATH_RE.match(url)
    if not m:
        return out
    base = m.group("base")
    acc = m.group("acc")
    tail = m.group("tail") or ""
    acc_nodash = acc.replace("-", "")
    if acc_nodash != acc:
        out.append(f"{base}{acc_nodash}{tail}")
    # Dedup preserving order.
    seen: set[str] = set()
    unique = []
    for u in out:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


async def _sec_discover_primary_doc(
    client: httpx.AsyncClient, url: str
) -> str | None:
    """Given a SEC folder URL, fetch index.json and return a plausible
    primary-document URL (first .htm file that isn't the filing index)."""
    m = _SEC_ARCHIVE_PATH_RE.match(url)
    if not m:
        return None
    base = m.group("base")
    acc = m.group("acc").replace("-", "")
    idx_url = f"{base}{acc}/index.json"
    try:
        resp = await client.get(idx_url)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None
    items = ((data.get("directory") or {}).get("item")) or []
    # Prefer the primary document — typically the first non-index .htm.
    for item in items:
        name = str(item.get("name") or "")
        if not name.lower().endswith((".htm", ".html")):
            continue
        if "index" in name.lower():
            continue
        return f"{base}{acc}/{name}"
    # Fall back to any htm including index.
    for item in items:
        name = str(item.get("name") or "")
        if name.lower().endswith((".htm", ".html")):
            return f"{base}{acc}/{name}"
    return None

_INDUSTRY_STOPWORDS = {
    "and",
    "of",
    "the",
    "&",
    "-",
    "a",
    "an",
    "for",
    "in",
    "services",
    "products",
    "industries",
    "industry",
    "equipment",
    "systems",
}


def _industry_tokens(label: str | None) -> set[str]:
    if not label:
        return set()
    raw = re.sub(r"[^A-Za-z0-9 ]", " ", label).lower().split()
    out: set[str] = set()
    for t in raw:
        if not t or t in _INDUSTRY_STOPWORDS or len(t) <= 2:
            continue
        # Crude stemming: drop trailing 's' to collapse plurals.
        if len(t) > 4 and t.endswith("s"):
            t = t[:-1]
        out.add(t)
    return out


# Words that look ticker-shaped (1–5 uppercase letters) but never are,
# so we don't waste a Finnhub lookup validating them.
_TICKER_FALSE_POSITIVES: frozenset[str] = frozenset(
    {
        "A", "AN", "AND", "AS", "AT", "BE", "BUT", "BY", "DO", "FOR", "FROM",
        "GO", "HAS", "HE", "IF", "IN", "IS", "IT", "ITS", "ME", "MY", "NO",
        "NOT", "OF", "ON", "OR", "OUR", "OUT", "SO", "THE", "THEY", "TO", "UP",
        "US", "VS", "WE", "WHO", "WHY", "WITH", "YOU", "YOUR",
        # Common uppercase noise that shows up in article titles / snippets.
        "CEO", "CFO", "CTO", "COO", "SEC", "FDA", "FTC", "IPO", "ETF", "USA",
        "USD", "EUR", "GBP", "JPY", "CNY", "AI", "ML", "API", "NYSE", "NASDAQ",
        "AMEX", "OTC", "ADR", "GAAP", "ESG", "EPS", "PE", "YOY", "QOQ", "YTD",
        "MTD", "DTD", "TTM", "ARR", "MRR", "CAGR", "EBIT", "EBITDA", "FCF",
        "ROI", "ROE", "ROA", "TAM", "SAM", "SOM", "CAPEX", "OPEX",
        "Q", "QQ", "QQQ", "FY", "CY", "AM", "PM", "ET", "PT", "CT", "MT",
        "BUY", "SELL", "HOLD", "LONG", "SHORT", "TODAY", "WEEK", "MONTH",
        "YEAR", "NEW", "TOP", "BIG", "BEST", "WORST", "HUGE", "LOW", "HIGH",
        "CASH", "LOSS", "GAIN", "RISK", "BULL", "BEAR", "DEAL", "MERGE",
        "NEWS", "STORY", "VIDEO", "LIVE", "REPORT", "SAYS", "SAID", "SEE",
    }
)

# Cashtags ($AAPL) are unambiguous; plain uppercase words need context.
_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})(?:[.\-][A-Z0-9]{1,3})?\b")
_PLAIN_TICKER_RE = re.compile(r"\b([A-Z]{2,5})\b")
_PARENTHETICAL_TICKER_RE = re.compile(
    # "Company Name (NYSE: XYZ)" or "Company Name (NASDAQ:XYZ)".
    r"\(\s*(?:NYSE|NASDAQ|AMEX|OTC|NYSEARCA|NYSEAMERICAN)\s*:\s*([A-Z]{1,5}(?:[.\-][A-Z0-9]{1,3})?)\s*\)",
    re.IGNORECASE,
)


def _extract_candidate_tickers(text: str) -> list[str]:
    """Pull ticker candidates out of a blob of text.

    Three layers of confidence:
      1. Exchange-qualified parentheticals (e.g. "(NYSE: ABC)") — near-certain
      2. Cashtags ($ABC) — high confidence
      3. Plain uppercase words — need later profile validation
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for rx in (_PARENTHETICAL_TICKER_RE, _CASHTAG_RE):
        for m in rx.finditer(text):
            t = m.group(1).upper()
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
    for m in _PLAIN_TICKER_RE.finditer(text):
        t = m.group(1).upper()
        if t in seen or t in _TICKER_FALSE_POSITIVES:
            continue
        seen.add(t)
        out.append(t)
    return out


def _industry_similar(a: str | None, b: str | None) -> bool:
    """Loose industry match — shared stemmed tokens count as a hit.

    Finnhub uses slightly different labels across tickers in the same
    sector (e.g. "Semiconductors" vs "Semiconductor Equipment" vs
    "Technology — Semiconductors"). Exact string match drops real peers,
    so we compare on stemmed-token overlap with stopwords removed.
    """
    if not a or not b:
        return False
    if a.strip().lower() == b.strip().lower():
        return True
    ta = _industry_tokens(a)
    tb = _industry_tokens(b)
    return bool(ta and tb and ta & tb)


def _canonical_tool_name(name: str) -> str:
    """Normalize tool names emitted by providers into canonical snake_case.

    Some LLM providers emit ``functions.deep_dive``, ``#deep_dive``,
    ``deep-dive``, etc. instead of the plain ``deep_dive``. We map all of
    those to the lowercase snake_case form the handler map uses.
    """
    s = (name or "").strip()
    if "." in s:
        s = s.rsplit(".", 1)[-1]
    s = _TOOL_NAME_PREFIX_RE.sub("", s)
    return s.replace("-", "_").lower()


_ARG_ALIASES: dict[str, str] = {
    "symbols": "symbol",
    "ticker": "symbol",
    "tickers": "symbol",
    "ticker_symbol": "symbol",
    "company_symbol": "symbol",
    "urls": "url",
    "link": "url",
    "q": "query",
    "search_query": "query",
    # search_tickers-ish aliases: the model loves "sentence", "text",
    # "name", "company_name", "company", etc. when it really wants the
    # name-to-ticker lookup. Route them to `query` so search_tickers
    # picks them up; for symbol-required tools we separately try to
    # salvage a ticker from any of these below in _normalize_tool_args.
    "sentence": "query",
    "text": "query",
    "name": "query",
    "company": "query",
    "company_name": "query",
    "query_text": "query",
    "input_text": "query",
    "n": "top_k",
    "limit_results": "top_k",
}


_TICKER_LOOKUP_HINT_RE = re.compile(
    r"\b(stock|ticker|symbol|share|equity|company|corp|inc|ltd|plc|llc)\b",
    flags=re.IGNORECASE,
)


def _infer_tool_from_args(args: dict[str, Any]) -> str | None:
    """Guess the tool the model meant based on the args shape.

    Called when the tool name was unparseable, missing, or the literal
    sentinel "unknown". Returns a canonical tool name or None.
    """
    if not isinstance(args, dict):
        return None
    keys = {k.lower() for k in args.keys()}
    # Intraday timeframe markers → intraday history.
    if "timeframe" in keys or "sessions" in keys:
        return "get_intraday_history"
    if "days" in keys or "period" in keys or "lookback" in keys:
        # Ambiguous between price_history and technicals. Price_history
        # is safer — it's a strict subset of what technicals needs.
        return "get_price_history"
    if "form_type" in keys or "filings" in keys:
        return "get_sec_filings"
    if "url" in keys:
        return "read_filing"
    if "query" in keys:
        # Disambiguate search_tickers vs web_search by query shape.
        q = str(args.get("query") or "").strip()
        q_up = q.upper()
        # Explicit ticker-lookup intent — "AAPL stock", "Kodiak company".
        if _TICKER_LOOKUP_HINT_RE.search(q):
            return "search_tickers"
        # Short, no stopwords, no punctuation → likely a company name.
        if q and len(q) <= 40 and len(q.split()) <= 4 and "?" not in q:
            # Bare ticker or tight name → search_tickers resolves it.
            if _TICKER_LIKE_RE.match(q_up) or q[0].isupper():
                return "search_tickers"
        return "web_search"
    # Default: treat symbol-only as "what is this ticker?".
    if "symbol" in keys or not keys:
        return "get_company_profile"
    return None


_NESTED_ARG_KEYS = ("input", "inputs", "parameters", "params", "args", "payload")


def _coerce_args(args: Any) -> dict[str, Any]:
    """Coerce whatever the model emitted into a dict.

    Handles:
    - dict (pass-through)
    - JSON string (parse)
    - bare ticker string (wrap as ``{"symbol": value}``)
    - list / tuple (drop; return {})
    - None / anything else (return {})
    - Nested wrappers like ``{"input": {...}}`` (unwrap one level)
    """
    # Double-encoded JSON — some providers wrap arguments as a string.
    if isinstance(args, str):
        s = args.strip()
        if not s:
            return {}
        if s.startswith("{") and s.endswith("}"):
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict):
                    return _coerce_args(parsed)
            except Exception:
                pass
        if _TICKER_LIKE_RE.match(s.upper()):
            return {"symbol": s.upper()}
        if s.startswith(("http://", "https://")):
            return {"url": s}
        return {"query": s}
    if not isinstance(args, dict):
        return {}
    # Unwrap one layer of ``{"input": {...}}``-style nesting.
    if len(args) == 1:
        only_key = next(iter(args))
        only_val = args[only_key]
        if only_key.lower() in _NESTED_ARG_KEYS and isinstance(only_val, dict):
            return _coerce_args(only_val)
    return dict(args)


def _normalize_tool_args(args: Any) -> dict[str, Any]:
    """Apply common arg-name aliases the model tends to hallucinate.

    Tools expect ``symbol`` / ``url`` / ``query`` singulars, but the
    model sometimes emits ``symbols``, ``ticker``, ``tickers``, etc.
    We canonicalize here instead of making every tool handler repeat the
    same aliasing logic.

    For list / comma-separated values we take the first token — all
    affected tools are single-symbol by design.
    """
    out = _coerce_args(args)
    for alias, canonical in _ARG_ALIASES.items():
        if alias not in out:
            continue
        val = out.pop(alias)
        if canonical in out:
            # Canonical already set — drop the alias rather than letting
            # it leak through as an unknown kwarg.
            continue
        if isinstance(val, list):
            val = val[0] if val else ""
        if isinstance(val, str) and "," in val:
            val = val.split(",", 1)[0]
        out[canonical] = val
    # Normalize ticker capitalization so downstream handlers match
    # consistently regardless of case.
    sym = out.get("symbol")
    if isinstance(sym, str):
        out["symbol"] = sym.strip().upper()
    # If we only have a `query` but not a `symbol`, and the query looks
    # ticker-shaped, promote it to `symbol` so legacy callers don't break.
    # This is a non-destructive copy — the original query is preserved.
    q = out.get("query")
    if (
        not out.get("symbol")
        and isinstance(q, str)
        and _TICKER_LIKE_RE.match(q.strip().upper())
        and q.strip().upper() not in _TICKER_FALSE_POSITIVES
    ):
        out["symbol"] = q.strip().upper()
    return out


_COMPARISON_SPLIT_RE = re.compile(
    r"\s+(?:vs\.?|versus|compared\s+to|v\.?s\.?)\s+",
    flags=re.IGNORECASE,
)


def _split_comparison(raw: str) -> list[str] | None:
    """Detect ``A vs B`` / ``A versus B`` / ``A compared to B`` in a string.

    Returns the trimmed parts if the string reads as a comparison between
    two (or more) named entities, else None. Used to auto-recover when the
    model puts a comparison phrase in place of a tool name.
    """
    if not raw or len(raw) > 200:
        return None
    parts = [p.strip() for p in _COMPARISON_SPLIT_RE.split(raw)]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None
    # Sanity-check each side looks like a short name, not a full sentence.
    for part in parts:
        if len(part) > 80 or len(part.split()) > 6:
            return None
    return parts[:4]  # cap — don't fan out more than 4 searches


def _fuzzy_match_tool(name: str, known: set[str]) -> str | None:
    """Try to match a garbled tool name to a known one.

    Handles typos like ``get_pricehistory`` (missing underscore),
    ``getPriceHistory`` (camelCase), or ``price_history`` (missing
    verb prefix). Returns the canonical tool name if confident, None
    otherwise. Uses a letter-only alphanumeric key so minor punctuation
    and case variations don't matter.
    """
    def key(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())
    target = key(name)
    if not target:
        return None
    # Exact hit after key normalization.
    by_key = {key(k): k for k in known}
    if target in by_key:
        return by_key[target]
    # Suffix match — "price_history" → "get_price_history".
    for k_norm, k_orig in by_key.items():
        if k_norm.endswith(target) or target.endswith(k_norm):
            if min(len(k_norm), len(target)) >= 6:
                return k_orig
    return None


_HTML_SCRIPT_RE = re.compile(
    r"<(script|style)[^>]*>.*?</\1>", flags=re.DOTALL | re.IGNORECASE
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
_HTML_NUMERIC_ENTITY_RE = re.compile(r"&#(x?)([0-9a-fA-F]+);")
_HTML_NAMED_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&apos;": "'",
    "&nbsp;": " ",
    "&ensp;": " ",
    "&emsp;": " ",
    "&thinsp;": " ",
    "&mdash;": "—",
    "&ndash;": "–",
    "&hellip;": "…",
    "&lsquo;": "‘",
    "&rsquo;": "’",
    "&ldquo;": "“",
    "&rdquo;": "”",
    "&bull;": "•",
    "&middot;": "·",
    "&copy;": "©",
    "&reg;": "®",
    "&trade;": "™",
    "&deg;": "°",
    "&times;": "×",
}


def _decode_numeric_entity(m: re.Match[str]) -> str:
    is_hex = m.group(1) == "x"
    raw = m.group(2)
    try:
        code = int(raw, 16 if is_hex else 10)
    except ValueError:
        return m.group(0)
    if code == 160:
        return " "  # nbsp
    try:
        return chr(code)
    except (ValueError, OverflowError):
        return m.group(0)


def _html_to_text(html: str) -> str:
    s = _HTML_SCRIPT_RE.sub("", html)
    s = re.sub(
        r"</?(p|div|br|tr|li|h[1-6]|hr|section|article|table)[^>]*>",
        "\n",
        s,
        flags=re.IGNORECASE,
    )
    s = _HTML_TAG_RE.sub("", s)
    for ent, rep in _HTML_NAMED_ENTITIES.items():
        s = s.replace(ent, rep)
    s = _HTML_NUMERIC_ENTITY_RE.sub(_decode_numeric_entity, s)
    s = _WHITESPACE_RE.sub(" ", s)
    s = _BLANK_LINES_RE.sub("\n\n", s)
    return s.strip()


# ---------- filing highlights ------------------------------------------------

_FILING_SECTIONS: list[tuple[str, re.Pattern[str]]] = [
    (
        "overview",
        re.compile(
            r"(Item\s+1\.\s+Business|Business\s+Overview|Overview|"
            r"About\s+(?:the\s+Company|Us))",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "risk_factors",
        re.compile(r"(Item\s+1A\.?\s+Risk\s+Factors|Risk\s+Factors)", flags=re.IGNORECASE),
    ),
    (
        "mdna",
        re.compile(
            r"(Item\s+7\.?\s+Management'?s\s+Discussion|"
            r"Management'?s\s+Discussion\s+and\s+Analysis|MD&A)",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "liquidity",
        re.compile(r"Liquidity\s+and\s+Capital\s+Resources", flags=re.IGNORECASE),
    ),
    (
        "revenue",
        re.compile(r"(Revenue|Net\s+Sales|Total\s+Revenues)", flags=re.IGNORECASE),
    ),
    (
        "results_of_operations",
        re.compile(r"Results\s+of\s+Operations", flags=re.IGNORECASE),
    ),
]

_8K_ITEM_RE = re.compile(
    r"Item\s+(\d+\.\d+)\s*[\.\-\u2013\u2014:]*\s*([A-Z][A-Za-z0-9 ,/'\-\(\)&]{3,120})",
)

# Dollar figures with magnitude words. Captures e.g. "$1.2 billion",
# "$432.5 million", "$10.5M", "$1,250,000".
_MONEY_RE = re.compile(
    r"\$\s?([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?)\s*"
    r"(billion|million|thousand|bn|mm|m|k)?",
    flags=re.IGNORECASE,
)

# Share-count / split patterns.
_SHARE_ACTION_RE = re.compile(
    r"(repurchased?|bought\s+back|issued|authorized|sold|redeemed)\s+"
    r"(?:up\s+to\s+)?(approximately\s+)?"
    r"([0-9][0-9,\.]*)\s*(million|billion|thousand|bn|mm|m|k)?\s+"
    r"(shares?|units?)",
    flags=re.IGNORECASE,
)

_PERCENT_RE = re.compile(
    r"(?:increased|decreased|grew|fell|rose|declined|up|down)\s+(?:by\s+)?"
    r"(?:approximately\s+)?([0-9]+(?:\.[0-9]+)?)\s?%",
    flags=re.IGNORECASE,
)


def _money_to_usd(raw: str, unit: str | None) -> float | None:
    try:
        value = float(raw.replace(",", ""))
    except ValueError:
        return None
    u = (unit or "").lower()
    if u in ("billion", "bn"):
        return value * 1_000_000_000
    if u in ("million", "mm", "m"):
        return value * 1_000_000
    if u in ("thousand", "k"):
        return value * 1_000
    return value


def _truncate_sentence(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars]
    last = max(cut.rfind(". "), cut.rfind(".\n"))
    if last > max_chars * 0.5:
        return cut[: last + 1].strip()
    return cut.strip() + "…"


_SENT_END_RE = re.compile(r"[.!?](?:\s|$)")


def _sentence_context(
    text: str, start: int, end: int, *, max_chars: int = 200
) -> str:
    """Extract a clean sentence-bounded context around a match range.

    Walks BACKWARD from ``start`` to the previous sentence terminator (or
    start of paragraph / buffer) and FORWARD from ``end`` to the next
    terminator, then trims to ``max_chars`` without cutting mid-word.
    """
    n = len(text)
    # Walk back up to 200 chars looking for a sentence/line boundary.
    look_back = max(0, start - 240)
    back_slice = text[look_back:start]
    # Find last ". " or "\n" in back slice — use that as our start.
    candidates = [
        back_slice.rfind(". "),
        back_slice.rfind(".\n"),
        back_slice.rfind("! "),
        back_slice.rfind("? "),
        back_slice.rfind("\n\n"),
    ]
    back_cut = max(candidates)
    if back_cut >= 0:
        begin = look_back + back_cut + 2  # skip ". " / "\n\n"
    else:
        # No sentence boundary — round to a word boundary so we don't
        # leave a half-word like "e years".
        begin = max(0, start - 120)
        space = text.find(" ", begin)
        if 0 <= space < start:
            begin = space + 1

    # Walk forward similarly.
    look_fwd = min(n, end + 240)
    fwd_slice = text[end:look_fwd]
    fwd_m = _SENT_END_RE.search(fwd_slice)
    if fwd_m is not None:
        finish = end + fwd_m.end()
    else:
        finish = min(n, end + 120)
        space = text.rfind(" ", end, finish)
        if 0 <= space > end:
            finish = space

    excerpt = text[begin:finish].strip().replace("\n", " ")
    # Collapse multi-space.
    excerpt = re.sub(r"\s+", " ", excerpt)
    if len(excerpt) <= max_chars:
        return excerpt
    # Trim to word boundary, not mid-word, if still too long.
    cut = excerpt[: max_chars]
    space = cut.rfind(" ")
    if space > max_chars * 0.5:
        return cut[:space].rstrip(",;:.") + "…"
    return cut + "…"


def _extract_section_excerpt(
    text: str, pattern: re.Pattern[str], *, max_chars: int = 1200
) -> str | None:
    """Return the excerpt immediately following the first heading match."""
    m = pattern.search(text)
    if m is None:
        return None
    start = m.end()
    # Take up to ~3000 chars then trim to a sentence boundary.
    raw = text[start : start + 3000].strip()
    if not raw:
        return None
    # Drop the heading line if it bled into the excerpt.
    if raw.startswith((":", "-", "—", "–")):
        raw = raw.lstrip(":—–- ").strip()
    return _truncate_sentence(raw, max_chars)


def _extract_filing_highlights(text: str, url: str) -> dict[str, Any]:
    """Distill a filing's readable body into a compact highlight payload.

    Extracts in priority order:
      1. 8-K Items (short form, directly actionable)
      2. Named section excerpts (risk factors, MD&A, overview)
      3. Notable dollar figures (by magnitude)
      4. Share-count actions (buybacks, issuances)
      5. Percent changes (guidance, YoY moves)
    """
    is_8k = "8-k" in url.lower() or re.search(r"^\s*Item\s+\d+\.\d+", text, re.IGNORECASE | re.MULTILINE) is not None

    sections: dict[str, str] = {}
    for key, pat in _FILING_SECTIONS:
        excerpt = _extract_section_excerpt(text, pat, max_chars=900)
        if excerpt:
            sections[key] = excerpt
        if len(sections) >= 4:
            break

    # 8-K items — first ~200 chars after each Item heading.
    item_hits: list[dict[str, str]] = []
    if is_8k:
        for m in _8K_ITEM_RE.finditer(text):
            tail = text[m.end() : m.end() + 600].strip()
            tail = _truncate_sentence(tail, 400)
            item_hits.append(
                {
                    "item": m.group(1),
                    "title": m.group(2).strip().rstrip(","),
                    "excerpt": tail,
                }
            )
            if len(item_hits) >= 8:
                break

    # Notable money figures — dedupe by rounded magnitude + context.
    money: list[dict[str, Any]] = []
    seen_money: set[tuple[float, str]] = set()
    for m in _MONEY_RE.finditer(text):
        usd = _money_to_usd(m.group(1), m.group(2))
        if usd is None or usd < 10_000:
            continue
        context = _sentence_context(text, m.start(), m.end(), max_chars=220)
        if len(context) < 30:
            continue
        magnitude_bucket = round(usd, -max(0, int(len(str(int(usd))) - 2)))
        key = (magnitude_bucket, context[:40])
        if key in seen_money:
            continue
        seen_money.add(key)
        money.append(
            {
                "text": m.group(0),
                "usd": usd,
                "context": context,
            }
        )
        if len(money) >= 10:
            break
    money.sort(key=lambda x: x["usd"], reverse=True)
    money = money[:8]

    # Share actions.
    shares: list[dict[str, Any]] = []
    for m in _SHARE_ACTION_RE.finditer(text):
        action = m.group(1).lower()
        count_raw = m.group(3)
        unit = m.group(4)
        try:
            count = float(count_raw.replace(",", ""))
        except ValueError:
            continue
        multiplier = 1
        u = (unit or "").lower()
        if u in ("billion", "bn"):
            multiplier = 1_000_000_000
        elif u in ("million", "mm", "m"):
            multiplier = 1_000_000
        elif u in ("thousand", "k"):
            multiplier = 1_000
        total = count * multiplier
        context = _sentence_context(text, m.start(), m.end(), max_chars=220)
        if len(context) < 30:
            continue
        shares.append(
            {
                "action": action,
                "count": total,
                "raw": m.group(0),
                "context": context,
            }
        )
        if len(shares) >= 6:
            break

    # Percent changes.
    pct_hits: list[dict[str, Any]] = []
    for m in _PERCENT_RE.finditer(text):
        context = _sentence_context(text, m.start(), m.end(), max_chars=200)
        if len(context) < 30:
            continue
        pct_hits.append(
            {
                "pct": float(m.group(1)),
                "context": context,
            }
        )
        if len(pct_hits) >= 8:
            break

    return {
        "is_8k": is_8k,
        "sections": sections,
        "items_8k": item_hits,
        "money": money,
        "share_actions": shares,
        "percent_moves": pct_hits,
    }


def _summarize_insider_rows(
    symbol: str, rows: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build a rich summary from raw Form-4 rows.

    Returns a payload with:
      - ``summary``: counts, net share flow, net USD flow, ranges
      - ``windows``: 30d / 90d / 365d rollups
      - ``top_buyers`` / ``top_sellers``: per-insider aggregates
      - ``notable``: 5 largest buys + 5 largest sells by USD
      - ``rows``: original Finnhub rows (passed through for the table)
    """
    def _code(r: dict[str, Any]) -> str:
        return (r.get("transactionCode") or "").upper()

    def _is_buy(r: dict[str, Any]) -> bool:
        c = _code(r)
        # P = open-market purchase. A / M (award, option exercise) aren't
        # directional — exclude from buy count.
        if c.startswith("P"):
            return True
        # Fall back to a positive share change with no code (some feeds
        # emit code="") only if price > 0, else it's a grant.
        if not c and (r.get("change") or 0) > 0 and (r.get("transactionPrice") or 0) > 0:
            return True
        return False

    def _is_sell(r: dict[str, Any]) -> bool:
        c = _code(r)
        return c.startswith("S") or c == "F"  # F = tax-withholding sale

    def _usd(r: dict[str, Any]) -> float:
        price = float(r.get("transactionPrice") or 0.0)
        change = float(r.get("change") or 0.0)
        return price * change

    def _parse_date(r: dict[str, Any]) -> datetime | None:
        for key in ("transactionDate", "filingDate"):
            raw = r.get(key)
            if isinstance(raw, str) and raw:
                try:
                    return datetime.fromisoformat(raw).replace(tzinfo=UTC)
                except Exception:
                    continue
        return None

    now = datetime.now(UTC)

    buy_count = 0
    sell_count = 0
    net_shares = 0.0
    net_usd = 0.0
    buy_shares = 0.0
    sell_shares = 0.0
    buy_usd = 0.0
    sell_usd = 0.0
    first_date: str | None = None
    last_date: str | None = None

    windows = {
        "d30": {"buys": 0, "sells": 0, "net_shares": 0.0, "net_usd": 0.0},
        "d90": {"buys": 0, "sells": 0, "net_shares": 0.0, "net_usd": 0.0},
        "d365": {"buys": 0, "sells": 0, "net_shares": 0.0, "net_usd": 0.0},
    }

    per_insider: dict[str, dict[str, Any]] = {}
    sized: list[tuple[float, dict[str, Any]]] = []  # (signed_usd, compact row)

    for r in rows:
        buy = _is_buy(r)
        sell = _is_sell(r)
        change = float(r.get("change") or 0.0)
        usd = _usd(r)
        date = _parse_date(r)
        date_str = r.get("transactionDate") or r.get("filingDate")
        if isinstance(date_str, str):
            if first_date is None or date_str < first_date:
                first_date = date_str
            if last_date is None or date_str > last_date:
                last_date = date_str

        if buy:
            buy_count += 1
            buy_shares += change
            buy_usd += usd
        if sell:
            sell_count += 1
            sell_shares += abs(change)
            sell_usd += abs(usd)

        signed_shares = change if buy else (-abs(change) if sell else 0.0)
        signed_usd = usd if buy else (-abs(usd) if sell else 0.0)
        net_shares += signed_shares
        net_usd += signed_usd

        if date is not None and (buy or sell):
            age = (now - date).days
            for key, cutoff in (("d30", 30), ("d90", 90), ("d365", 365)):
                if age <= cutoff:
                    w = windows[key]
                    if buy:
                        w["buys"] += 1
                    if sell:
                        w["sells"] += 1
                    w["net_shares"] += signed_shares
                    w["net_usd"] += signed_usd

        name = (r.get("name") or "").strip() or "—"
        title = r.get("position") or r.get("title") or ""
        agg = per_insider.setdefault(
            name,
            {
                "name": name,
                "title": title,
                "buys": 0,
                "sells": 0,
                "net_shares": 0.0,
                "net_usd": 0.0,
                "last_date": None,
            },
        )
        if title and not agg["title"]:
            agg["title"] = title
        if buy:
            agg["buys"] += 1
        if sell:
            agg["sells"] += 1
        agg["net_shares"] += signed_shares
        agg["net_usd"] += signed_usd
        if isinstance(date_str, str):
            if agg["last_date"] is None or date_str > agg["last_date"]:
                agg["last_date"] = date_str

        if buy or sell:
            sized.append(
                (
                    signed_usd,
                    {
                        "name": name,
                        "date": date_str,
                        "code": _code(r),
                        "shares": change,
                        "price": r.get("transactionPrice"),
                        "usd": signed_usd,
                    },
                )
            )

    # Top buyers / sellers by net USD, capped at 10 each.
    insiders = list(per_insider.values())
    top_buyers = sorted(
        [i for i in insiders if i["net_usd"] > 0],
        key=lambda x: x["net_usd"],
        reverse=True,
    )[:10]
    top_sellers = sorted(
        [i for i in insiders if i["net_usd"] < 0],
        key=lambda x: x["net_usd"],
    )[:10]

    notable_buys = sorted(
        [t for t in sized if t[0] > 0], key=lambda x: x[0], reverse=True
    )[:5]
    notable_sells = sorted(
        [t for t in sized if t[0] < 0], key=lambda x: x[0]
    )[:5]

    summary = {
        "total_txns": len(rows),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "buy_shares": buy_shares,
        "sell_shares": sell_shares,
        "buy_usd": buy_usd,
        "sell_usd": sell_usd,
        "net_shares": net_shares,
        "net_usd": net_usd,
        "unique_insiders": len(per_insider),
        "date_range": {"from": first_date, "to": last_date},
    }

    return {
        "symbol": symbol,
        "summary": summary,
        "windows": windows,
        "top_buyers": top_buyers,
        "top_sellers": top_sellers,
        "notable": {
            "largest_buys": [x[1] for x in notable_buys],
            "largest_sells": [x[1] for x in notable_sells],
        },
        "rows": rows,
    }


def _summarize_ownership(
    symbol: str,
    inst_rows: list[dict[str, Any]],
    fund_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a compact ownership snapshot.

    Finnhub's ``ownership`` and ``fund-ownership`` rows carry ``name``,
    ``share`` (shares held), ``change`` (vs prior filing), ``filingDate``,
    and an implied market value we compute off the most recent close if
    the payload doesn't include one (it usually doesn't).
    """
    def _compact(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": r.get("name"),
            "shares": r.get("share"),
            "share_change": r.get("change"),
            "filed": r.get("filingDate") or r.get("reportDate"),
            "percent": r.get("percentage") or r.get("portfolioPercent"),
        }

    inst = [_compact(r) for r in inst_rows][:10]
    funds = [_compact(r) for r in fund_rows][:10]
    inst_total_shares = sum(
        float(r.get("shares") or 0) for r in inst if r.get("shares") is not None
    )
    fund_total_shares = sum(
        float(r.get("shares") or 0) for r in funds if r.get("shares") is not None
    )
    return {
        "symbol": symbol,
        "institutions": inst,
        "funds": funds,
        "summary": {
            "institutions_count": len(inst),
            "funds_count": len(funds),
            "top10_inst_shares": inst_total_shares,
            "top10_fund_shares": fund_total_shares,
        },
    }


def _sma(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / float(window)


def _ema(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    k = 2.0 / (window + 1.0)
    ema = sum(values[:window]) / float(window)
    for v in values[window:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def _rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(-period, 0):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _compute_technicals(
    closes: list[float], highs: list[float], lows: list[float]
) -> dict[str, Any]:
    last = closes[-1]
    window_52w = closes[-252:] if len(closes) >= 252 else closes
    hi_52w = max(highs[-252:]) if len(highs) >= 1 else None
    lo_52w = min(lows[-252:]) if len(lows) >= 1 else None
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    macd = (ema12 - ema26) if (ema12 is not None and ema26 is not None) else None
    macd_series: list[float] = []
    for i in range(26, len(closes) + 1):
        e12 = _ema(closes[:i], 12)
        e26 = _ema(closes[:i], 26)
        if e12 is not None and e26 is not None:
            macd_series.append(e12 - e26)
    macd_signal = _ema(macd_series, 9) if len(macd_series) >= 9 else None
    atr = None
    if len(closes) >= 15:
        trs: list[float] = []
        for i in range(1, len(closes)):
            hi = highs[i]
            lo = lows[i]
            prev_close = closes[i - 1]
            tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
            trs.append(tr)
        atr = sum(trs[-14:]) / 14.0
    return {
        "last_close": last,
        "sma_20": _sma(closes, 20),
        "sma_50": _sma(closes, 50),
        "sma_200": _sma(closes, 200),
        "ema_12": ema12,
        "ema_26": ema26,
        "rsi14": _rsi(closes, 14),
        "macd": macd,
        "macd_signal": macd_signal,
        "macd_hist": (macd - macd_signal)
        if (macd is not None and macd_signal is not None)
        else None,
        "atr14": atr,
        "high_52w": hi_52w,
        "low_52w": lo_52w,
        "pct_from_52w_high": ((last - hi_52w) / hi_52w * 100.0) if hi_52w else None,
        "pct_from_52w_low": ((last - lo_52w) / lo_52w * 100.0) if lo_52w else None,
        "window_bars": len(window_52w),
    }


# ---------- Tool schemas -----------------------------------------------------

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the public web. Use for up-to-date news, catalysts, "
            "filings, analyst commentary. Keep queries specific."
        ),
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 8},
            },
        },
    },
}

FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "Fetch a URL's readable text (stripped of HTML).",
        "parameters": {
            "type": "object",
            "required": ["url"],
            "properties": {"url": {"type": "string"}},
        },
    },
}

GET_QUOTE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_quote",
        "description": "Current quote + intraday OHLC for a ticker via Finnhub.",
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {"symbol": {"type": "string"}},
        },
    },
}

GET_COMPANY_NEWS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_company_news",
        "description": "Recent headlines for a ticker (last few days).",
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
        },
    },
}

GET_PRICE_HISTORY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_price_history",
        "description": (
            "Daily OHLCV bars for a ticker over the last N trading days "
            "(default 260 ≈ 1Y, max 400). Use for trend context, drawdown, "
            "volatility. The UI can toggle 1M/3M/6M/1Y over whatever you return."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {
                "symbol": {"type": "string"},
                "days": {"type": "integer", "minimum": 5, "maximum": 400},
            },
        },
    },
}

GET_RECENT_TRADES_TOOL = {
    "type": "function",
    "function": {
        "name": "get_recent_trades",
        "description": (
            "Our own trade history. Optionally filter by symbol. Returns "
            "the last N trades with P&L, entry/exit, hold time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
}

GET_RECENT_DECISIONS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_recent_decisions",
        "description": (
            "Our own AI decision log. Optionally filter by symbol. Returns "
            "what the decision agent proposed, whether it was approved, "
            "and its rationale."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
}

SEARCH_TICKERS_TOOL = {
    "type": "function",
    "function": {
        "name": "search_tickers",
        "description": (
            "Resolve a company NAME (or fuzzy query) to a US stock ticker. "
            "Returns up to 10 candidate matches with symbol + description. "
            "ALWAYS call this first when the user gives a company name "
            "(e.g. 'Katapult', 'Palantir', 'Rivian') instead of guessing "
            "the ticker. Do NOT pass tickers here — pass the human name."
        ),
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Company name or fuzzy description. Examples: "
                        "'Katapult Holdings', 'Rivian', 'the EV startup "
                        "that competes with Tesla'."
                    ),
                },
            },
        },
    },
}

GET_COMPANY_PROFILE_TOOL = {
    "type": "function",
    "function": {
        "name": "get_company_profile",
        "description": (
            "Company fundamentals for a ticker: full name, industry, "
            "exchange, country, IPO date, market cap, shares outstanding, "
            "website, logo. Call this FIRST for any unfamiliar ticker. "
            "If you only have a company name, call `search_tickers` first "
            "to resolve it — do NOT guess a ticker."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {"symbol": {"type": "string"}},
        },
    },
}

GET_PEERS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_peers",
        "description": (
            "List peer/competitor tickers for a symbol, with each peer's "
            "industry label attached so mis-classified peers are easy to "
            "spot. Drops industry-mismatched peers silently and (when "
            "industry is known) runs a sector-scoped web search for true "
            "competitors."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {"symbol": {"type": "string"}},
        },
    },
}

GET_BASIC_FINANCIALS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_basic_financials",
        "description": (
            "Key metrics & ratios: 52-week high/low, P/E, beta, margins, "
            "returns, volume averages. Use for fundamental valuation and "
            "technical range context."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {"symbol": {"type": "string"}},
        },
    },
}

GET_SEC_FILINGS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_sec_filings",
        "description": (
            "Recent SEC filings for a US-listed ticker (10-K, 10-Q, 8-K, "
            "S-1, S-4, etc.). Returns filing type, date, and document URL."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                "form_type": {"type": "string"},
            },
        },
    },
}

SEARCH_SEC_TOOL = {
    "type": "function",
    "function": {
        "name": "search_sec",
        "description": "Full-text search across EDGAR filings.",
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "form_type": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
        },
    },
}

READ_FILING_TOOL = {
    "type": "function",
    "function": {
        "name": "read_filing",
        "description": (
            "Fetch and distill an SEC filing (pass the URL from "
            "get_sec_filings). Returns extracted highlights: key sections "
            "(risk factors, MD&A, overview), notable dollar figures, "
            "share-count changes, and headline items like 8-K Item "
            "numbers. Falls back to truncated raw text if extraction "
            "finds nothing. Use include_text=true to force the raw body."
        ),
        "parameters": {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {"type": "string"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 2000,
                    "maximum": 40000,
                    "description": "Cap on raw text if returned (default 15k).",
                },
                "include_text": {
                    "type": "boolean",
                    "description": (
                        "If true, include the raw filing body alongside "
                        "the highlights. Default false — highlights only."
                    ),
                },
            },
        },
    },
}

GET_ANALYST_RATINGS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_analyst_ratings",
        "description": (
            "Analyst consensus: buy/hold/sell counts over recent months "
            "PLUS median/high/low price target."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {"symbol": {"type": "string"}},
        },
    },
}

GET_EARNINGS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_earnings",
        "description": (
            "Upcoming earnings date plus historical beats/misses."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {"symbol": {"type": "string"}},
        },
    },
}

GET_INSIDER_TXNS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_insider_transactions",
        "description": (
            "Recent Form-4 insider buys/sells, aggregated: 30d/90d/365d "
            "net flows, per-insider net rollups (top buyers + sellers), "
            "notable largest transactions, plus the raw rows."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {
                "symbol": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
        },
    },
}

GET_OWNERSHIP_TOOL = {
    "type": "function",
    "function": {
        "name": "get_ownership",
        "description": (
            "Top institutional and mutual-fund shareholders from the most "
            "recent 13F filings: name, shares held, share-count change "
            "vs the prior filing, and estimated USD value at the latest "
            "close. Use to gauge smart-money conviction and crowding."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {
                "symbol": {"type": "string"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 25,
                    "description": "Max holders per category (default 10).",
                },
            },
        },
    },
}

GET_INTRADAY_TOOL = {
    "type": "function",
    "function": {
        "name": "get_intraday_history",
        "description": (
            "Intraday bars (5m/15m/1h) for the last N sessions."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {
                "symbol": {"type": "string"},
                "timeframe": {
                    "type": "string",
                    "enum": ["5Min", "15Min", "1Hour"],
                },
                "sessions": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
        },
    },
}

GET_TECHNICALS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_technicals",
        "description": (
            "Computed technical indicators from daily bars: SMA-20/50/200, "
            "RSI-14, MACD (12/26/9), ATR-14, distance-from-52w-range."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {
                "symbol": {"type": "string"},
                "days": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 400,
                },
            },
        },
    },
}

GET_MARKET_CONTEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_market_context",
        "description": (
            "One-shot macro snapshot: SPY, QQQ, IWM, VIX, DXY, TLT, GLD."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

DEEP_DIVE_TOOL = {
    "type": "function",
    "function": {
        "name": "deep_dive",
        "description": (
            "ONE-SHOT comprehensive dossier for a ticker. Fans out in "
            "parallel and returns: profile (name, industry, market cap), "
            "latest quote, basic financials, analyst consensus + target, "
            "next earnings, insider buy/sell count, top 5 news headlines, "
            "top 5 recent SEC filings, Finnhub peers enriched with their "
            "industry, and — when industry is known — a sector-scoped web "
            "search for real competitors and a news sweep keyed on the "
            "company name + sector. Prefer this as the opening call for "
            "any unfamiliar ticker; fall back to individual tools only "
            "when you need more depth on one slice."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol"],
            "properties": {
                "symbol": {"type": "string"},
                "include_filings": {
                    "type": "boolean",
                    "description": "Pull recent SEC filings (default true).",
                },
                "include_web": {
                    "type": "boolean",
                    "description": (
                        "Run the sector-scoped web searches (default true; "
                        "skip if web_search is known-unavailable)."
                    ),
                },
            },
        },
    },
}

ALL_TOOLS: list[dict[str, Any]] = [
    WEB_SEARCH_TOOL,
    FETCH_URL_TOOL,
    SEARCH_TICKERS_TOOL,
    GET_QUOTE_TOOL,
    GET_COMPANY_PROFILE_TOOL,
    GET_COMPANY_NEWS_TOOL,
    GET_PEERS_TOOL,
    GET_BASIC_FINANCIALS_TOOL,
    GET_ANALYST_RATINGS_TOOL,
    GET_EARNINGS_TOOL,
    GET_INSIDER_TXNS_TOOL,
    GET_OWNERSHIP_TOOL,
    GET_SEC_FILINGS_TOOL,
    SEARCH_SEC_TOOL,
    READ_FILING_TOOL,
    GET_PRICE_HISTORY_TOOL,
    GET_INTRADAY_TOOL,
    GET_TECHNICALS_TOOL,
    GET_MARKET_CONTEXT_TOOL,
    GET_RECENT_TRADES_TOOL,
    GET_RECENT_DECISIONS_TOOL,
    DEEP_DIVE_TOOL,
]

_TOOL_BY_NAME = {t["function"]["name"]: t for t in ALL_TOOLS}


# Tool names whose output is stable within a single user turn. Quotes,
# intraday bars, and market context move second-by-second so we do NOT
# cache them even within a single turn.
CACHEABLE_TOOLS = frozenset({
    "search_tickers",
    "get_company_profile",
    "get_company_news",
    "get_peers",
    "get_basic_financials",
    "get_analyst_ratings",
    "get_earnings",
    "get_insider_transactions",
    "get_ownership",
    "get_sec_filings",
    "search_sec",
    "read_filing",
    "get_price_history",
    "get_technicals",
    "get_recent_trades",
    "get_recent_decisions",
    "web_search",
    "fetch_url",
    "deep_dive",
})


def is_cacheable(tool_name: str) -> bool:
    return tool_name in CACHEABLE_TOOLS


def cache_signature(tool_name: str, args: dict[str, Any]) -> str:
    """Canonical signature — stable across arg ordering."""
    return tool_name + "::" + json.dumps(args or {}, sort_keys=True, default=str)


def tool_names() -> list[str]:
    return [t["function"]["name"] for t in ALL_TOOLS]


class ResearchToolbelt:
    """Shared research tool belt used by every agent that needs data.

    Construct once per process with live dependencies. Agents ask for
    whichever subset of schemas they want via ``schemas(include=...)``
    and call ``dispatch(name, args)`` to execute.
    """

    def __init__(
        self,
        *,
        finnhub: FinnhubClient | None = None,
        search: WebSearchClient | None = None,
        fetch: UrlFetchClient | None = None,
        alpaca_api_key: str | None = None,
        alpaca_api_secret: str | None = None,
        alpaca_data_url: str = "https://data.alpaca.markets",
        session_factory: async_sessionmaker | None = None,
    ) -> None:
        self._finnhub = finnhub
        self._search = search or WebSearchClient()
        self._fetch = fetch or UrlFetchClient()
        self._alpaca_api_key = alpaca_api_key
        self._alpaca_api_secret = alpaca_api_secret
        self._alpaca_data_url = alpaca_data_url.rstrip("/")
        self._session_factory = session_factory

    # ---------- schema selection --------------------------------------------

    def schemas(
        self,
        *,
        include: list[str] | set[str] | None = None,
        exclude: list[str] | set[str] | None = None,
    ) -> list[dict[str, Any]]:
        if include is not None:
            wanted = [t for t in ALL_TOOLS if t["function"]["name"] in include]
        else:
            wanted = list(ALL_TOOLS)
        if exclude:
            wanted = [t for t in wanted if t["function"]["name"] not in exclude]
        return wanted

    @property
    def all_schemas(self) -> list[dict[str, Any]]:
        return list(ALL_TOOLS)

    # ---------- dispatch -----------------------------------------------------

    async def dispatch(
        self, name: str, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        """Run one tool. Returns (full_json_text, preview, structured_payload)."""
        handlers = {
            "web_search": self._tool_web_search,
            "fetch_url": self._tool_fetch_url,
            "search_tickers": self._tool_search_tickers,
            "get_quote": self._tool_get_quote,
            "get_company_news": self._tool_get_company_news,
            "get_company_profile": self._tool_get_company_profile,
            "get_peers": self._tool_get_peers,
            "get_basic_financials": self._tool_get_basic_financials,
            "get_sec_filings": self._tool_get_sec_filings,
            "search_sec": self._tool_search_sec,
            "read_filing": self._tool_read_filing,
            "get_analyst_ratings": self._tool_get_analyst_ratings,
            "get_earnings": self._tool_get_earnings,
            "get_insider_transactions": self._tool_get_insider_transactions,
            "get_ownership": self._tool_get_ownership,
            "get_price_history": self._tool_get_price_history,
            "get_intraday_history": self._tool_get_intraday_history,
            "get_technicals": self._tool_get_technicals,
            "get_market_context": self._tool_get_market_context,
            "get_recent_trades": self._tool_get_recent_trades,
            "get_recent_decisions": self._tool_get_recent_decisions,
            "deep_dive": self._tool_deep_dive,
        }
        try:
            return await self._dispatch_inner(name, args, handlers)
        except Exception as exc:
            # Last-resort safety net — no tool call, however malformed,
            # should raise out of dispatch. The researcher loop already
            # catches exceptions, but returning a structured error gives
            # the model actionable context instead of a bare string.
            logger.exception("dispatch safety net caught %s", name)
            payload = {
                "tool": name,
                "error": f"{type(exc).__name__}: {exc}",
            }
            return (json.dumps(payload), payload["error"], payload)

    async def _dispatch_inner(
        self,
        name: str,
        args: Any,
        handlers: dict[str, Any],
    ) -> tuple[str, str, dict[str, Any]]:
        # Coerce args first — handles double-encoded JSON, bare strings,
        # `{"input": {...}}` wrappers. Alias normalization runs on top.
        args = _normalize_tool_args(args)

        # Missing / literal-"unknown" / literal-"function" names happen when
        # the provider drops the function name or the model emits a
        # name-less call. Infer from args shape before giving up.
        raw_name = (name or "").strip()
        if not raw_name or raw_name.lower() in {
            "unknown", "function", "functions", "tool", "call", "none", "null",
        }:
            inferred = _infer_tool_from_args(args)
            if inferred and inferred in handlers:
                logger.info(
                    "recovering nameless tool call → %s (from args %s)",
                    inferred, sorted(args.keys()),
                )
                return await handlers[inferred](args)

        # Be lenient about name shape — some providers emit
        # "functions.deep_dive", "#deep_dive", "deep-dive", etc. Normalize
        # to the canonical snake_case name the handler map uses.
        canonical = _canonical_tool_name(name)
        handler = handlers.get(canonical) or handlers.get(name)

        # Fuzzy match for typos (camelCase, missing underscore, missing
        # "get_" prefix, etc.). Only runs if exact lookup failed.
        if handler is None:
            fuzzy = _fuzzy_match_tool(canonical or name, set(handlers.keys()))
            if fuzzy:
                logger.info("fuzzy tool-name match: %r → %s", name, fuzzy)
                handler = handlers[fuzzy]

        if handler is None:
            # Recover when the model used a ticker symbol or company name
            # as the tool name (e.g. name="AAPL" args={"days": 120} really
            # meant get_price_history). Infer the intended tool from the
            # args shape and inject the ticker as `symbol`.
            raw = (name or "").strip()
            if _TICKER_LIKE_RE.match(raw.upper()):
                inferred = _infer_tool_from_args(args)
                if inferred and inferred in handlers:
                    logger.info(
                        "recovering unknown tool %r → %s (ticker-as-name)",
                        name, inferred,
                    )
                    args = {**args, "symbol": raw.upper()}
                    return await handlers[inferred](args)

            # Comparison phrasing — "Kodiak AI vs Aurora Innovation",
            # "X compared to Y" — the model invented a comparison tool.
            # Auto-recover by running search_tickers on each side so the
            # next round has real candidate tickers instead of burning
            # budget on more hallucinated calls.
            comp_parts = _split_comparison(raw)
            if comp_parts and "search_tickers" in handlers:
                logger.info(
                    "recovering unknown tool %r → search_tickers x%d",
                    name, len(comp_parts),
                )
                combined: dict[str, Any] = {
                    "note": (
                        f"Recovered from invalid tool call {name!r}. "
                        "Ran search_tickers on each side; pick a symbol "
                        "then call real tools with it."
                    ),
                    "sides": [],
                }
                for part in comp_parts:
                    _, _, payload = await handlers["search_tickers"](
                        {"query": part}
                    )
                    combined["sides"].append(payload)
                preview = " · ".join(
                    f"{side.get('query', '?')}→"
                    f"{side.get('best_symbol') or 'no match'}"
                    for side in combined["sides"]
                )
                return (json.dumps(combined), preview, combined)

            looks_like_company = (
                " " in raw or any(
                    tok in raw.lower()
                    for tok in (" inc", " corp", " ltd", " llc", " plc")
                )
            )
            if looks_like_company:
                hint = (
                    f" — looks like a company name. Call `search_tickers` "
                    f"with {{\"query\": {json.dumps(raw)}}} to resolve it "
                    "to a ticker, then use that ticker with real tools."
                )
            elif _TICKER_LIKE_RE.match(raw.upper()):
                hint = (
                    f" — looks like a ticker. Use a real tool name (e.g. "
                    f"`get_price_history`) with {{\"symbol\": \"{raw.upper()}\"}}."
                )
            else:
                # List the closest-looking names to help the model self-correct.
                known = sorted(handlers.keys())
                hint = f" — available tools: {', '.join(known[:8])}, …"
            err = {"error": f"unknown tool: {name}{hint}"}
            return (json.dumps(err), err["error"], err)
        return await handler(args)

    # ---------- web ----------------------------------------------------------

    async def _tool_web_search(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        query = str(args.get("query") or "").strip()
        top_k = max(1, min(int(args.get("top_k") or 6), 8))
        if not query:
            payload = {"results": [], "error": "empty query"}
            return (json.dumps(payload), "(empty query)", payload)
        results = await self._search.search(query, top_k=top_k)
        if not results:
            payload = {
                "query": query,
                "results": [],
                "error": (
                    "web_search unavailable (network block or zero results). "
                    "Use get_company_profile, get_sec_filings, or "
                    "get_company_news instead — they're more reliable."
                ),
            }
            preview = f"web_search unavailable for {query!r}"
            return (json.dumps(payload), preview, payload)
        payload = {
            "query": query,
            "results": [
                {"title": r.title, "url": r.url, "snippet": r.snippet}
                for r in results
            ],
        }
        preview = f"{len(results)} results for {query!r}"
        return (json.dumps(payload), preview, payload)

    async def _tool_fetch_url(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        url = str(args.get("url") or "").strip()
        if not url:
            payload = {"error": "empty url"}
            return (json.dumps(payload), "(empty url)", payload)
        # For SEC archive URLs, try dash-stripped accession forms too —
        # the LLM often builds URLs with dashes that EDGAR 404s on.
        for cand in _sec_url_candidates(url):
            result = await self._fetch.fetch(cand)
            if result is not None:
                payload = {
                    "url": result.url,
                    "title": result.title,
                    "text": result.text,
                    "truncated": result.truncated,
                }
                preview = (
                    f"fetched {result.title or cand} ({len(result.text)} chars)"
                )
                return (json.dumps(payload), preview, payload)
        payload = {"url": url, "error": "fetch_failed"}
        return (json.dumps(payload), f"fetch failed: {url}", payload)

    # ---------- Finnhub: market data ----------------------------------------

    async def _tool_get_quote(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        if not symbol or self._finnhub is None:
            payload = {"error": "finnhub unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        q = await self._finnhub.quote(symbol)
        if q is None:
            payload = {"symbol": symbol, "error": "no_quote"}
            return (json.dumps(payload), f"no quote for {symbol}", payload)
        payload = q.to_dict()
        preview = f"{symbol} ${q.current:.2f} ({q.change_pct:+.2f}% today)"
        return (json.dumps(payload), preview, payload)

    async def _tool_get_company_news(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        limit = max(1, min(int(args.get("limit") or 8), 20))
        if not symbol or self._finnhub is None:
            payload = {"error": "finnhub unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        items = await self._finnhub.company_news(symbol, limit=limit)
        payload = {
            "symbol": symbol,
            "items": [n.to_dict() for n in items],
        }
        preview = f"{len(items)} headlines for {symbol}"
        return (json.dumps(payload), preview, payload)

    async def _tool_search_tickers(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        query = str(args.get("query") or "").strip()
        if not query:
            payload = {"error": "empty query"}
            return (json.dumps(payload), payload["error"], payload)
        if self._finnhub is None:
            payload = {"error": "finnhub unavailable"}
            return (json.dumps(payload), payload["error"], payload)
        matches = await self._finnhub.symbol_search(query, limit=10)
        if not matches:
            payload = {"query": query, "matches": [], "error": "no matches"}
            return (
                json.dumps(payload),
                f"no tickers matched '{query}'",
                payload,
            )
        # Prefer the shortest / most exact match as the primary candidate:
        # Finnhub returns e.g. PLTR for "Palantir" but also buries it among
        # warrants and foreign listings.
        q_lower = query.lower()

        def score(m: dict[str, Any]) -> tuple[int, int]:
            desc = (m.get("description") or "").lower()
            sym = (m.get("symbol") or "").lower()
            # Lower score = better. Exact-name match beats substring.
            exact = 0 if desc == q_lower else 1
            starts = 0 if desc.startswith(q_lower) else 1
            return (exact, starts + len(sym))

        ranked = sorted(matches, key=score)
        best = ranked[0]
        payload = {
            "query": query,
            "matches": ranked,
            "best_symbol": best.get("symbol"),
            "best_description": best.get("description"),
        }
        preview = (
            f"{query!r} → {best.get('symbol')} ({best.get('description')})"
            + (f" +{len(ranked) - 1} more" if len(ranked) > 1 else "")
        )
        return (json.dumps(payload), preview, payload)

    async def _tool_get_company_profile(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        if not symbol or self._finnhub is None:
            # Fall back to SEC ticker map if Finnhub is down entirely.
            sec_hit = await _sec_ticker_lookup(symbol) if symbol else None
            if sec_hit:
                payload = {"symbol": symbol, **sec_hit, "source": "sec"}
                return (
                    json.dumps(payload),
                    f"{symbol}: {sec_hit.get('name')} (SEC)",
                    payload,
                )
            payload = {"error": "finnhub unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        profile = await self._finnhub.company_profile(symbol)
        if not profile:
            sec_hit = await _sec_ticker_lookup(symbol)
            if sec_hit:
                payload = {"symbol": symbol, **sec_hit, "source": "sec"}
                preview = f"{symbol}: {sec_hit.get('name')} (SEC)"
                return (json.dumps(payload), preview, payload)
            payload = {"symbol": symbol, "error": "no profile"}
            return (json.dumps(payload), f"no profile for {symbol}", payload)
        preview = (
            f"{profile.get('ticker', symbol)}: {profile.get('name', '?')} "
            f"({profile.get('finnhubIndustry', 'n/a')}, "
            f"{profile.get('exchange', 'n/a')})"
        )
        return (json.dumps(profile), preview, profile)

    async def _tool_get_peers(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        if not symbol or self._finnhub is None:
            payload = {"error": "finnhub unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        peers, subject_profile = await asyncio.gather(
            self._finnhub.peers(symbol),
            self._finnhub.company_profile(symbol),
        )
        subject_industry = (
            (subject_profile or {}).get("finnhubIndustry")
            if subject_profile
            else None
        )
        subject_name = (subject_profile or {}).get("name") if subject_profile else None
        subject_mcap = (
            float((subject_profile or {}).get("marketCapitalization") or 0.0)
            if subject_profile
            else 0.0
        )
        top = [t for t in peers if t.upper() != symbol.upper()][:12]
        profile_coros = [self._finnhub.company_profile(t) for t in top]
        peer_profiles = await asyncio.gather(*profile_coros, return_exceptions=True)
        detailed: list[dict[str, Any]] = []
        recommended: list[dict[str, Any]] = []
        weak: list[dict[str, Any]] = []
        for t, prof in zip(top, peer_profiles, strict=False):
            row: dict[str, Any] = {"symbol": t}
            industry = None
            mcap = 0.0
            if isinstance(prof, dict):
                industry = prof.get("finnhubIndustry")
                try:
                    mcap = float(prof.get("marketCapitalization") or 0.0)
                except (TypeError, ValueError):
                    mcap = 0.0
                row.update(
                    {
                        "name": prof.get("name"),
                        "industry": industry,
                        "exchange": prof.get("exchange"),
                        "market_cap_usd_m": prof.get("marketCapitalization"),
                    }
                )
            ind_match = _industry_similar(subject_industry, industry)
            cap_ok = (
                subject_mcap <= 0.0
                or mcap <= 0.0
                or 0.1 <= (mcap / subject_mcap) <= 10.0
            )
            row["industry_match"] = bool(ind_match)
            row["cap_ratio"] = (
                round(mcap / subject_mcap, 3)
                if subject_mcap > 0 and mcap > 0
                else None
            )
            detailed.append(row)
            if ind_match and cap_ok:
                recommended.append(row)
            else:
                weak.append(row)
        # Run multiple targeted web queries and harvest candidate tickers
        # from the snippets. Exchange-qualified parentheticals and cashtags
        # are the strongest signal; plain uppercase words get profile-
        # validated before we trust them.
        web_raw: list[dict[str, Any]] = []
        queries: list[str] = []
        if subject_name:
            queries.append(f"{subject_name} top competitors")
            if subject_industry:
                queries.append(f"{subject_industry} stocks similar to {subject_name}")
            queries.append(f"{subject_name} peer comparison public companies")
        if self._search is not None and queries:
            search_coros = [self._search.search(q, top_k=5) for q in queries]
            results_per_q = await asyncio.gather(
                *search_coros, return_exceptions=True
            )
            for q, res in zip(queries, results_per_q, strict=False):
                if isinstance(res, Exception):
                    continue
                for r in res:
                    web_raw.append(
                        {
                            "query": q,
                            "title": r.title,
                            "url": r.url,
                            "snippet": r.snippet,
                        }
                    )

        # Harvest candidates from titles + snippets. Skip the subject itself
        # and anything already in the Finnhub peer list.
        known_syms = {symbol.upper(), *(t.upper() for t in peers)}
        candidate_tickers: list[str] = []
        seen_cands: set[str] = set()
        for r in web_raw:
            text = f"{r.get('title') or ''} {r.get('snippet') or ''}"
            for t in _extract_candidate_tickers(text):
                if t in known_syms or t in seen_cands:
                    continue
                seen_cands.add(t)
                candidate_tickers.append(t)
        # Cap lookups — Finnhub profile is rate-limited; trust strong signals first.
        candidate_tickers = candidate_tickers[:15]
        cand_profiles: list[Any] = []
        if candidate_tickers:
            cand_profiles = await asyncio.gather(
                *(self._finnhub.company_profile(t) for t in candidate_tickers),
                return_exceptions=True,
            )
        discovered: list[dict[str, Any]] = []
        for t, prof in zip(candidate_tickers, cand_profiles, strict=False):
            if not isinstance(prof, dict) or not prof.get("name"):
                continue
            industry = prof.get("finnhubIndustry")
            try:
                mcap = float(prof.get("marketCapitalization") or 0.0)
            except (TypeError, ValueError):
                mcap = 0.0
            ind_match = _industry_similar(subject_industry, industry)
            cap_ok = (
                subject_mcap <= 0.0
                or mcap <= 0.0
                or 0.05 <= (mcap / subject_mcap) <= 20.0
            )
            row = {
                "symbol": t,
                "name": prof.get("name"),
                "industry": industry,
                "exchange": prof.get("exchange"),
                "market_cap_usd_m": prof.get("marketCapitalization"),
                "industry_match": bool(ind_match),
                "cap_ratio": (
                    round(mcap / subject_mcap, 3)
                    if subject_mcap > 0 and mcap > 0
                    else None
                ),
                "source": "web_research",
            }
            discovered.append(row)
            if ind_match and cap_ok:
                recommended.append(row)
            elif ind_match or cap_ok:
                weak.append(row)

        # Merge + dedupe across finnhub + web-discovered, preserving first occurrence.
        def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            seen: set[str] = set()
            out: list[dict[str, Any]] = []
            for r in rows:
                s = str(r.get("symbol") or "").upper()
                if not s or s in seen:
                    continue
                seen.add(s)
                out.append(r)
            return out

        recommended = _dedupe(recommended)
        weak = _dedupe(weak)
        # A ticker can't be both strong and weak — strong wins.
        strong_syms = {r["symbol"] for r in recommended}
        weak = [r for r in weak if r["symbol"] not in strong_syms]

        notes: list[str] = []
        if subject_industry is None:
            notes.append(
                "subject industry unknown — cannot evaluate peer overlap. "
                "Trust the web research results over finnhub's peer list."
            )
        elif not recommended:
            notes.append(
                "no strong industry/cap peers found — likely a niche, small-cap, "
                "or recent listing. Cross-reference the 10-K competitive "
                "landscape section and web_research_results below."
            )
        elif len(recommended) < 3:
            notes.append(
                f"only {len(recommended)} strong peer(s) — supplement with "
                "web_research_results and filings for additional competitors."
            )
        if discovered:
            notes.append(
                f"{len(discovered)} additional candidate(s) surfaced from web "
                "research beyond finnhub's peer list."
            )
        payload = {
            "symbol": symbol,
            "subject_name": subject_name,
            "subject_industry": subject_industry,
            "subject_market_cap_usd_m": subject_mcap or None,
            "peers": peers,
            "peers_detailed": detailed,
            "recommended_peers": recommended,
            "weak_peers": weak,
            "web_discovered_peers": discovered,
            "web_research_results": web_raw,
            "web_research_queries": queries,
            "notes": notes,
        }
        preview_bits = [f"{len(peers)} raw"]
        if recommended:
            preview_bits.append(f"{len(recommended)} strong")
        if discovered:
            preview_bits.append(f"{len(discovered)} web")
        if subject_industry:
            preview_bits.append(subject_industry)
        preview = f"{symbol}: " + " · ".join(preview_bits)
        return (json.dumps(payload), preview, payload)

    async def _tool_get_basic_financials(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        if not symbol:
            payload = {"error": "empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        raw = (
            await self._finnhub.basic_financials(symbol)
            if self._finnhub is not None
            else None
        )
        if not raw:
            # Finnhub free tier has thin coverage on micro / recent-IPO
            # names. Fall back to Alpaca bars to synthesize 52w high/low,
            # YoY return, and volume so the model isn't left empty-handed.
            fb = await self._financials_from_bars(symbol)
            if fb is not None:
                return fb
            payload = {
                "symbol": symbol,
                "error": "no metrics",
                "note": (
                    "Finnhub returned no metrics for this ticker (common "
                    "for micro-caps and recent IPOs) and Alpaca bars were "
                    "also unavailable. Try `get_quote` for spot price, "
                    "`read_filing` on the latest 10-K/10-Q for fundamentals, "
                    "or skip fundamentals and focus on price/news."
                ),
            }
            return (
                json.dumps(payload),
                f"no metrics for {symbol} (source fallbacks exhausted)",
                payload,
            )
        metric = raw.get("metric") or {}
        keep_keys = [
            "marketCapitalization",
            "52WeekHigh",
            "52WeekLow",
            "52WeekPriceReturnDaily",
            "peNormalizedAnnual",
            "peBasicExclExtraTTM",
            "beta",
            "10DayAverageTradingVolume",
            "3MonthAverageTradingVolume",
            "revenueGrowthTTMYoy",
            "epsGrowthTTMYoy",
            "grossMarginTTM",
            "operatingMarginTTM",
            "netProfitMarginTTM",
            "dividendYieldIndicatedAnnual",
        ]
        summary = {k: metric.get(k) for k in keep_keys if metric.get(k) is not None}
        payload = {"symbol": symbol, "summary": summary, "raw": metric}
        high = metric.get("52WeekHigh")
        low = metric.get("52WeekLow")
        beta = metric.get("beta")
        preview = (
            f"{symbol} · 52w ${low}-{high} · β {beta}"
            if high is not None
            else f"{symbol} metrics ({len(metric)} fields)"
        )
        return (json.dumps(payload), preview, payload)

    async def _tool_get_analyst_ratings(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        if not symbol or self._finnhub is None:
            payload = {"error": "finnhub unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        recs, target = await asyncio.gather(
            self._finnhub.recommendations(symbol),
            self._finnhub.price_target(symbol),
        )
        latest = recs[0] if recs else {}
        payload = {
            "symbol": symbol,
            "price_target": target or {},
            "recommendations": recs[:6],
        }
        preview_parts: list[str] = []
        if latest:
            preview_parts.append(
                f"buy {latest.get('strongBuy', 0) + latest.get('buy', 0)} "
                f"/ hold {latest.get('hold', 0)} "
                f"/ sell {latest.get('sell', 0) + latest.get('strongSell', 0)}"
            )
        if target and target.get("targetMedian"):
            preview_parts.append(f"target ${target.get('targetMedian')}")
        preview = f"{symbol}: " + (" · ".join(preview_parts) or "no consensus")
        return (json.dumps(payload), preview, payload)

    async def _tool_get_earnings(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        if not symbol or self._finnhub is None:
            payload = {"error": "finnhub unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        upcoming, surprises = await asyncio.gather(
            self._finnhub.earnings_calendar(
                symbol=symbol, lookback_days=0, lookahead_days=120
            ),
            self._finnhub.earnings_surprises(symbol),
        )
        next_ev = upcoming[0] if upcoming else None
        payload = {
            "symbol": symbol,
            "next_earnings": next_ev,
            "surprises": surprises[:8],
        }
        if next_ev:
            preview = f"{symbol} next earnings {next_ev.get('date')}"
        elif surprises:
            s = surprises[0]
            preview = (
                f"{symbol} last EPS: actual {s.get('actual')} "
                f"vs est {s.get('estimate')} ({s.get('period')})"
            )
        else:
            preview = f"{symbol}: no earnings data"
        return (json.dumps(payload), preview, payload)

    async def _tool_get_insider_transactions(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        limit = max(1, min(int(args.get("limit") or 20), 50))
        if not symbol or self._finnhub is None:
            payload = {"error": "finnhub unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        rows = await self._finnhub.insider_transactions(symbol, limit=limit)
        payload = _summarize_insider_rows(symbol, rows)
        s = payload["summary"]
        preview = (
            f"{symbol}: {s['total_txns']} form-4 "
            f"({s['buy_count']} buys / {s['sell_count']} sells), "
            f"net ${s['net_usd']:+,.0f}"
        )
        return (json.dumps(payload), preview, payload)

    async def _tool_get_ownership(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        limit = max(1, min(int(args.get("limit") or 10), 25))
        if not symbol or self._finnhub is None:
            payload = {"error": "finnhub unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        inst_task = self._finnhub.ownership(symbol, limit=limit)
        fund_task = self._finnhub.fund_ownership(symbol, limit=limit)
        quote_task = self._finnhub.quote(symbol)
        inst, funds, quote = await asyncio.gather(
            inst_task, fund_task, quote_task, return_exceptions=True
        )
        if isinstance(inst, Exception):
            inst = []
        if isinstance(funds, Exception):
            funds = []
        price: float | None = None
        if not isinstance(quote, Exception) and quote is not None:
            price = float(quote.current)
        payload = _summarize_ownership(symbol, inst, funds)
        payload["price_used"] = price
        # Attach USD value at latest close if we got a quote.
        if price is not None:
            for bucket in ("institutions", "funds"):
                for r in payload[bucket]:
                    shares = r.get("shares")
                    if isinstance(shares, (int, float)):
                        r["value_usd"] = float(shares) * price
        s = payload["summary"]
        preview = (
            f"{symbol}: {s['institutions_count']} inst + "
            f"{s['funds_count']} fund holders"
            + (f", top10 inst ≈ {s['top10_inst_shares']:,.0f} sh" if s.get("top10_inst_shares") else "")
        )
        return (json.dumps(payload), preview, payload)

    async def _tool_get_market_context(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        if self._finnhub is None:
            payload = {"error": "finnhub unavailable"}
            return (json.dumps(payload), payload["error"], payload)
        symbols = ["SPY", "QQQ", "IWM", "VIX", "DXY", "TLT", "GLD"]
        quotes = await asyncio.gather(
            *(self._finnhub.quote(s) for s in symbols), return_exceptions=True
        )
        out: list[dict[str, Any]] = []
        for s, q in zip(symbols, quotes, strict=False):
            if hasattr(q, "to_dict"):
                d = q.to_dict()  # type: ignore[union-attr]
                out.append({"symbol": s, **d})
            else:
                out.append({"symbol": s, "error": "unavailable"})
        payload = {"snapshots": out}
        ups = sum(
            1 for e in out if isinstance(e.get("change_pct"), (int, float))
            and e["change_pct"] > 0
        )
        preview = f"{ups}/{len(out)} green"
        return (json.dumps(payload), preview, payload)

    # ---------- Alpaca: bars / technicals -----------------------------------

    async def _tool_get_price_history(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        days = max(5, min(int(args.get("days") or 260), 400))
        if not symbol or not self._alpaca_api_key:
            payload = {"error": "alpaca unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        end = datetime.now(UTC)
        start = end - timedelta(days=int(days * 1.6) + 5)
        url = f"{self._alpaca_data_url}/v2/stocks/{symbol}/bars"
        params = {
            "timeframe": "1Day",
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
            "limit": days,
            "adjustment": "split",
            "feed": "iex",
        }
        headers = {
            "APCA-API-KEY-ID": self._alpaca_api_key,
            "APCA-API-SECRET-KEY": self._alpaca_api_secret or "",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json() or {}
        except Exception as exc:
            payload = {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"}
            return (json.dumps(payload), payload["error"], payload)

        bars = data.get("bars") or []
        compact = [
            {
                "t": b.get("t"),
                "o": b.get("o"),
                "h": b.get("h"),
                "l": b.get("l"),
                "c": b.get("c"),
                "v": b.get("v"),
            }
            for b in bars
        ][-days:]
        our_trades = await self._load_trade_markers(symbol)
        earnings = await self._load_earnings_markers(symbol)
        payload = {
            "symbol": symbol,
            "days": len(compact),
            "bars": compact,
            "our_trades": our_trades,
            "earnings_dates": earnings,
        }
        if compact:
            first = compact[0]["c"]
            last = compact[-1]["c"]
            move = ((last - first) / first * 100.0) if first else 0.0
            preview = (
                f"{symbol} {len(compact)}d: "
                f"${first:.2f} → ${last:.2f} ({move:+.1f}%)"
            )
        else:
            preview = f"{symbol}: no bars"
        return (json.dumps(payload), preview, payload)

    async def _tool_get_intraday_history(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        timeframe = str(args.get("timeframe") or "15Min").strip()
        sessions = max(1, min(int(args.get("sessions") or 3), 10))
        if timeframe not in {"5Min", "15Min", "1Hour"}:
            payload = {"error": f"invalid timeframe {timeframe}"}
            return (json.dumps(payload), payload["error"], payload)
        if not symbol or not self._alpaca_api_key:
            payload = {"error": "alpaca unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        end = datetime.now(UTC)
        start = end - timedelta(days=sessions * 3 + 2)
        url = f"{self._alpaca_data_url}/v2/stocks/{symbol}/bars"
        limit = {"5Min": 400, "15Min": 260, "1Hour": 160}[timeframe]
        params = {
            "timeframe": timeframe,
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
            "limit": limit,
            "adjustment": "raw",
            "feed": "iex",
        }
        headers = {
            "APCA-API-KEY-ID": self._alpaca_api_key,
            "APCA-API-SECRET-KEY": self._alpaca_api_secret or "",
        }
        try:
            async with httpx.AsyncClient(timeout=10.0, headers=headers) as client:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                data = resp.json() or {}
        except Exception as exc:
            payload = {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"}
            return (json.dumps(payload), payload["error"], payload)
        bars = data.get("bars") or []
        compact = [
            {
                "t": b.get("t"),
                "o": b.get("o"),
                "h": b.get("h"),
                "l": b.get("l"),
                "c": b.get("c"),
                "v": b.get("v"),
            }
            for b in bars
        ]
        payload = {
            "symbol": symbol,
            "timeframe": timeframe,
            "count": len(compact),
            "bars": compact,
        }
        if compact:
            first = compact[0]["c"]
            last = compact[-1]["c"]
            move = ((last - first) / first * 100.0) if first else 0.0
            preview = (
                f"{symbol} {timeframe} {len(compact)} bars "
                f"${first:.2f} → ${last:.2f} ({move:+.1f}%)"
            )
        else:
            preview = f"{symbol} {timeframe}: no bars"
        return (json.dumps(payload), preview, payload)

    async def _tool_get_technicals(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        days = max(60, min(int(args.get("days") or 260), 400))
        if not symbol or not self._alpaca_api_key:
            payload = {"error": "alpaca unavailable or empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        _, _, bars_payload = await self._tool_get_price_history(
            {"symbol": symbol, "days": days}
        )
        bars = bars_payload.get("bars") or []
        closes = [float(b["c"]) for b in bars if b.get("c") is not None]
        highs = [float(b["h"]) for b in bars if b.get("h") is not None]
        lows = [float(b["l"]) for b in bars if b.get("l") is not None]
        if len(closes) < 20:
            payload = {"symbol": symbol, "error": "insufficient bars"}
            return (json.dumps(payload), payload["error"], payload)
        metrics = _compute_technicals(closes, highs, lows)
        payload = {"symbol": symbol, "days": len(closes), **metrics}
        preview_parts: list[str] = [f"{symbol}"]
        if metrics.get("rsi14") is not None:
            preview_parts.append(f"RSI {metrics['rsi14']:.0f}")
        if metrics.get("sma_50") is not None and metrics.get("sma_200") is not None:
            preview_parts.append(
                f"SMA50 ${metrics['sma_50']:.2f} / SMA200 ${metrics['sma_200']:.2f}"
            )
        if metrics.get("pct_from_52w_high") is not None:
            preview_parts.append(f"{metrics['pct_from_52w_high']:+.0f}% vs 52w hi")
        preview = " · ".join(preview_parts)
        return (json.dumps(payload), preview, payload)

    # ---------- SEC ----------------------------------------------------------

    async def _tool_get_sec_filings(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = str(args.get("symbol") or "").upper().strip()
        limit = max(1, min(int(args.get("limit") or 10), 25))
        form_filter = (args.get("form_type") or "").strip().upper() or None
        if not symbol:
            payload = {"error": "empty symbol"}
            return (json.dumps(payload), payload["error"], payload)
        cik_str = await _sec_cik_for_ticker(symbol)
        if not cik_str:
            payload = {"symbol": symbol, "error": "not in SEC ticker list"}
            return (
                json.dumps(payload),
                f"{symbol} not found in SEC ticker map",
                payload,
            )
        padded = cik_str.zfill(10)
        url = f"https://data.sec.gov/submissions/CIK{padded}.json"
        try:
            async with httpx.AsyncClient(
                timeout=10.0, headers=_SEC_HEADERS
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            msg = f"SEC {exc.response.status_code} for {symbol}"
            logger.info("%s (%s)", msg, url)
            payload = {"symbol": symbol, "error": msg}
            return (json.dumps(payload), msg, payload)
        except Exception as exc:
            payload = {"symbol": symbol, "error": f"{type(exc).__name__}: {exc}"}
            return (json.dumps(payload), payload["error"], payload)

        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accessions = recent.get("accessionNumber") or []
        primary_docs = recent.get("primaryDocument") or []
        out: list[dict[str, Any]] = []
        for form, date, acc, doc in zip(
            forms, dates, accessions, primary_docs, strict=False
        ):
            if form_filter and form.upper() != form_filter:
                continue
            acc_nodash = acc.replace("-", "")
            doc_url = (
                f"https://www.sec.gov/Archives/edgar/data/{int(cik_str)}/"
                f"{acc_nodash}/{doc}"
            )
            out.append(
                {"form": form, "filed": date, "accession": acc, "url": doc_url}
            )
            if len(out) >= limit:
                break
        payload = {
            "symbol": symbol,
            "cik": padded,
            "name": data.get("name"),
            "filings": out,
        }
        preview = (
            f"{symbol} / {data.get('name', '?')}: {len(out)} filings"
            + (f" ({form_filter})" if form_filter else "")
        )
        return (json.dumps(payload), preview, payload)

    async def _tool_search_sec(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        query = str(args.get("query") or "").strip()
        limit = max(1, min(int(args.get("limit") or 10), 20))
        form_filter = (args.get("form_type") or "").strip().upper()
        if not query:
            payload = {"error": "empty query"}
            return (json.dumps(payload), payload["error"], payload)
        params: dict[str, Any] = {"q": query}
        if form_filter:
            params["forms"] = form_filter
        try:
            async with httpx.AsyncClient(
                timeout=10.0, headers=_SEC_HEADERS
            ) as client:
                resp = await client.get(
                    "https://efts.sec.gov/LATEST/search-index", params=params
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            msg = f"SEC search {exc.response.status_code}"
            logger.info("%s for %r", msg, query)
            payload = {"query": query, "error": msg}
            return (json.dumps(payload), msg, payload)
        except Exception as exc:
            payload = {"query": query, "error": f"{type(exc).__name__}: {exc}"}
            return (json.dumps(payload), payload["error"], payload)

        hits = (data.get("hits") or {}).get("hits") or []
        out: list[dict[str, Any]] = []
        for h in hits[:limit]:
            src = h.get("_source") or {}
            acc = (h.get("_id") or "").split(":")[0]
            cik = ((src.get("ciks") or [None])[0]) or ""
            files = h.get("_source", {}).get("display_names") or []
            url = (
                f"https://www.sec.gov/cgi-bin/browse-edgar?"
                f"action=getcompany&CIK={cik}&type={src.get('form', '')}"
                f"&dateb=&owner=include&count=10"
                if cik
                else ""
            )
            out.append(
                {
                    "company": files[0] if files else None,
                    "form": src.get("form"),
                    "filed": src.get("file_date"),
                    "accession": acc,
                    "search_url": url,
                }
            )
        payload = {"query": query, "form_type": form_filter or None, "hits": out}
        preview = f"{len(out)} SEC hits for {query!r}"
        return (json.dumps(payload), preview, payload)

    async def _tool_read_filing(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        url = str(args.get("url") or "").strip()
        max_chars = max(2000, min(int(args.get("max_chars") or 15000), 40000))
        include_text = bool(args.get("include_text"))
        if not url:
            payload = {"error": "empty url"}
            return (json.dumps(payload), payload["error"], payload)
        html: str | None = None
        resolved_url = url
        last_error: str | None = None
        candidates = _sec_url_candidates(url)
        try:
            async with httpx.AsyncClient(
                timeout=15.0, headers=_SEC_HEADERS, follow_redirects=True
            ) as client:
                for cand in candidates:
                    try:
                        resp = await client.get(cand)
                        resp.raise_for_status()
                        html = resp.text
                        resolved_url = cand
                        break
                    except Exception as exc:
                        last_error = f"{type(exc).__name__}: {exc}"
                        continue
                if html is None:
                    # Last resort — discover the primary doc via index.json.
                    primary = await _sec_discover_primary_doc(client, url)
                    if primary and primary not in candidates:
                        try:
                            resp = await client.get(primary)
                            resp.raise_for_status()
                            html = resp.text
                            resolved_url = primary
                        except Exception as exc:
                            last_error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if html is None:
            payload = {
                "url": url,
                "error": last_error or "fetch failed",
                "tried": candidates,
            }
            return (json.dumps(payload), payload["error"], payload)
        url = resolved_url
        text = _html_to_text(html)
        highlights = _extract_filing_highlights(text, url)
        found_anything = bool(
            highlights["sections"]
            or highlights["items_8k"]
            or highlights["money"]
            or highlights["share_actions"]
            or highlights["percent_moves"]
        )
        # When we can't extract anything structured, fall back to the raw
        # truncated body so the agent still has something to read.
        include_raw = include_text or not found_anything
        truncated = len(text) > max_chars
        body = text[:max_chars] if include_raw else ""
        payload: dict[str, Any] = {
            "url": url,
            "total_chars": len(text),
            "highlights": highlights,
        }
        if include_raw:
            payload["text"] = body
            payload["chars"] = len(body)
            payload["truncated"] = truncated
        section_summary = ", ".join(sorted(highlights["sections"].keys())) or None
        pieces: list[str] = []
        if highlights["items_8k"]:
            pieces.append(f"{len(highlights['items_8k'])} 8-K items")
        if section_summary:
            pieces.append(section_summary)
        if highlights["money"]:
            pieces.append(f"{len(highlights['money'])} $ figures")
        if highlights["share_actions"]:
            pieces.append(f"{len(highlights['share_actions'])} share actions")
        if not pieces:
            pieces.append(f"{len(body):,} raw chars")
        preview = "filing: " + " · ".join(pieces)
        return (json.dumps(payload), preview, payload)

    # ---------- fallbacks ---------------------------------------------------

    async def _financials_from_bars(
        self, symbol: str
    ) -> tuple[str, str, dict[str, Any]] | None:
        """Synthesize basic_financials from 1Y of Alpaca bars.

        Used when Finnhub coverage is missing. Covers 52-week high/low,
        current price, 1Y price return, and 30/90-day ADV. Intentionally
        a subset of the Finnhub metric bundle — returns None if we can't
        even produce that much.
        """
        if not self._alpaca_api_key:
            return None
        _, _, bars_payload = await self._tool_get_price_history(
            {"symbol": symbol, "days": 260}
        )
        bars = (bars_payload or {}).get("bars") or []
        if not bars or len(bars) < 20:
            return None
        highs = [b["h"] for b in bars if b.get("h") is not None]
        lows = [b["l"] for b in bars if b.get("l") is not None]
        closes = [b["c"] for b in bars if b.get("c") is not None]
        vols = [b["v"] for b in bars if b.get("v") is not None]
        if not highs or not lows or not closes:
            return None
        wk52_high = max(highs)
        wk52_low = min(lows)
        last = closes[-1]
        first = closes[0]
        return_pct = ((last - first) / first * 100.0) if first else 0.0
        adv30 = sum(vols[-30:]) / min(30, len(vols[-30:])) if vols else None
        adv90 = sum(vols[-90:]) / min(90, len(vols[-90:])) if vols else None
        summary = {
            "52WeekHigh": wk52_high,
            "52WeekLow": wk52_low,
            "lastClose": last,
            "52WeekPriceReturnDaily": round(return_pct, 2),
            "10DayAverageTradingVolume": (
                round(sum(vols[-10:]) / min(10, len(vols[-10:])), 0)
                if vols else None
            ),
            "3MonthAverageTradingVolume": (
                round(adv90, 0) if adv90 is not None else None
            ),
        }
        summary = {k: v for k, v in summary.items() if v is not None}
        payload = {
            "symbol": symbol,
            "summary": summary,
            "source": "computed_from_bars",
            "note": (
                "Finnhub had no metrics for this ticker (common for "
                "micro-caps / recent IPOs). These fields were computed "
                "from Alpaca daily bars. Fields like P/E, margins, and "
                "beta are NOT available — if you need them, read the "
                "latest 10-K/10-Q via `read_filing`."
            ),
        }
        preview = (
            f"{symbol} · 52w ${wk52_low:.2f}-${wk52_high:.2f} · "
            f"1y {return_pct:+.1f}% (computed)"
        )
        return (json.dumps(payload), preview, payload)

    # ---------- our own DB --------------------------------------------------

    async def _load_trade_markers(self, symbol: str) -> list[dict[str, Any]]:
        if self._session_factory is None:
            return []
        try:
            async with self._session_factory() as session:
                stmt = (
                    select(Trade)
                    .where(Trade.symbol == symbol)
                    .order_by(desc(Trade.id))
                    .limit(40)
                )
                rows = (await session.execute(stmt)).scalars().all()
        except Exception:
            logger.warning("load_trade_markers failed for %s", symbol, exc_info=True)
            return []
        out: list[dict[str, Any]] = []
        for t in rows:
            if t.opened_at and t.entry_price:
                out.append(
                    {
                        "kind": "entry",
                        "action": t.action,
                        "price": float(t.entry_price),
                        "date": t.opened_at.isoformat(),
                    }
                )
            if t.closed_at and t.exit_price:
                out.append(
                    {
                        "kind": "exit",
                        "action": t.action,
                        "price": float(t.exit_price),
                        "date": t.closed_at.isoformat(),
                        "pnl": float(t.realized_pnl_usd or 0.0),
                    }
                )
        return out

    async def _load_earnings_markers(self, symbol: str) -> list[str]:
        if self._finnhub is None:
            return []
        try:
            events = await self._finnhub.earnings_calendar(
                symbol=symbol, lookback_days=365, lookahead_days=120
            )
        except Exception:
            return []
        return [
            e.get("date") for e in events if isinstance(e.get("date"), str)
        ]

    async def _tool_get_recent_trades(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = args.get("symbol")
        symbol = str(symbol).upper().strip() if symbol else None
        limit = max(1, min(int(args.get("limit") or 20), 50))
        if self._session_factory is None:
            payload = {"error": "db unavailable"}
            return (json.dumps(payload), payload["error"], payload)
        async with self._session_factory() as session:
            if symbol:
                stmt = (
                    select(Trade)
                    .where(Trade.symbol == symbol)
                    .order_by(desc(Trade.id))
                    .limit(limit)
                )
            else:
                stmt = select(Trade).order_by(desc(Trade.id)).limit(limit)
            rows = (await session.execute(stmt)).scalars().all()
        items = [
            {
                "id": t.id,
                "symbol": t.symbol,
                "action": t.action,
                "status": t.status,
                "size_usd": float(t.size_usd or 0.0),
                "entry_price": float(t.entry_price or 0.0) if t.entry_price else None,
                "exit_price": float(t.exit_price or 0.0) if t.exit_price else None,
                "realized_pnl_usd": float(t.realized_pnl_usd or 0.0),
                "opened_at": t.opened_at.isoformat() if t.opened_at else None,
                "closed_at": t.closed_at.isoformat() if t.closed_at else None,
            }
            for t in rows
        ]
        payload = {"symbol": symbol, "trades": items}
        preview = f"{len(items)} trade(s)" + (f" for {symbol}" if symbol else "")
        return (json.dumps(payload), preview, payload)

    async def _tool_get_recent_decisions(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        symbol = args.get("symbol")
        symbol = str(symbol).upper().strip() if symbol else None
        limit = max(1, min(int(args.get("limit") or 20), 50))
        if self._session_factory is None:
            payload = {"error": "db unavailable"}
            return (json.dumps(payload), payload["error"], payload)
        async with self._session_factory() as session:
            stmt = select(Decision).order_by(desc(Decision.id)).limit(limit * 3)
            rows = (await session.execute(stmt)).scalars().all()
        out = []
        for row in rows:
            prop = row.proposal_json or {}
            if symbol and prop.get("symbol") != symbol:
                continue
            out.append(
                {
                    "id": row.id,
                    "created_at": row.created_at.isoformat(),
                    "symbol": prop.get("symbol"),
                    "action": prop.get("action"),
                    "approved": row.approved,
                    "executed": row.executed,
                    "rationale": row.rationale,
                    "rejection_code": row.rejection_code,
                }
            )
            if len(out) >= limit:
                break
        payload = {"symbol": symbol, "decisions": out}
        preview = f"{len(out)} decision(s)" + (f" for {symbol}" if symbol else "")
        return (json.dumps(payload), preview, payload)

    # ---------- compound ----------------------------------------------------

    async def _tool_deep_dive(
        self, args: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any]]:
        """One-shot dossier: profile, quote, financials, analyst, earnings,
        insiders, news, filings, peers (with industry verification) and —
        when industry is known — a sector-scoped web search.

        Optimised for unfamiliar tickers where the agent would otherwise
        fire 6-8 single-purpose tools and pay the round-trip tax.
        """
        symbol = str(args.get("symbol") or "").upper().strip()
        include_filings = args.get("include_filings")
        include_filings = True if include_filings is None else bool(include_filings)
        include_web = args.get("include_web")
        include_web = True if include_web is None else bool(include_web)
        if not symbol:
            payload = {"error": "empty symbol"}
            return (json.dumps(payload), payload["error"], payload)

        # Stage 1: fan out the Finnhub-backed tools in parallel.
        profile_task = self._tool_get_company_profile({"symbol": symbol})
        quote_task = self._tool_get_quote({"symbol": symbol})
        news_task = self._tool_get_company_news({"symbol": symbol, "limit": 5})
        financials_task = self._tool_get_basic_financials({"symbol": symbol})
        analyst_task = self._tool_get_analyst_ratings({"symbol": symbol})
        earnings_task = self._tool_get_earnings({"symbol": symbol})
        insider_task = self._tool_get_insider_transactions({"symbol": symbol, "limit": 10})
        peers_task = self._tool_get_peers({"symbol": symbol})
        filings_task: Any = None
        if include_filings:
            filings_task = self._tool_get_sec_filings({"symbol": symbol, "limit": 6})
        tasks: list[Any] = [
            profile_task,
            quote_task,
            news_task,
            financials_task,
            analyst_task,
            earnings_task,
            insider_task,
            peers_task,
        ]
        if filings_task is not None:
            tasks.append(filings_task)
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        def _payload_of(r: Any) -> dict[str, Any]:
            if isinstance(r, tuple) and len(r) == 3:
                return r[2] if isinstance(r[2], dict) else {}
            if isinstance(r, Exception):
                return {"error": f"{type(r).__name__}: {r}"}
            return {}

        profile_p = _payload_of(raw_results[0])
        quote_p = _payload_of(raw_results[1])
        news_p = _payload_of(raw_results[2])
        financials_p = _payload_of(raw_results[3])
        analyst_p = _payload_of(raw_results[4])
        earnings_p = _payload_of(raw_results[5])
        insider_p = _payload_of(raw_results[6])
        peers_p = _payload_of(raw_results[7])
        filings_p = _payload_of(raw_results[8]) if include_filings else {}

        subject_name = profile_p.get("name")
        subject_industry = profile_p.get("finnhubIndustry") or profile_p.get(
            "industry"
        )

        # Stage 2: with profile in hand, do sector-scoped web sweeps.
        # We don't silently re-issue web_search for the agent — this is
        # the explicit behaviour of deep_dive. When name + industry are
        # known we run two queries: competitors and news/outlook.
        web_results: dict[str, Any] = {}
        if include_web and subject_name:
            queries: list[str] = []
            if subject_industry:
                queries.append(f"{subject_name} {subject_industry} competitors")
                queries.append(f"{subject_name} {subject_industry} outlook 2025 2026")
            else:
                queries.append(f"{subject_name} competitors")
                queries.append(f"{subject_name} outlook")
            web_tasks = [self._search.search(q, top_k=4) for q in queries]
            web_raw = await asyncio.gather(*web_tasks, return_exceptions=True)
            for q, r in zip(queries, web_raw, strict=False):
                if isinstance(r, Exception) or not r:
                    web_results[q] = []
                else:
                    web_results[q] = [
                        {"title": x.title, "url": x.url, "snippet": x.snippet}
                        for x in r
                    ]

        payload = {
            "symbol": symbol,
            "profile": profile_p,
            "quote": quote_p,
            "financials": financials_p,
            "analyst": analyst_p,
            "earnings": earnings_p,
            "insider": insider_p,
            "news": news_p.get("items") or [],
            "peers": {
                "raw": peers_p.get("peers") or [],
                "detailed": peers_p.get("peers_detailed") or [],
                "industry_matched": peers_p.get("industry_matched") or [],
                "industry_mismatched": peers_p.get("industry_mismatched") or [],
                "subject_industry": peers_p.get("subject_industry"),
                "web_peer_search": peers_p.get("web_peer_search"),
            },
            "filings": filings_p.get("filings") or [],
            "web": web_results,
        }
        bits: list[str] = [symbol]
        if subject_name:
            bits.append(str(subject_name)[:40])
        if subject_industry:
            bits.append(str(subject_industry))
        if isinstance(quote_p.get("current"), (int, float)):
            bits.append(f"${quote_p['current']:.2f}")
        preview = "deep_dive · " + " · ".join(bits)
        return (json.dumps(payload), preview, payload)


__all__ = [
    "ALL_TOOLS",
    "CACHEABLE_TOOLS",
    "DEEP_DIVE_TOOL",
    "FETCH_URL_TOOL",
    "GET_ANALYST_RATINGS_TOOL",
    "GET_BASIC_FINANCIALS_TOOL",
    "GET_COMPANY_NEWS_TOOL",
    "GET_COMPANY_PROFILE_TOOL",
    "GET_EARNINGS_TOOL",
    "GET_INSIDER_TXNS_TOOL",
    "GET_INTRADAY_TOOL",
    "GET_MARKET_CONTEXT_TOOL",
    "GET_OWNERSHIP_TOOL",
    "GET_PEERS_TOOL",
    "GET_PRICE_HISTORY_TOOL",
    "GET_QUOTE_TOOL",
    "GET_RECENT_DECISIONS_TOOL",
    "GET_RECENT_TRADES_TOOL",
    "GET_SEC_FILINGS_TOOL",
    "GET_TECHNICALS_TOOL",
    "READ_FILING_TOOL",
    "SEARCH_SEC_TOOL",
    "WEB_SEARCH_TOOL",
    "ResearchToolbelt",
    "cache_signature",
    "is_cacheable",
    "tool_names",
]
