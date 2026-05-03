"""APScheduler wiring — starts the TradingLoop and RuntimeMonitor on their
configured cadences. Respects market hours (for stocks adapter)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.activity import EventSeverity, get_bus

from . import heartbeat
from .loop import TradingLoop
from .monitor import RuntimeMonitor
from .pending_reconciler import PendingReconciler
from .position_review import PositionReviewLoop
from .post_mortem import PostMortemLoop
from .reconciler import BracketReconciler
from .safety import SafetyMonitor
from .scout import ScoutLoop

logger = logging.getLogger(__name__)


class SchedulerRunner:
    def __init__(
        self,
        *,
        trading_loops: list[TradingLoop],
        monitors: list[RuntimeMonitor],
        decision_interval_min: int,
        monitor_interval_sec: int,
        scout_loops: list[ScoutLoop] | None = None,
        scout_interval_min: int = 2,
        position_review_loops: list[PositionReviewLoop] | None = None,
        position_review_interval_sec: int = 90,
        safety_monitors: list[SafetyMonitor] | None = None,
        post_mortem_loops: list[PostMortemLoop] | None = None,
        post_mortem_interval_sec: int = 120,
        reconcilers: list[BracketReconciler] | None = None,
        reconciler_interval_sec: int = 45,
        pending_reconcilers: list[PendingReconciler] | None = None,
        pending_reconciler_interval_sec: int = 30,
    ) -> None:
        self.trading_loops = trading_loops
        self.monitors = monitors
        self.scout_loops = scout_loops or []
        self.position_review_loops = position_review_loops or []
        self.safety_monitors = safety_monitors or []
        self.post_mortem_loops = post_mortem_loops or []
        self.reconcilers = reconcilers or []
        self.pending_reconcilers = pending_reconcilers or []
        self.decision_interval_min = decision_interval_min
        self.monitor_interval_sec = monitor_interval_sec
        self.scout_interval_min = scout_interval_min
        self.position_review_interval_sec = position_review_interval_sec
        self.post_mortem_interval_sec = post_mortem_interval_sec
        self.reconciler_interval_sec = reconciler_interval_sec
        self.pending_reconciler_interval_sec = pending_reconciler_interval_sec
        # APScheduler defaults: coalesce=False, max_instances=1, misfire
        # grace = 1s. A long tick (esp. an LLM-bound loop) silently drops
        # all overlapping triggers and the loop goes quiet. Give ourselves
        # a bigger grace so a single slow tick catches up on the next run
        # instead of stranding the schedule.
        self._scheduler = AsyncIOScheduler(
            job_defaults={
                "coalesce": True,
                "max_instances": 1,
                "misfire_grace_time": 60,
            },
        )

    def start(self) -> None:
        bus = get_bus()
        # Run the first loop tick 15s after boot so the user sees activity
        # immediately instead of waiting N minutes.
        first = datetime.now(UTC) + timedelta(seconds=15)
        scout_first = datetime.now(UTC) + timedelta(seconds=8)
        for loop in self.trading_loops:
            self._scheduler.add_job(
                _safe_call(loop.tick, f"loop[{loop.broker.market.value}]"),
                trigger=IntervalTrigger(minutes=self.decision_interval_min),
                next_run_time=first,
                id=f"loop-{loop.broker.market.value}",
            )
        for mon in self.monitors:
            self._scheduler.add_job(
                _safe_call(mon.tick, f"monitor[{mon.broker.market.value}]"),
                trigger=IntervalTrigger(seconds=self.monitor_interval_sec),
                id=f"monitor-{mon.broker.market.value}",
            )
        for safety in self.safety_monitors:
            self._scheduler.add_job(
                _safe_call(
                    safety.tick,
                    f"safety[{safety._broker.market.value}]",
                ),
                trigger=IntervalTrigger(seconds=self.monitor_interval_sec),
                id=f"safety-{safety._broker.market.value}",
            )
        for scout in self.scout_loops:
            self._scheduler.add_job(
                _safe_call(scout.tick, f"scout[{scout.market_label}]"),
                trigger=IntervalTrigger(minutes=self.scout_interval_min),
                next_run_time=scout_first,
                id=f"scout-{scout.market_label}",
            )
        pr_first = datetime.now(UTC) + timedelta(seconds=45)
        for pr in self.position_review_loops:
            self._scheduler.add_job(
                _safe_call(pr.tick, f"position_review[{pr.market_label}]"),
                trigger=IntervalTrigger(
                    seconds=self.position_review_interval_sec
                ),
                next_run_time=pr_first,
                id=f"position-review-{pr.market_label}",
            )
        rec_first = datetime.now(UTC) + timedelta(seconds=50)
        for rec in self.reconcilers:
            self._scheduler.add_job(
                _safe_call(rec.tick, f"reconciler[{rec.market_label}]"),
                trigger=IntervalTrigger(seconds=self.reconciler_interval_sec),
                next_run_time=rec_first,
                id=f"reconciler-{rec.market_label}",
            )
        pending_first = datetime.now(UTC) + timedelta(seconds=40)
        for rec in self.pending_reconcilers:
            self._scheduler.add_job(
                _safe_call(
                    rec.tick, f"pending_reconciler[{rec.market_label}]"
                ),
                trigger=IntervalTrigger(
                    seconds=self.pending_reconciler_interval_sec
                ),
                next_run_time=pending_first,
                id=f"pending-reconciler-{rec.market_label}",
            )
        pm_first = datetime.now(UTC) + timedelta(seconds=60)
        for pm in self.post_mortem_loops:
            self._scheduler.add_job(
                _safe_call(pm.tick, f"post_mortem[{pm.market_label}]"),
                trigger=IntervalTrigger(
                    seconds=self.post_mortem_interval_sec
                ),
                next_run_time=pm_first,
                id=f"post-mortem-{pm.market_label}",
            )
        self._scheduler.start()
        logger.info(
            "scheduler started — %d loops, %d monitors, %d scouts, "
            "%d reviewers, %d post-mortems",
            len(self.trading_loops),
            len(self.monitors),
            len(self.scout_loops),
            len(self.position_review_loops),
            len(self.post_mortem_loops),
        )
        bus.publish(
            "scheduler.started",
            (
                f"{len(self.trading_loops)} loop(s), "
                f"{len(self.monitors)} monitor(s), "
                f"{len(self.scout_loops)} scout(s), "
                f"{len(self.position_review_loops)} reviewer(s); "
                f"first tick in ~15s, then every {self.decision_interval_min}m"
            ),
            severity=EventSeverity.SUCCESS,
            data={
                "loops": [loop.broker.market.value for loop in self.trading_loops],
                "monitors": [m.broker.market.value for m in self.monitors],
                "scouts": [s.market_label for s in self.scout_loops],
                "position_reviews": [
                    p.market_label for p in self.position_review_loops
                ],
                "decision_interval_min": self.decision_interval_min,
                "monitor_interval_sec": self.monitor_interval_sec,
                "scout_interval_min": self.scout_interval_min,
                "position_review_interval_sec": self.position_review_interval_sec,
            },
        )

    def stop(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)
        get_bus().publish(
            "scheduler.stopped",
            "scheduler shut down",
            severity=EventSeverity.WARN,
        )


def _safe_call(fn, label: str):
    bus = get_bus()

    async def wrapped() -> None:
        try:
            await fn()
        except Exception as exc:
            logger.exception("%s tick raised", label)
            bus.publish(
                "scheduler.error",
                f"{label} raised: {exc}",
                severity=EventSeverity.ERROR,
            )
            return
        # Stamp the heartbeat only on successful completion. A raising
        # tick must not appear "alive" to /system/status watchers.
        heartbeat.mark(label)

    return wrapped
