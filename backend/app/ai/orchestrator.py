"""Multi-agent orchestrator: fan-out research → fan-in decision.

One "research agent" is spawned per focus symbol. Each agent uses the
same research tools (web_search / fetch_url) but with a narrow prompt —
"given this symbol and context, produce a structured finding." All
agents run concurrently. The orchestrator then hands the collected
findings to the existing decision flow.

Activity bus events per agent:
  - ``agent.started``   — agent spun up, symbol + role
  - ``agent.progress``  — tool calls or intermediate step
  - ``agent.done``      — finding captured (or failure)

These events are what the UI renders as agent cards; they're identical
in shape to the existing ai.* events so the activity log absorbs them
for free.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import EventSeverity, get_bus
from app.ai.llm_provider import AIResponse, LLMProvider
from app.ai.research_loop import (
    FETCH_URL_TOOL,
    WEB_SEARCH_TOOL,
    ResearchAgent,
    ResearchArtifact,
)
from app.ai.research_toolbelt import ResearchToolbelt
from app.ai.tools import PROPOSE_STRUCTURE_TOOL
from app.ai.usage import log_usage

logger = logging.getLogger(__name__)


REPORT_FINDING_TOOL = {
    "type": "function",
    "function": {
        "name": "report_finding",
        "description": (
            "Commit your structured finding for this symbol. Call this exactly "
            "once when you have enough context. If the catalyst is weak or "
            "ambiguous, still report — the decision agent weighs confidence."
        ),
        "parameters": {
            "type": "object",
            "required": ["symbol", "bias", "confidence", "summary"],
            "properties": {
                "symbol": {"type": "string"},
                "bias": {
                    "type": "string",
                    "enum": ["bullish", "bearish", "neutral", "avoid"],
                    "description": (
                        "Directional lean. 'avoid' means no edge worth trading."
                    ),
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "catalyst": {
                    "type": "string",
                    "description": "Specific news / price action driving the lean.",
                },
                "risks": {
                    "type": "string",
                    "description": "What could invalidate the thesis.",
                },
                "summary": {
                    "type": "string",
                    "description": "Two to four sentences the decision agent will read.",
                },
            },
        },
    },
}


@dataclass
class AgentFinding:
    symbol: str
    bias: str
    confidence: float
    catalyst: str
    risks: str
    summary: str
    artifacts: list[ResearchArtifact]
    elapsed_sec: float
    error: str | None = None
    # Optional: if the specialist emitted propose_structure, the full
    # structure payload lands here so the decision agent can read it.
    structure: dict[str, Any] | None = None


@dataclass
class OrchestrationResult:
    findings: list[AgentFinding]
    aggregate_usage: dict[str, int]


_RESEARCH_SYSTEM = (
    "You are a short-term OPTIONS trading specialist for ONE symbol. Use "
    "web_search and fetch_url to nail the catalyst (earnings, guidance, "
    "M&A, analyst notes, flow, IV regime). Be terse — tight tool-call "
    "budget. Then commit ONE of:\n"
    "  · propose_structure  — preferred. Name the exact options structure, "
    "legs, direction, max loss/profit, and rationale. Defined-risk only.\n"
    "  · report_finding     — fallback if you can't land a concrete "
    "structure; just capture bias + confidence + summary.\n"
    "Call one of those exactly once when ready."
)


class Orchestrator:
    def __init__(
        self,
        *,
        provider: LLMProvider,
        research_agent: ResearchAgent,
        focus_count: int = 3,
        per_agent_max_tool_calls: int = 6,
        per_agent_max_rounds: int = 8,
        session_factory: async_sessionmaker | None = None,
        toolbelt: ResearchToolbelt | None = None,
        extra_tool_names: list[str] | None = None,
    ) -> None:
        self._provider = provider
        self._research_agent = research_agent
        self._focus_count = focus_count
        self._per_agent_max_tool_calls = per_agent_max_tool_calls
        self._per_agent_max_rounds = per_agent_max_rounds
        self._session_factory = session_factory
        # When provided, per-symbol research agents gain the listed
        # extra tools (on top of web_search + fetch_url + the terminal
        # report_finding / propose_structure tools).
        self._toolbelt = toolbelt
        self._extra_tool_names: list[str] = list(extra_tool_names or [])

    def pick_focus(self, ordered_symbols: list[str]) -> list[str]:
        """Deterministic focus pick: take the top N distinct symbols from
        the pre-ordered candidate list (positions + discovery + shortlist).
        No heuristic — the ordering already reflects the
        caller's priority."""
        return list(dict.fromkeys(ordered_symbols))[: self._focus_count]

    async def orchestrate(
        self,
        *,
        symbols: list[str],
        per_symbol_context: dict[str, str],
    ) -> OrchestrationResult:
        bus = get_bus()
        focus = self.pick_focus(symbols)
        if not focus:
            return OrchestrationResult(findings=[], aggregate_usage={})

        bus.publish(
            "agent.fanout",
            f"spawning {len(focus)} research agents",
            data={"symbols": focus},
        )

        tasks = [
            self._run_one(sym, per_symbol_context.get(sym, ""))
            for sym in focus
        ]
        findings = await asyncio.gather(*tasks, return_exceptions=False)

        agg = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        for f in findings:
            for a in f.artifacts:
                # Research artifacts don't carry token counts themselves —
                # aggregation already handled inside ResearchAgent; leave
                # aggregate_usage reserved for future use.
                _ = a

        bus.publish(
            "agent.fanin",
            f"collected {len(findings)} findings",
            data={
                "findings": [
                    {"symbol": f.symbol, "bias": f.bias, "confidence": f.confidence}
                    for f in findings
                ]
            },
        )
        return OrchestrationResult(findings=list(findings), aggregate_usage=agg)

    async def _run_one(self, symbol: str, context_blob: str) -> AgentFinding:
        bus = get_bus()
        agent_id = f"research-{symbol.lower()}"
        started = time.monotonic()
        bus.publish(
            "agent.started",
            f"research agent for {symbol} online",
            data={"agent_id": agent_id, "role": "research", "symbol": symbol},
        )

        user_msg = (
            f"Symbol: {symbol}\n\n"
            f"Context from the trading cycle:\n{context_blob or '(none)'}"
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _RESEARCH_SYSTEM},
            {"role": "user", "content": user_msg},
        ]
        tools = [
            REPORT_FINDING_TOOL,
            PROPOSE_STRUCTURE_TOOL,
            WEB_SEARCH_TOOL,
            FETCH_URL_TOOL,
        ]
        if self._toolbelt is not None and self._extra_tool_names:
            extras = self._toolbelt.schemas(
                include=[
                    n for n in self._extra_tool_names
                    if n not in {"web_search", "fetch_url"}
                ],
            )
            tools = tools + extras
        terminal_tools = [REPORT_FINDING_TOOL, PROPOSE_STRUCTURE_TOOL]
        terminal_names = {"report_finding", "propose_structure"}
        artifacts: list[ResearchArtifact] = []
        tool_calls_used = 0

        try:
            for round_idx in range(self._per_agent_max_rounds):
                active_tools = tools
                if tool_calls_used >= self._per_agent_max_tool_calls:
                    active_tools = terminal_tools

                response: AIResponse = await self._provider.raw_completion(
                    messages=messages,
                    tools=active_tools,
                )

                call_id: int | None = None
                if self._session_factory is not None:
                    try:
                        row = await log_usage(
                            self._session_factory,
                            response,
                            purpose="research_agent",
                            agent_id=agent_id,
                            round_idx=round_idx,
                            prompt_messages=list(messages),
                        )
                        call_id = row.id
                    except Exception:
                        logger.exception("failed to persist llm call row")

                choice = response.raw_response.get("choices", [{}])[0]
                msg = choice.get("message") or {}
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    messages.append({
                        "role": "user",
                        "content": "Please call report_finding now.",
                    })
                    continue

                messages.append({
                    "role": "assistant",
                    "content": msg.get("content") or "",
                    "tool_calls": tool_calls,
                })

                final_call = None
                final_name: str | None = None
                for call in tool_calls:
                    name = (call.get("function") or {}).get("name")
                    if name in terminal_names:
                        final_call = call
                        final_name = name
                        break
                    tool_calls_used += 1
                    args_raw = (call.get("function") or {}).get("arguments") or "{}"
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                    except Exception:
                        args = {}
                    result_text, preview, count = await self._research_agent._dispatch(
                        name, args, bus
                    )
                    artifacts.append(
                        ResearchArtifact(
                            tool=name or "unknown",
                            arguments=args,
                            result_preview=preview,
                            result_count=count,
                        )
                    )
                    bus.publish(
                        "agent.progress",
                        f"[{agent_id}] {name}: {preview[:80]}",
                        data={
                            "agent_id": agent_id,
                            "tool": name,
                            "preview": preview,
                            "call_id": call_id,
                            "round_idx": round_idx,
                        },
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{round_idx}",
                        "name": name,
                        "content": result_text,
                    })

                if final_call is not None:
                    args_raw = (final_call.get("function") or {}).get("arguments") or "{}"
                    try:
                        payload = (
                            json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw)
                        )
                    except Exception:
                        payload = {}
                    elapsed = time.monotonic() - started
                    is_structure = final_name == "propose_structure"
                    bias = str(
                        payload.get("direction" if is_structure else "bias")
                        or "neutral"
                    )
                    summary = str(
                        payload.get("rationale" if is_structure else "summary")
                        or ""
                    )
                    finding = AgentFinding(
                        symbol=str(payload.get("symbol") or symbol).upper(),
                        bias=bias,
                        confidence=float(payload.get("confidence") or 0.0),
                        catalyst=str(payload.get("catalyst") or ""),
                        risks=str(payload.get("risks") or ""),
                        summary=summary,
                        artifacts=artifacts,
                        elapsed_sec=elapsed,
                        structure=payload if is_structure else None,
                    )
                    structure_summary = None
                    if finding.structure:
                        structure_summary = {
                            "structure": finding.structure.get("structure"),
                            "max_loss_usd": finding.structure.get("max_loss_usd"),
                            "max_profit_usd": finding.structure.get("max_profit_usd"),
                            "entry_price_estimate": finding.structure.get(
                                "entry_price_estimate"
                            ),
                        }
                    bus.publish(
                        "agent.done",
                        f"[{agent_id}] {finding.bias} "
                        f"(conf {finding.confidence:.2f}) in {elapsed:.1f}s",
                        data={
                            "agent_id": agent_id,
                            "symbol": finding.symbol,
                            "bias": finding.bias,
                            "confidence": finding.confidence,
                            "artifact_count": len(artifacts),
                            "elapsed_sec": elapsed,
                            "final_call_id": call_id,
                            "structure": structure_summary,
                        },
                    )
                    return finding

            raise RuntimeError("agent exhausted rounds without a terminal tool")

        except Exception as exc:
            elapsed = time.monotonic() - started
            bus.publish(
                "agent.failed",
                f"[{agent_id}] {exc}",
                severity=EventSeverity.WARN,
                data={"agent_id": agent_id, "symbol": symbol, "error": str(exc)},
            )
            return AgentFinding(
                symbol=symbol,
                bias="neutral",
                confidence=0.0,
                catalyst="",
                risks="",
                summary=f"research failed: {exc}",
                artifacts=artifacts,
                elapsed_sec=elapsed,
                error=str(exc),
            )


