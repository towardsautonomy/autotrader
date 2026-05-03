"""Agentic research loop.

Wraps ``LLMProvider`` with a tool-use loop that exposes ``web_search`` and
``fetch_url`` alongside ``propose_trade``. The model can issue any number
of research calls before committing to a final proposal — or it can go
straight to ``propose_trade`` if the prompt context is already enough.

The loop terminates when:
  - The model calls ``propose_trade`` → return the AIResponse.
  - The per-cycle tool-call budget is exhausted → the wrapper forces a
    final decision by re-asking with research disabled.
  - The provider fails N times in a row → raise.

Every tool result is also published to the activity bus so the UI can
show what the agent actually read.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import EventSeverity, get_bus
from app.ai.llm_provider import AIResponse, LLMProvider, TRADE_TOOL
from app.ai.research import UrlFetchClient, WebSearchClient
from app.ai.research_toolbelt import ResearchToolbelt
from app.ai.usage import log_usage

logger = logging.getLogger(__name__)


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the public web for up-to-date information (news, filings, "
            "earnings calendars, analyst commentary). Use this BEFORE "
            "propose_trade when the prompt lacks a catalyst or you need to "
            "confirm a thesis. Keep queries specific — ticker + topic."
        ),
        "parameters": {
            "type": "object",
            "required": ["query"],
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query, e.g. 'NVDA earnings preview Q3 2026'.",
                },
                "top_k": {
                    "type": "integer",
                    "description": "How many results to return (1-8).",
                    "minimum": 1,
                    "maximum": 8,
                },
            },
        },
    },
}


FETCH_URL_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": (
            "Fetch a URL's readable text (stripped of HTML). Use this to "
            "drill into a specific article or filing surfaced by web_search. "
            "Large pages are truncated to a few thousand characters."
        ),
        "parameters": {
            "type": "object",
            "required": ["url"],
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Absolute URL to fetch.",
                },
            },
        },
    },
}


@dataclass
class ResearchArtifact:
    """A single tool call the agent made during a decision cycle."""

    tool: str  # "web_search" | "fetch_url"
    arguments: dict[str, Any]
    result_preview: str
    result_count: int = 0


@dataclass
class ResearchOutcome:
    """Result bundle from a research loop."""

    response: AIResponse
    artifacts: list[ResearchArtifact] = field(default_factory=list)
    aggregate_usage: dict[str, int] = field(default_factory=dict)


class ResearchAgent:
    """Agentic wrapper around ``LLMProvider`` with web + fetch tools.

    A single public entrypoint: :meth:`propose`. Internally loops until
    the model emits ``propose_trade``.
    """

    def __init__(
        self,
        *,
        provider: LLMProvider,
        search_client: WebSearchClient | None = None,
        fetch_client: UrlFetchClient | None = None,
        max_tool_calls: int = 6,
        max_rounds: int = 8,
        session_factory: async_sessionmaker | None = None,
        toolbelt: ResearchToolbelt | None = None,
        extra_tool_names: list[str] | None = None,
    ) -> None:
        self._provider = provider
        self._search = search_client or WebSearchClient()
        self._fetch = fetch_client or UrlFetchClient()
        self._max_tool_calls = max_tool_calls
        self._max_rounds = max_rounds
        self._session_factory = session_factory
        # Optional shared research tool belt. When provided, the tools
        # listed in ``extra_tool_names`` are exposed alongside the
        # default web_search + fetch_url. Everything else about the
        # loop (TRADE_TOOL as terminal, budget, forcing path) is
        # unchanged — this just widens the research surface.
        self._toolbelt = toolbelt
        self._extra_tool_names: list[str] = list(extra_tool_names or [])

    async def propose(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int = 1024,
        agent_id: str = "decision",
        purpose: str = "stock_decision",
    ) -> ResearchOutcome:
        bus = get_bus()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        tools = [TRADE_TOOL, WEB_SEARCH_TOOL, FETCH_URL_TOOL]
        if self._toolbelt is not None and self._extra_tool_names:
            extras = self._toolbelt.schemas(
                include=[
                    n for n in self._extra_tool_names
                    if n not in {"web_search", "fetch_url"}
                ],
            )
            tools = tools + extras
        artifacts: list[ResearchArtifact] = []
        aggregate = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        tool_calls_used = 0

        for round_idx in range(self._max_rounds):
            # If we've burned the research budget, switch to the forcing path:
            # strip the research tools so the only option is propose_trade.
            active_tools = tools
            if tool_calls_used >= self._max_tool_calls:
                active_tools = [TRADE_TOOL]

            response = await self._provider.raw_completion(
                messages=messages,
                tools=active_tools,
                max_tokens=max_tokens,
            )
            aggregate["prompt_tokens"] += response.prompt_tokens
            aggregate["completion_tokens"] += response.completion_tokens
            aggregate["total_tokens"] += response.total_tokens

            call_id: int | None = None
            if self._session_factory is not None:
                try:
                    row = await log_usage(
                        self._session_factory,
                        response,
                        purpose=purpose,
                        agent_id=agent_id,
                        round_idx=round_idx,
                        prompt_messages=list(messages),
                    )
                    call_id = row.id
                except Exception:
                    logger.exception("failed to persist llm call row")

            choice = response.raw_response.get("choices", [{}])[0]
            message = choice.get("message") or {}
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                # Provider returned free text — treat as a soft-fail and retry
                # once by nudging; most providers recover on a second pass.
                if round_idx < self._max_rounds - 1:
                    messages.append({
                        "role": "user",
                        "content": "Please call propose_trade now with your decision.",
                    })
                    continue
                raise RuntimeError("agent returned no tool_calls")

            # Persist the assistant message with all tool calls so the
            # follow-up tool results can be attached in order.
            messages.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })

            final_call = None
            for call in tool_calls:
                name = (call.get("function") or {}).get("name")
                if name == "propose_trade":
                    final_call = call
                    break
                # Research tool → execute and feed the result back.
                tool_calls_used += 1
                args_raw = (call.get("function") or {}).get("arguments") or "{}"
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                except Exception:
                    args = {}
                result_text, preview, count = await self._dispatch(name, args, bus)
                artifacts.append(
                    ResearchArtifact(
                        tool=name or "unknown",
                        arguments=args,
                        result_preview=preview,
                        result_count=count,
                    )
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{round_idx}",
                    "name": name,
                    "content": result_text,
                })

            if final_call is not None:
                tool_input = _parse_tool_args(final_call)
                if not tool_input:
                    raise RuntimeError("propose_trade call had empty arguments")
                # Rebuild an AIResponse whose tool_input is the final decision,
                # but whose usage reflects the full loop spend.
                composed = AIResponse(
                    tool_input=tool_input,
                    raw_request={"messages_len": len(messages), "rounds": round_idx + 1},
                    raw_response=response.raw_response,
                    model=response.model,
                    provider=response.provider,
                    prompt_tokens=aggregate["prompt_tokens"],
                    completion_tokens=aggregate["completion_tokens"],
                    total_tokens=aggregate["total_tokens"],
                )
                return ResearchOutcome(
                    response=composed,
                    artifacts=artifacts,
                    aggregate_usage=dict(aggregate),
                )

        raise RuntimeError(
            f"research loop exhausted {self._max_rounds} rounds without propose_trade"
        )

    async def _dispatch(
        self, name: str | None, args: dict[str, Any], bus
    ) -> tuple[str, str, int]:
        """Run one research tool call. Returns (full_text, preview, count)."""
        if name == "web_search":
            query = str(args.get("query") or "").strip()
            top_k = int(args.get("top_k") or 6)
            top_k = max(1, min(top_k, 8))
            if not query:
                return ("{}", "(empty query)", 0)
            results = await self._search.search(query, top_k=top_k)
            payload = {
                "query": query,
                "results": [
                    {"title": r.title, "url": r.url, "snippet": r.snippet}
                    for r in results
                ],
            }
            preview = (
                f"{len(results)} results for {query!r}: "
                + ", ".join(r.title[:60] for r in results[:3])
            )
            bus.publish(
                "research.search",
                f"search {query!r} → {len(results)} results",
                data={"query": query, "result_urls": [r.url for r in results[:5]]},
            )
            return (json.dumps(payload), preview, len(results))

        if name == "fetch_url":
            url = str(args.get("url") or "").strip()
            if not url:
                return ("{}", "(empty url)", 0)
            result = await self._fetch.fetch(url)
            if result is None:
                bus.publish(
                    "research.fetch",
                    f"fetch failed {url}",
                    severity=EventSeverity.WARN,
                    data={"url": url},
                )
                return (
                    json.dumps({"url": url, "error": "fetch failed"}),
                    f"fetch failed: {url}",
                    0,
                )
            payload = {
                "url": result.url,
                "title": result.title,
                "text": result.text,
                "truncated": result.truncated,
            }
            preview = (
                f"{result.title or url}"
                + (" (truncated)" if result.truncated else "")
            )
            bus.publish(
                "research.fetch",
                f"fetched {result.title or url}",
                data={"url": url, "title": result.title, "chars": len(result.text)},
            )
            return (json.dumps(payload), preview, len(result.text))

        # Any tool that isn't web_search or fetch_url must be routed via
        # the shared toolbelt (added at construction via extra_tool_names).
        if self._toolbelt is not None:
            try:
                _, preview, payload = await self._toolbelt.dispatch(name or "", args)
            except Exception as exc:
                logger.warning("toolbelt %s raised", name, exc_info=exc)
                err = {"error": f"{type(exc).__name__}: {exc}"}
                return (json.dumps(err), err["error"], 0)
            # The toolbelt emits a structured payload; serialise it for
            # the model and treat item/result lengths as the "count".
            count = 0
            if isinstance(payload, dict):
                for k in ("results", "items", "filings", "rows", "decisions", "trades"):
                    v = payload.get(k)
                    if isinstance(v, list):
                        count = len(v)
                        break
            bus.publish(
                "research.toolbelt",
                f"{name}: {preview}"[:120],
                data={"tool": name, "args": args, "preview": preview},
            )
            return (json.dumps(payload), preview, count)

        logger.warning("unknown research tool %r", name)
        return (
            json.dumps({"error": f"unknown tool {name}"}),
            f"unknown tool {name}",
            0,
        )


def _parse_tool_args(call: dict[str, Any]) -> dict[str, Any]:
    args_raw = (call.get("function") or {}).get("arguments") or "{}"
    try:
        return json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
    except Exception:
        return {}


__all__ = [
    "ResearchAgent",
    "ResearchArtifact",
    "ResearchOutcome",
    "WEB_SEARCH_TOOL",
    "FETCH_URL_TOOL",
]
