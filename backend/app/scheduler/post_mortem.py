"""Post-mortem loop — runs one LLM call per just-closed trade.

Polls for trades in status=CLOSED with post_mortem_done=False, builds a
compact summary, and asks the PostMortemAgent for a structured lesson.
The result is saved to ``trade_post_mortems`` and the Trade is flagged
done so the loop doesn't reprocess it.

Runs independently of trading cadence — loss-leader for future prompts.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.activity import EventSeverity, get_bus
from app.ai.post_mortem_agent import PostMortemAgent, TradeSummary
from app.models import Decision, Trade, TradePostMortem, TradeStatus

from .budget import budget_exceeded

logger = logging.getLogger(__name__)


class PostMortemLoop:
    def __init__(
        self,
        *,
        agent: PostMortemAgent,
        session_factory: async_sessionmaker,
        daily_llm_budget_usd: float = 0.0,
        batch_size: int = 3,
        market_label: str = "stocks",
    ) -> None:
        self._agent = agent
        self._session_factory = session_factory
        self._budget_ceiling = daily_llm_budget_usd
        self._batch_size = batch_size
        self._market = market_label

    @property
    def market_label(self) -> str:
        return self._market

    async def tick(self) -> int:
        bus = get_bus()
        if self._budget_ceiling > 0 and await budget_exceeded(
            self._session_factory, self._budget_ceiling
        ):
            return 0

        async with self._session_factory() as session:
            pending = (
                await session.execute(
                    select(Trade)
                    .where(
                        Trade.status == TradeStatus.CLOSED,
                        Trade.post_mortem_done.is_(False),
                        Trade.market == self._market,
                    )
                    .order_by(Trade.closed_at.desc())
                    .limit(self._batch_size)
                )
            ).scalars().all()

        if not pending:
            return 0

        acted = 0
        for trade in pending:
            summary = await self._build_summary(trade)
            outcome = await self._agent.review(summary)
            if outcome.error is not None or not outcome.lesson:
                async with self._session_factory() as session:
                    live = await session.get(Trade, trade.id)
                    if live is not None:
                        live.post_mortem_done = True
                        session.add(live)
                        await session.commit()
                continue

            async with self._session_factory() as session:
                session.add(
                    TradePostMortem(
                        trade_id=trade.id,
                        symbol=trade.symbol,
                        verdict=outcome.verdict,
                        lesson=outcome.lesson,
                        realized_pnl_usd=trade.realized_pnl_usd or 0.0,
                        call_id=outcome.call_id,
                    )
                )
                live = await session.get(Trade, trade.id)
                if live is not None:
                    live.post_mortem_done = True
                    session.add(live)
                await session.commit()

            bus.publish(
                "post_mortem.saved",
                f"[{trade.symbol}] {outcome.verdict} — {outcome.lesson[:120]}",
                severity=EventSeverity.INFO,
                data={
                    "trade_id": trade.id,
                    "symbol": trade.symbol,
                    "verdict": outcome.verdict,
                    "pnl_usd": trade.realized_pnl_usd,
                },
            )
            acted += 1

        return acted

    async def _build_summary(self, trade: Trade) -> TradeSummary:
        entry_rationale: str | None = None
        if trade.decision_id is not None:
            async with self._session_factory() as session:
                decision = await session.get(Decision, trade.decision_id)
                if decision is not None and decision.rationale:
                    entry_rationale = decision.rationale
        hold_minutes: float | None = None
        if trade.opened_at is not None and trade.closed_at is not None:
            hold_minutes = (
                trade.closed_at - trade.opened_at
            ).total_seconds() / 60.0
        structure = None
        if trade.option_json and isinstance(trade.option_json, dict):
            structure = trade.option_json.get("structure")
        return TradeSummary(
            trade_id=trade.id,
            symbol=trade.symbol,
            action=trade.action,
            size_usd=trade.size_usd,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            stop_loss_pct=trade.stop_loss_pct,
            take_profit_pct=trade.take_profit_pct,
            realized_pnl_usd=trade.realized_pnl_usd or 0.0,
            hold_minutes=hold_minutes,
            option_structure=structure,
            entry_rationale=entry_rationale,
        )


__all__ = ["PostMortemLoop"]
