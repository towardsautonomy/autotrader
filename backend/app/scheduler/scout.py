"""Scout loop — fast-cadence discovery that feeds the decision queue.

Runs on its own interval (default: 2 min). Pulls movers + screener
shortlist, pushes fresh candidates onto a shared CandidateQueue. The
decision loop drains that queue on its own cadence.

Phase-B will layer an LLM ScoutAgent on top (emit_candidates tool call)
that scores and filters before pushing — but the plumbing is the same.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import EventSeverity, get_bus
from app.ai.scout_agent import ScoutAgent
from app.ai.trace import cycle_scope, new_cycle_id
from app.clock import is_us_equities_regular_session
from app.market_data import MoversClient, Screener
from app.models import Trade, TradeStatus

from .budget import budget_exceeded
from .candidate_queue import CandidateQueue, ScoutCandidate
from .snapshot import agents_paused, pause_when_market_closed

logger = logging.getLogger(__name__)


class ScoutLoop:
    """A lightweight scan → queue loop. Does no LLM calls yet; a later
    phase wires in a scoring agent."""

    def __init__(
        self,
        *,
        queue: CandidateQueue,
        movers_client: MoversClient | None = None,
        screener: Screener | None = None,
        per_bucket: int = 5,
        screener_top_k: int = 10,
        session_factory: async_sessionmaker | None = None,
        daily_llm_budget_usd: float = 0.0,
        market_label: str = "stocks",
        llm_agent: ScoutAgent | None = None,
        respect_market_hours: bool = False,
    ) -> None:
        self._queue = queue
        self._movers = movers_client
        self._screener = screener
        self._per_bucket = per_bucket
        self._screener_top_k = screener_top_k
        self._session_factory = session_factory
        self._budget_ceiling = daily_llm_budget_usd
        self._market = market_label
        self._llm_agent = llm_agent
        self._respect_market_hours = respect_market_hours

    @property
    def market_label(self) -> str:
        return self._market

    async def tick(self) -> int:
        bus = get_bus()

        if self._session_factory is not None and await agents_paused(
            self._session_factory
        ):
            bus.publish(
                "scout.skipped_paused",
                f"[{self._market}] agents paused — scout skipped",
            )
            return 0

        if (
            self._session_factory is not None
            and await pause_when_market_closed(self._session_factory)
            and not is_us_equities_regular_session()
        ):
            bus.publish(
                "scout.skipped_market_closed",
                f"[{self._market}] market closed — scout idle (auto)",
            )
            return 0

        if self._respect_market_hours and not is_us_equities_regular_session():
            bus.publish(
                "scout.skipped_market_closed",
                f"[{self._market}] market closed — scout skipped",
            )
            return 0

        if self._session_factory is not None and self._budget_ceiling > 0:
            over, spent = await budget_exceeded(
                self._session_factory, ceiling_usd=self._budget_ceiling
            )
            if over:
                bus.publish(
                    "scout.skipped_budget",
                    f"daily LLM spend ${spent:.4f} ≥ cap ${self._budget_ceiling:.2f}",
                    severity=EventSeverity.WARN,
                    data={"spent_usd": spent, "ceiling_usd": self._budget_ceiling},
                )
                return 0

        bus.publish("scout.started", f"[{self._market}] scout scan started")

        picks: list[ScoutCandidate] = []

        if self._movers and self._movers.enabled:
            try:
                movers = await self._movers.fetch()
                for m in (movers.gainers or [])[: self._per_bucket]:
                    pct = m.percent_change
                    picks.append(
                        ScoutCandidate(
                            symbol=m.symbol,
                            source="mover_gainer",
                            note=f"+{pct * 100:.2f}%" if pct is not None else "gainer",
                            score=abs(pct) if pct is not None else None,
                        )
                    )
                for m in (movers.losers or [])[: self._per_bucket]:
                    pct = m.percent_change
                    picks.append(
                        ScoutCandidate(
                            symbol=m.symbol,
                            source="mover_loser",
                            note=f"{pct * 100:.2f}%" if pct is not None else "loser",
                            score=abs(pct) if pct is not None else None,
                        )
                    )
                for m in (movers.most_active or [])[: self._per_bucket]:
                    picks.append(
                        ScoutCandidate(
                            symbol=m.symbol,
                            source="mover_active",
                            note="high volume",
                        )
                    )
            except Exception:
                logger.warning("scout movers fetch failed", exc_info=True)

        if self._screener and self._screener.enabled:
            try:
                snap = await self._screener.shortlist(top_k=self._screener_top_k)
                for c in snap.candidates or []:
                    picks.append(
                        ScoutCandidate(
                            symbol=c.symbol,
                            source="screener",
                            note=(
                                f"vol {c.vol_ratio:.1f}x, "
                                f"{c.pct_change * 100:+.2f}%, "
                                f"gap {c.gap_pct * 100:+.2f}%"
                            ),
                            score=c.vol_ratio,
                        )
                    )
            except Exception:
                logger.warning("scout screener fetch failed", exc_info=True)

        held_symbols = await self._fetch_held_symbols()
        if held_symbols:
            picks = [p for p in picks if p.symbol.upper() not in held_symbols]

        if not picks:
            bus.publish(
                "scout.candidates_found",
                f"[{self._market}] no scout candidates this cycle",
                data={"count": 0},
            )
            return 0

        if self._llm_agent is not None:
            scout_cycle_id = new_cycle_id().replace("cyc-", "scout-", 1)
            try:
                with cycle_scope(scout_cycle_id):
                    result = await self._llm_agent.score(
                        raw_candidates=[
                            {"symbol": p.symbol, "source": p.source, "note": p.note}
                            for p in picks
                        ],
                        market_note="",
                        held_symbols=sorted(held_symbols),
                    )
                    if result.picks:
                        by_symbol = {p.symbol: p for p in picks}
                        llm_picks: list[ScoutCandidate] = []
                        for pk in result.picks:
                            if pk.symbol.upper() in held_symbols:
                                continue
                            base = by_symbol.get(pk.symbol)
                            llm_picks.append(
                                ScoutCandidate(
                                    symbol=pk.symbol,
                                    source=f"llm:{base.source if base else 'scan'}",
                                    note=pk.reason or (base.note if base else ""),
                                    score=pk.score,
                                )
                            )
                        picks = llm_picks
            except Exception:
                logger.warning("scout LLM refinement failed", exc_info=True)

        await self._queue.push_many(picks)
        size = await self._queue.size()

        top_preview = [{"symbol": c.symbol, "source": c.source} for c in picks[:8]]
        bus.publish(
            "scout.candidates_found",
            (
                f"[{self._market}] scout added {len(picks)} candidates "
                f"(queue={size})"
            ),
            severity=EventSeverity.SUCCESS,
            data={
                "added": len(picks),
                "queue_size": size,
                "top": top_preview,
            },
        )
        return len(picks)

    async def _fetch_held_symbols(self) -> set[str]:
        """Underlying tickers we currently hold on this market — scout is
        told to skip these so we don't propose duplicates."""
        if self._session_factory is None:
            return set()
        try:
            async with self._session_factory() as session:
                rows = (
                    await session.execute(
                        select(Trade.symbol).where(
                            Trade.market == self._market,
                            Trade.status == TradeStatus.OPEN,
                        )
                    )
                ).scalars().all()
                return {s.upper() for s in rows if s}
        except Exception:
            logger.warning("scout held-symbols lookup failed", exc_info=True)
            return set()
