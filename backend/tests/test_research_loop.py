"""Research loop: tool-use branching, budget enforcement, artifact capture."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.ai.llm_provider import AIResponse, LLMProvider
from app.ai.research import FetchResult, SearchResult
from app.ai.research_loop import ResearchAgent, ResearchOutcome


class _FakeProvider:
    """LLMProvider stand-in that replays a scripted sequence of raw responses."""

    def __init__(self, scripted: list[dict[str, Any]]) -> None:
        self._scripted = list(scripted)
        self.calls: list[dict[str, Any]] = []

    @property
    def provider(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    async def raw_completion(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        max_tokens: int = 1024,
        tool_choice: str = "required",
    ) -> AIResponse:
        self.calls.append({"messages": list(messages), "tools": list(tools)})
        raw = self._scripted.pop(0)
        return AIResponse(
            tool_input={},
            raw_request={},
            raw_response=raw,
            model=self.model,
            provider=self.provider,
            prompt_tokens=raw.get("usage", {}).get("prompt_tokens", 0),
            completion_tokens=raw.get("usage", {}).get("completion_tokens", 0),
            total_tokens=raw.get("usage", {}).get("total_tokens", 0),
        )


class _FakeSearch:
    def __init__(self, results: list[SearchResult]) -> None:
        self._results = results
        self.queries: list[str] = []

    async def search(self, query: str, *, top_k: int = 6) -> list[SearchResult]:
        self.queries.append(query)
        return self._results[:top_k]


class _FakeFetch:
    def __init__(self, body: str = "test body") -> None:
        self._body = body
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchResult | None:
        self.urls.append(url)
        return FetchResult(url=url, title="T", text=self._body, truncated=False)


def _raw_tool(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


@pytest.mark.asyncio
async def test_direct_propose_no_research():
    """Model can skip research and go straight to propose_trade."""
    provider = _FakeProvider([
        _raw_tool("c1", "propose_trade", {
            "action": "hold",
            "rationale": "nothing actionable",
            "confidence": 0.4,
        })
    ])
    agent = ResearchAgent(
        provider=provider,  # type: ignore[arg-type]
        search_client=_FakeSearch([]),  # type: ignore[arg-type]
        fetch_client=_FakeFetch(),  # type: ignore[arg-type]
    )
    outcome = await agent.propose(system="s", user="u")
    assert outcome.response.tool_input["action"] == "hold"
    assert outcome.artifacts == []
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_search_then_propose():
    """Model searches, then commits. Artifact is captured."""
    provider = _FakeProvider([
        _raw_tool("s1", "web_search", {"query": "NVDA earnings"}),
        _raw_tool("p1", "propose_trade", {
            "action": "open_long",
            "symbol": "NVDA",
            "size_usd": 100.0,
            "rationale": "earnings beat",
            "confidence": 0.6,
        }),
    ])
    search = _FakeSearch([
        SearchResult(title="NVDA beats", url="https://ex.com/a", snippet="..."),
    ])
    agent = ResearchAgent(
        provider=provider,  # type: ignore[arg-type]
        search_client=search,  # type: ignore[arg-type]
        fetch_client=_FakeFetch(),  # type: ignore[arg-type]
    )
    outcome = await agent.propose(system="s", user="u")
    assert outcome.response.tool_input["action"] == "open_long"
    assert len(outcome.artifacts) == 1
    assert outcome.artifacts[0].tool == "web_search"
    assert search.queries == ["NVDA earnings"]
    # Aggregate usage sums both LLM calls.
    assert outcome.aggregate_usage["total_tokens"] == 30


@pytest.mark.asyncio
async def test_research_loop_persists_per_round_usage():
    """When a session_factory is provided, each raw_completion round
    writes one LlmUsageRow carrying its prompt messages + response body."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.models import Base, LlmUsageRow

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    provider = _FakeProvider([
        _raw_tool("s1", "web_search", {"query": "NVDA earnings"}),
        _raw_tool("p1", "propose_trade", {
            "action": "hold",
            "rationale": "mixed",
            "confidence": 0.3,
        }),
    ])
    agent = ResearchAgent(
        provider=provider,  # type: ignore[arg-type]
        search_client=_FakeSearch([
            SearchResult(title="t", url="u", snippet="s"),
        ]),  # type: ignore[arg-type]
        fetch_client=_FakeFetch(),  # type: ignore[arg-type]
        session_factory=factory,
    )
    outcome = await agent.propose(
        system="s", user="u", agent_id="decision", purpose="stock_decision"
    )
    assert outcome.response.tool_input["action"] == "hold"

    async with factory() as s:
        rows = (
            await s.execute(select(LlmUsageRow).order_by(LlmUsageRow.id))
        ).scalars().all()
    assert len(rows) == 2
    assert [r.round_idx for r in rows] == [0, 1]
    assert all(r.agent_id == "decision" for r in rows)
    assert all(r.purpose == "stock_decision" for r in rows)
    # Prompt messages grew between rounds (assistant + tool appended).
    assert len(rows[0].prompt_messages) < len(rows[1].prompt_messages)
    # Response bodies are the scripted raw responses.
    assert rows[0].response_body["choices"][0]["message"]["tool_calls"][0][
        "function"
    ]["name"] == "web_search"
    await engine.dispose()


@pytest.mark.asyncio
async def test_tool_budget_forces_final_decision():
    """After max_tool_calls searches, research tools are stripped and the
    model must commit."""
    provider = _FakeProvider([
        _raw_tool("s1", "web_search", {"query": "q1"}),
        _raw_tool("s2", "web_search", {"query": "q2"}),
        # Budget now exhausted (limit=2); next call must pass TRADE_TOOL only.
        _raw_tool("p1", "propose_trade", {
            "action": "hold",
            "rationale": "budget forced",
            "confidence": 0.3,
        }),
    ])
    search = _FakeSearch([
        SearchResult(title="t", url="u", snippet="s"),
    ])
    agent = ResearchAgent(
        provider=provider,  # type: ignore[arg-type]
        search_client=search,  # type: ignore[arg-type]
        fetch_client=_FakeFetch(),  # type: ignore[arg-type]
        max_tool_calls=2,
    )
    outcome = await agent.propose(system="s", user="u")
    assert outcome.response.tool_input["rationale"] == "budget forced"
    assert len(outcome.artifacts) == 2
    # The third (forcing) call should have only propose_trade in its tools list.
    final_tools = provider.calls[-1]["tools"]
    assert len(final_tools) == 1
    assert final_tools[0]["function"]["name"] == "propose_trade"
