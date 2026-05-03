"""Web research tools for the trading agent.

Two tools exposed via LLM tool-use:
  - ``web_search``  — top-N results from DuckDuckGo's HTML endpoint
  - ``fetch_url``   — pull + clean readable text from a page

Both share an in-memory TTL cache so repeated queries across a decision
cycle (or within a short window across cycles) don't re-hit the network.
The cache is also the rate-limit — capped by max entries and a minimum
inter-query delay.

DuckDuckGo is the default backend because it works without an API key.
A paid backend (Brave, Serper) can be dropped in by adding the env knobs
and branching in ``WebSearchClient.search``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


_SEARCH_CACHE_TTL_SEC = 900  # 15 min — headlines turn over fast but cycle-to-cycle reuse is fine
_FETCH_CACHE_TTL_SEC = 1800  # 30 min
_MAX_CACHE_ENTRIES = 256
_FETCH_MAX_BYTES = 200_000  # 200KB before truncation
_MAX_CLEAN_CHARS = 8000  # hand this much text to the model per fetch
_MIN_INTERVAL_SEC = 0.25  # minimum spacing between network calls (soft rate limit)


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    title: str | None
    text: str
    truncated: bool


class _TtlCache:
    """Tiny TTL cache with FIFO eviction. Safe for asyncio concurrent access."""

    def __init__(self, max_entries: int = _MAX_CACHE_ENTRIES) -> None:
        self._entries: dict[str, tuple[float, Any]] = {}
        self._max = max_entries
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Any | None:
        async with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.time():
                self._entries.pop(key, None)
                return None
            return value

    async def set(self, key: str, value: Any, ttl_sec: int) -> None:
        async with self._lock:
            if len(self._entries) >= self._max:
                # Drop oldest (dicts preserve insertion order)
                oldest = next(iter(self._entries))
                self._entries.pop(oldest, None)
            self._entries[key] = (time.time() + ttl_sec, value)


class WebSearchClient:
    """Web search with multi-provider fallback.

    Tries API-keyed providers first in order of reliability (Tavily →
    Brave → Serper), falls back to keyless DuckDuckGo HTML scraping. The
    first provider to return non-empty results wins; failures silently
    cascade to the next backend.
    """

    _DDG_URL = "https://html.duckduckgo.com/html/"
    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    )
    _HEADERS = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        tavily_api_key: str = "",
        brave_api_key: str = "",
        serper_api_key: str = "",
    ) -> None:
        self._timeout = timeout
        self._cache = _TtlCache()
        self._last_call = 0.0
        self._spacing_lock = asyncio.Lock()
        self._tavily_key = (tavily_api_key or "").strip()
        self._brave_key = (brave_api_key or "").strip()
        self._serper_key = (serper_api_key or "").strip()

    async def search(self, query: str, *, top_k: int = 6) -> list[SearchResult]:
        key = f"search::{query.strip().lower()}::{top_k}"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached

        providers: list[tuple[str, Any]] = []
        if self._tavily_key:
            providers.append(("tavily", self._search_tavily))
        if self._brave_key:
            providers.append(("brave", self._search_brave))
        if self._serper_key:
            providers.append(("serper", self._search_serper))
        providers.append(("ddg", self._search_ddg))

        results: list[SearchResult] = []
        for name, fn in providers:
            await self._space()
            try:
                results = await fn(query, top_k)
            except httpx.HTTPStatusError as exc:
                # 401/402/403/429 = bad key / quota / rate-limit.
                # We have a cascade of providers, so just try the next one.
                logger.info(
                    "web_search %s unavailable (%s) — trying next provider",
                    name, exc.response.status_code,
                )
                results = []
            except Exception as exc:
                logger.info(
                    "web_search %s raised %s — trying next provider",
                    name, type(exc).__name__,
                )
                results = []
            if results:
                logger.debug("web_search %s returned %d for %r", name, len(results), query)
                break

        await self._cache.set(key, results, _SEARCH_CACHE_TTL_SEC)
        return results

    async def _search_tavily(
        self, query: str, top_k: int
    ) -> list[SearchResult]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                "https://api.tavily.com/search",
                json={
                    "api_key": self._tavily_key,
                    "query": query,
                    "max_results": top_k,
                    "search_depth": "basic",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        out: list[SearchResult] = []
        for r in data.get("results", [])[:top_k]:
            url = r.get("url") or ""
            title = r.get("title") or url
            snippet = r.get("content") or ""
            if url:
                out.append(SearchResult(title=title, url=url, snippet=snippet))
        return out

    async def _search_brave(
        self, query: str, top_k: int
    ) -> list[SearchResult]:
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._brave_key,
        }
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
            resp = await client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={"q": query, "count": top_k},
            )
            resp.raise_for_status()
            data = resp.json()
        out: list[SearchResult] = []
        for r in (data.get("web") or {}).get("results", [])[:top_k]:
            url = r.get("url") or ""
            title = r.get("title") or url
            snippet = r.get("description") or ""
            if url:
                out.append(
                    SearchResult(title=_html_to_text(title), url=url, snippet=_html_to_text(snippet))
                )
        return out

    async def _search_serper(
        self, query: str, top_k: int
    ) -> list[SearchResult]:
        headers = {
            "X-API-KEY": self._serper_key,
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout, headers=headers) as client:
            resp = await client.post(
                "https://google.serper.dev/search",
                json={"q": query, "num": top_k},
            )
            resp.raise_for_status()
            data = resp.json()
        out: list[SearchResult] = []
        for r in data.get("organic", [])[:top_k]:
            url = r.get("link") or ""
            title = r.get("title") or url
            snippet = r.get("snippet") or ""
            if url:
                out.append(SearchResult(title=title, url=url, snippet=snippet))
        return out

    async def _search_ddg(
        self, query: str, top_k: int
    ) -> list[SearchResult]:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._HEADERS,
            follow_redirects=True,
        ) as client:
            resp = await client.post(self._DDG_URL, data={"q": query})
            resp.raise_for_status()
            html_body = resp.text
        return _parse_ddg_html(html_body)[:top_k]

    async def _space(self) -> None:
        """Enforce minimum inter-query spacing."""
        async with self._spacing_lock:
            delta = time.time() - self._last_call
            if delta < _MIN_INTERVAL_SEC:
                await asyncio.sleep(_MIN_INTERVAL_SEC - delta)
            self._last_call = time.time()


class UrlFetchClient:
    """Fetch a URL and return a cleaned-up, length-capped text body."""

    # Same as WebSearchClient — most news sites 401/403 bot-looking UAs.
    _UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36"
    )
    _HEADERS = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._cache = _TtlCache()
        self._last_call = 0.0
        self._spacing_lock = asyncio.Lock()

    async def fetch(self, url: str) -> FetchResult | None:
        key = f"fetch::{url}"
        cached = await self._cache.get(key)
        if cached is not None:
            return cached

        await self._space()
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                headers=self._HEADERS,
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                body = resp.content[:_FETCH_MAX_BYTES]
                text = body.decode(resp.encoding or "utf-8", errors="replace")
        except httpx.HTTPError as exc:
            logger.info("fetch_url skipped (%s): %s", type(exc).__name__, url)
            return None
        except Exception:
            logger.warning("fetch_url failed for %s", url, exc_info=True)
            return None

        title = _extract_title(text)
        cleaned = _strip_to_text(text)
        truncated = len(cleaned) > _MAX_CLEAN_CHARS
        if truncated:
            cleaned = cleaned[:_MAX_CLEAN_CHARS]
        result = FetchResult(url=url, title=title, text=cleaned, truncated=truncated)
        await self._cache.set(key, result, _FETCH_CACHE_TTL_SEC)
        return result

    async def _space(self) -> None:
        async with self._spacing_lock:
            delta = time.time() - self._last_call
            if delta < _MIN_INTERVAL_SEC:
                await asyncio.sleep(_MIN_INTERVAL_SEC - delta)
            self._last_call = time.time()


_DDG_RESULT_RE = re.compile(
    r'<a rel="nofollow" class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>'
    r'.*?<a class="result__snippet"[^>]*>(.*?)</a>',
    re.DOTALL,
)
_DDG_URL_PARAM_RE = re.compile(r"uddg=([^&]+)")


def _parse_ddg_html(body: str) -> list[SearchResult]:
    """Parse DuckDuckGo HTML results. Format is stable enough for a regex.

    DDG wraps the real URL behind a redirect like
    ``//duckduckgo.com/l/?uddg=<encoded>`` — we unwrap that to get the
    actual target."""
    out: list[SearchResult] = []
    for match in _DDG_RESULT_RE.finditer(body):
        raw_url, title_html, snippet_html = match.groups()
        url = _unwrap_ddg_url(raw_url)
        title = _html_to_text(title_html)
        snippet = _html_to_text(snippet_html)
        if url and title:
            out.append(SearchResult(title=title, url=url, snippet=snippet))
    return out


def _unwrap_ddg_url(raw: str) -> str:
    """DDG wraps redirects; pull the real URL out of the uddg= param."""
    import urllib.parse as up

    if raw.startswith("//"):
        raw = "https:" + raw
    m = _DDG_URL_PARAM_RE.search(raw)
    if m:
        return up.unquote(m.group(1))
    return raw


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)


def _strip_to_text(body: str) -> str:
    stripped = _SCRIPT_STYLE_RE.sub("", body)
    stripped = _TAG_RE.sub(" ", stripped)
    stripped = html.unescape(stripped)
    stripped = _WS_RE.sub(" ", stripped).strip()
    return stripped


def _html_to_text(fragment: str) -> str:
    return html.unescape(_TAG_RE.sub("", fragment)).strip()


def _extract_title(body: str) -> str | None:
    m = _TITLE_RE.search(body)
    if not m:
        return None
    return _html_to_text(m.group(1))[:200] or None


__all__ = [
    "FetchResult",
    "SearchResult",
    "UrlFetchClient",
    "WebSearchClient",
]
