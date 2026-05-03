"""API routes for the researcher chat.

Endpoints:
- ``GET  /research/conversations``                  — list threads
- ``GET  /research/conversations/{id}``              — full thread
- ``GET  /research/conversations/{id}/stream`` (SSE) — resume tailing an
  in-flight run after reconnect
- ``POST /research/conversations``                   — create new thread
- ``DELETE /research/conversations/{id}``            — delete a thread
- ``POST /research/chat`` (SSE stream)               — send a message; the
  agent loop runs detached from this response, so the run survives
  socket drops / tab close / laptop sleep. Reconnect via the
  ``/stream`` endpoint above to pick up where you left off.

Auth on the SSE endpoint uses ``?api_key=`` on the querystring because
EventSource cannot set custom headers — matches the existing activity
stream route.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.llm_provider import build_provider_from_settings
from app.ai.research import UrlFetchClient, WebSearchClient
from app.ai.research_runs import (
    ResearchRun,
    get_run,
    start_run,
    subscribe,
)
from app.ai.research_toolbelt import ResearchToolbelt
from app.ai.researcher import ResearcherAgent
from app.api.deps import get_db, require_api_key
from app.config import Settings, get_settings
from app.db import get_session_factory
from app.market_data.finnhub import FinnhubClient
from app.models import ResearchConversation, ResearchMessage

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Schemas -----------------------------------------------------------------


class ResearchMessageOut(BaseModel):
    id: int
    role: str
    content: str
    tool_name: str | None = None
    tool_payload: dict | None = None
    created_at: str


class ResearchConversationSummary(BaseModel):
    id: int
    title: str
    created_at: str
    message_count: int


class ResearchConversationDetail(BaseModel):
    id: int
    title: str
    created_at: str
    messages: list[ResearchMessageOut]


class CreateConversationIn(BaseModel):
    title: str | None = None


class ChatIn(BaseModel):
    conversation_id: int | None = None
    message: str
    # Optional title hint for newly-created threads.
    title: str | None = None


# --- Agent singleton ---------------------------------------------------------

_agent_cache: ResearcherAgent | None = None
_agent_key: tuple[str, str, str, str] | None = None


def _get_agent(settings: Settings) -> ResearcherAgent:
    """Lazy singleton. Rebuilds when credentials rotate."""
    global _agent_cache, _agent_key
    key = (
        settings.ai_provider or "",
        settings.openrouter_api_key or "",
        settings.finnhub_api_key or "",
        settings.alpaca_api_key or "",
    )
    if _agent_cache is not None and _agent_key == key:
        return _agent_cache

    provider = build_provider_from_settings(settings)
    finnhub = (
        FinnhubClient(settings.finnhub_api_key)
        if settings.finnhub_api_key
        else None
    )
    factory = get_session_factory()
    toolbelt = ResearchToolbelt(
        finnhub=finnhub,
        search=WebSearchClient(
            tavily_api_key=settings.tavily_api_key,
            brave_api_key=settings.brave_search_api_key,
            serper_api_key=settings.serper_api_key,
        ),
        fetch=UrlFetchClient(),
        alpaca_api_key=(
            settings.alpaca_api_key
            if "replace_me" not in settings.alpaca_api_key
            else None
        ),
        alpaca_api_secret=(
            settings.alpaca_api_secret
            if "replace_me" not in settings.alpaca_api_secret
            else None
        ),
        alpaca_data_url=settings.alpaca_data_url,
        session_factory=factory,
    )
    agent = ResearcherAgent(
        provider=provider,
        session_factory=factory,
        toolbelt=toolbelt,
        tool_result_chars=settings.research_tool_result_chars,
    )
    _agent_cache = agent
    _agent_key = key
    return agent


# --- CRUD --------------------------------------------------------------------


@router.get(
    "/research/conversations",
    response_model=list[ResearchConversationSummary],
    dependencies=[Depends(require_api_key)],
)
async def list_conversations(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(ResearchConversation).order_by(desc(ResearchConversation.id))
        )
    ).scalars().all()
    out: list[ResearchConversationSummary] = []
    for c in rows:
        count = (
            await db.execute(
                select(ResearchMessage.id).where(
                    ResearchMessage.conversation_id == c.id
                )
            )
        ).scalars().all()
        out.append(
            ResearchConversationSummary(
                id=c.id,
                title=c.title or _fallback_title(c.id),
                created_at=c.created_at.isoformat(),
                message_count=len(count),
            )
        )
    return out


@router.get(
    "/research/conversations/{conv_id}",
    response_model=ResearchConversationDetail,
    dependencies=[Depends(require_api_key)],
)
async def get_conversation(conv_id: int, db: AsyncSession = Depends(get_db)):
    conv = await db.get(ResearchConversation, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    msgs = (
        await db.execute(
            select(ResearchMessage)
            .where(ResearchMessage.conversation_id == conv_id)
            .order_by(ResearchMessage.id)
        )
    ).scalars().all()
    return ResearchConversationDetail(
        id=conv.id,
        title=conv.title or _fallback_title(conv.id),
        created_at=conv.created_at.isoformat(),
        messages=[
            ResearchMessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                tool_name=m.tool_name,
                tool_payload=m.tool_payload,
                created_at=m.created_at.isoformat(),
            )
            for m in msgs
        ],
    )


@router.post(
    "/research/conversations",
    response_model=ResearchConversationSummary,
    dependencies=[Depends(require_api_key)],
)
async def create_conversation(
    payload: CreateConversationIn, db: AsyncSession = Depends(get_db)
):
    conv = ResearchConversation(title=(payload.title or "").strip()[:200])
    db.add(conv)
    await db.commit()
    await db.refresh(conv)
    return ResearchConversationSummary(
        id=conv.id,
        title=conv.title or _fallback_title(conv.id),
        created_at=conv.created_at.isoformat(),
        message_count=0,
    )


@router.delete(
    "/research/conversations/{conv_id}",
    dependencies=[Depends(require_api_key)],
)
async def delete_conversation(conv_id: int, db: AsyncSession = Depends(get_db)):
    conv = await db.get(ResearchConversation, conv_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="conversation not found")
    await db.execute(
        delete(ResearchMessage).where(ResearchMessage.conversation_id == conv_id)
    )
    await db.delete(conv)
    await db.commit()
    return {"ok": True}


# --- Chat (SSE) --------------------------------------------------------------


# How often the SSE generator emits a keepalive comment frame. Idle
# intermediaries (Cloudflare, nginx default, home routers) drop TCP
# connections after ~30–60s of silence; the research loop can sit on a
# single LLM call for minutes. Keep the comment cheap (1 byte-ish).
_KEEPALIVE_INTERVAL_SEC = 20.0


@router.post("/research/chat")
async def chat(
    payload: ChatIn,
    api_key: str = Query(""),
):
    """Start a detached research run and stream its events.

    The agent loop runs as a background task; this response is just a
    consumer of the run's event stream. Disconnecting the socket
    (browser tab closed, laptop slept, network dropped) deregisters
    this listener but leaves the run going. The client can reconnect
    via ``GET /research/conversations/{id}/stream`` to resume.

    Auth via ``?api_key=`` on the querystring — SSE can't set headers.
    """
    settings = get_settings()
    if not api_key or api_key != settings.jwt_secret:
        raise HTTPException(status_code=401, detail="invalid api key")

    message = (payload.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")

    factory = get_session_factory()

    # Resolve or create the conversation before starting the run so the
    # client always gets a conversation_id back in the first event.
    async with factory() as session:
        if payload.conversation_id is not None:
            conv = await session.get(
                ResearchConversation, payload.conversation_id
            )
            if conv is None:
                raise HTTPException(
                    status_code=404, detail="conversation not found"
                )
        else:
            conv = ResearchConversation(
                title=(payload.title or message)[:200].strip()
            )
            session.add(conv)
            await session.commit()
            await session.refresh(conv)
        conv_id = conv.id
        conv_title = conv.title or _fallback_title(conv_id)

        prior = (
            await session.execute(
                select(ResearchMessage)
                .where(ResearchMessage.conversation_id == conv_id)
                .order_by(ResearchMessage.id)
            )
        ).scalars().all()
        prior_openai = _to_openai_messages(prior)

    agent = _get_agent(settings)
    run = await start_run(
        agent=agent,
        conversation_id=conv_id,
        title=conv_title,
        prior_messages=prior_openai,
        user_message=message,
    )

    return StreamingResponse(
        _stream_run(run, after_seq=0),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/research/conversations/{conv_id}/stream")
async def resume_stream(
    conv_id: int,
    api_key: str = Query(""),
    after_seq: int = Query(0, ge=0),
):
    """Resume tailing an in-flight (or recently finished) research run.

    Use after a network drop / tab reload to pick up where the prior
    stream left off. Pass ``after_seq`` = the last event sequence number
    the client received to avoid replaying the whole history.

    If no run is active for this conversation, returns 404 — the client
    should fall back to reading persisted messages via
    ``GET /research/conversations/{id}``.
    """
    settings = get_settings()
    if not api_key or api_key != settings.jwt_secret:
        raise HTTPException(status_code=401, detail="invalid api key")

    run = get_run(conv_id)
    if run is None:
        raise HTTPException(
            status_code=404, detail="no active run for this conversation"
        )

    return StreamingResponse(
        _stream_run(run, after_seq=after_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _stream_run(run: ResearchRun, *, after_seq: int):
    """SSE generator over a run's event stream.

    Interleaves live events with periodic keepalive comments so idle
    proxies don't close the socket during long LLM calls.
    """
    # Merge the subscription iterator with a keepalive ticker so we can
    # emit comment frames while waiting for the next event.
    agen = subscribe(run, after_seq=after_seq)
    try:
        # Pre-emit a comment so the client (and any intermediary) sees
        # traffic immediately and doesn't buffer.
        yield ": stream-open\n\n"

        next_task: asyncio.Task[Any] | None = None
        try:
            while True:
                if next_task is None:
                    next_task = asyncio.create_task(agen.__anext__())
                try:
                    se = await asyncio.wait_for(
                        asyncio.shield(next_task),
                        timeout=_KEEPALIVE_INTERVAL_SEC,
                    )
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                except StopAsyncIteration:
                    next_task = None
                    break
                next_task = None
                yield _sse_seq(se.seq, se.event.type, se.event.data)
        finally:
            if next_task is not None and not next_task.done():
                next_task.cancel()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("SSE streamer failed")
        yield _sse("error", {"error": f"{type(exc).__name__}: {exc}"})
    finally:
        await agen.aclose()


# --- Helpers -----------------------------------------------------------------


def _sse(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _sse_seq(seq: int, event_type: str, data: dict[str, Any]) -> str:
    """SSE frame with the event sequence number baked into ``id:`` and
    the payload so resuming clients can pass ``after_seq`` reliably.
    """
    data_with_seq = {**data, "_seq": seq}
    return (
        f"id: {seq}\n"
        f"event: {event_type}\n"
        f"data: {json.dumps(data_with_seq)}\n\n"
    )


def _fallback_title(conv_id: int) -> str:
    return f"thread #{conv_id}"


def _to_openai_messages(
    rows: list[ResearchMessage],
) -> list[dict[str, Any]]:
    """Reconstruct the OpenAI-style message history from persisted rows.

    We only replay user + assistant turns; tool_call / tool_result rows
    are logged for UI display but aren't re-sent to the model — the
    assistant's natural-language response captures the conclusion, and
    re-sending tool traffic would balloon token spend.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.role == "user":
            out.append({"role": "user", "content": r.content})
        elif r.role == "assistant" and r.content:
            out.append({"role": "assistant", "content": r.content})
    return out


__all__ = ["router"]