def findings_to_prompt_block(findings: list[AgentFinding]) -> str:
    """Render findings as a markdown-ish block for injection into the
    decision agent's user message."""
    if not findings:
        return "  (no research agents dispatched this cycle)"
    lines: list[str] = []
    for f in findings:
        line = (
            f"  · {f.symbol}: {f.bias.upper()} (conf {f.confidence:.2f}) — {f.summary}"
        )
        if f.structure:
            struct_name = f.structure.get("structure") or "?"
            max_loss = f.structure.get("max_loss_usd")
            max_profit = f.structure.get("max_profit_usd")
            entry = f.structure.get("entry_price_estimate")
            parts = [f"structure: {struct_name}"]
            if max_loss is not None:
                parts.append(f"max_loss ${max_loss}")
            if max_profit is not None:
                parts.append(f"max_profit ${max_profit}")
            if entry is not None:
                parts.append(f"entry ${entry}")
            line += f"\n      {' · '.join(parts)}"
            legs = f.structure.get("legs") or []
            if legs:
                leg_lines = []
                for leg in legs[:6]:
                    side = leg.get("side", "?")
                    right = leg.get("right", "?")
                    strike = leg.get("strike")
                    expiry = leg.get("expiry", "?")
                    qty = leg.get("quantity", 1)
                    leg_lines.append(
                        f"{side} {qty} {right} {strike} {expiry}".strip()
                    )
                line += "\n      legs: " + "; ".join(leg_lines)
        if f.catalyst:
            line += f"\n      catalyst: {f.catalyst}"
        if f.risks:
            line += f"\n      risks: {f.risks}"
        if f.error:
            line += f"\n      [agent error: {f.error}]"
        lines.append(line)
    return "\n".join(lines)


__all__ = [
    "AgentFinding",
    "OrchestrationResult",
    "Orchestrator",
    "REPORT_FINDING_TOOL",
    "findings_to_prompt_block",
]
