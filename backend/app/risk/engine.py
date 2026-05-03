"""RiskEngine — validates every trade proposal before it reaches a broker.

This module is the non-negotiable safety layer. Every AI-proposed trade
passes through :meth:`RiskEngine.validate` first. A reject here means the
trade never happens.

Invariant: the RiskEngine is pure — given the same (config, snapshot,
proposal), it always returns the same ValidationResult. Run state lives in
the AccountSnapshot that the caller assembles from the DB + broker.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from app.clock import ny_today

from .types import (
    AccountSnapshot,
    OptionStructure,
    RejectionCode,
    RiskConfig,
    TradeAction,
    TradeProposal,
    ValidationResult,
)


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def validate(
        self, proposal: TradeProposal, snapshot: AccountSnapshot
    ) -> ValidationResult:
        if not snapshot.trading_enabled:
            return ValidationResult.reject(
                RejectionCode.KILL_SWITCH,
                "trading_enabled=false (kill switch active)",
            )

        if proposal.action == TradeAction.CLOSE:
            return self._validate_close(proposal, snapshot)
        return self._validate_open(proposal, snapshot)

    def _validate_close(
        self, proposal: TradeProposal, snapshot: AccountSnapshot
    ) -> ValidationResult:
        existing = snapshot.find_position(proposal.market, proposal.symbol)
        if existing is None:
            return ValidationResult.reject(
                RejectionCode.NO_POSITION_TO_CLOSE,
                f"no open position for {proposal.market}:{proposal.symbol}",
            )
        return ValidationResult.approve(proposal)

    def _validate_open(
        self, proposal: TradeProposal, snapshot: AccountSnapshot
    ) -> ValidationResult:
        c = self.config

        if snapshot.total_exposure_usd >= c.budget_cap:
            return ValidationResult.reject(
                RejectionCode.OVER_BUDGET_DELEVERAGE,
                f"exposure ${snapshot.total_exposure_usd:.2f} >= budget cap "
                f"${c.budget_cap:.2f} — only close actions allowed until "
                "positions are unwound",
            )

        if proposal.size_usd <= 0:
            return ValidationResult.reject(
                RejectionCode.SIZE_NONPOSITIVE,
                f"size_usd must be positive, got {proposal.size_usd}",
            )

        if proposal.size_usd < c.min_trade_size_usd:
            return ValidationResult.reject(
                RejectionCode.SIZE_BELOW_MIN,
                f"size ${proposal.size_usd:.2f} below min ${c.min_trade_size_usd:.2f}",
            )

        if proposal.symbol.upper() in {s.upper() for s in c.blacklist}:
            return ValidationResult.reject(
                RejectionCode.BLACKLISTED,
                f"symbol {proposal.symbol} is blacklisted",
            )

        # Halts consider realized + unrealized P&L. Otherwise an open
        # position that's deep underwater doesn't trip the halt until it
        # closes — and the AI keeps stacking new trades on top of the
        # silent drawdown. day_pnl_total = day_realized + day_unrealized;
        # cumulative_pnl_with_open = all realized + all open unrealized.
        if snapshot.day_pnl_total <= c.daily_loss_limit_usd:
            return ValidationResult.reject(
                RejectionCode.DAILY_LOSS_HALT,
                f"HALT: day P&L {snapshot.day_pnl_total:.2f} "
                f"(realized {snapshot.day_realized_pnl:.2f} + unrealized "
                f"{snapshot.day_unrealized_pnl:.2f}) "
                f"<= limit {c.daily_loss_limit_usd:.2f}",
            )

        if snapshot.cumulative_pnl_with_open <= c.max_drawdown_limit_usd:
            return ValidationResult.reject(
                RejectionCode.MAX_DRAWDOWN_HALT,
                f"HALT: cumulative P&L (incl open) "
                f"{snapshot.cumulative_pnl_with_open:.2f} <= drawdown limit "
                f"{c.max_drawdown_limit_usd:.2f}",
            )

        if snapshot.daily_trade_count >= c.max_daily_trades:
            return ValidationResult.reject(
                RejectionCode.DAILY_TRADE_CAP_REACHED,
                f"daily trade cap ({c.max_daily_trades}) reached",
            )

        # PDT guard: this platform intends to close intraday. If we're
        # already at the FINRA 3-in-5 same-day round-trip count, a new
        # open would very likely become the 4th day trade on close and
        # trigger Alpaca's PDT restriction. Reject the open instead.
        if (
            snapshot.pdt_day_trades_window_used
            >= c.pdt_day_trade_count_5bd
        ):
            return ValidationResult.reject(
                RejectionCode.PDT_LIMIT_REACHED,
                f"PDT: {snapshot.pdt_day_trades_window_used} same-day "
                f"round-trips in trailing 5 business days >= cap "
                f"{c.pdt_day_trade_count_5bd} — no new opens until the "
                "rolling window clears",
            )

        existing = snapshot.find_position(proposal.market, proposal.symbol)
        if existing is not None:
            return ValidationResult.reject(
                RejectionCode.DUPLICATE_POSITION,
                f"already holding {proposal.symbol} — position-review agent "
                "owns existing holdings. Propose action=close to exit, or "
                "action=hold to keep the current position; do not re-open "
                "a duplicate.",
            )
        if len(snapshot.positions) >= c.max_concurrent_positions:
            return ValidationResult.reject(
                RejectionCode.MAX_CONCURRENT_REACHED,
                f"max concurrent positions ({c.max_concurrent_positions}) reached",
            )

        if proposal.size_usd > c.per_trade_max_usd:
            return ValidationResult.reject(
                RejectionCode.PER_TRADE_MAX_EXCEEDED,
                f"size ${proposal.size_usd:.2f} exceeds per-trade max "
                f"${c.per_trade_max_usd:.2f}",
            )

        if proposal.size_usd > snapshot.cash_balance:
            return ValidationResult.reject(
                RejectionCode.INSUFFICIENT_CASH,
                f"size ${proposal.size_usd:.2f} exceeds cash ${snapshot.cash_balance:.2f}",
            )

        # Effective cap = min(configured budget, actual equity). If the
        # account has drawn down below the user's configured budget_cap,
        # we mustn't let them deploy more capital than they actually hold.
        # This cross-check closes the gap where budget_cap drifts from
        # realized equity after losses.
        effective_cap = min(c.budget_cap, snapshot.total_equity)
        projected_exposure = snapshot.total_exposure_usd + proposal.size_usd
        if projected_exposure > effective_cap:
            return ValidationResult.reject(
                RejectionCode.BUDGET_EXCEEDED,
                f"projected exposure ${projected_exposure:.2f} exceeds "
                f"effective cap ${effective_cap:.2f} "
                f"(budget_cap ${c.budget_cap:.2f}, equity ${snapshot.total_equity:.2f})",
            )

        # Reject LLM-proposed stops that are wider than the configured
        # hard ceiling. Without this, a proposal with stop_loss_pct=0.50
        # passes — defeating the whole "bounded loss" promise.
        if (
            proposal.stop_loss_pct is not None
            and proposal.stop_loss_pct > c.max_stop_loss_pct
        ):
            return ValidationResult.reject(
                RejectionCode.STOP_LOSS_TOO_WIDE,
                f"stop_loss_pct {proposal.stop_loss_pct:.2%} exceeds "
                f"max_stop_loss_pct {c.max_stop_loss_pct:.2%}",
            )

        # Confidence floor — reject opens the LLM isn't convinced by. The
        # losing-streak audit found wins vs losses differ by only ~4.5pp
        # of confidence, so anything below the floor is coin-flip noise
        # and we should rather hold.
        if (
            c.min_open_confidence > 0
            and proposal.confidence is not None
            and proposal.confidence < c.min_open_confidence
        ):
            return ValidationResult.reject(
                RejectionCode.CONFIDENCE_TOO_LOW,
                f"confidence {proposal.confidence:.2f} below floor "
                f"{c.min_open_confidence:.2f} — skip marginal trades",
            )

        # Reward/risk floor — enforce take_profit_pct >= ratio * stop_loss_pct.
        # If either is missing we fall back to the config defaults before
        # checking; this catches proposals that specify a very tight tp
        # relative to the stop (the core "loser round-trips through the
        # stop" failure mode).
        effective_stop = (
            proposal.stop_loss_pct
            if proposal.stop_loss_pct is not None
            else c.default_stop_loss_pct
        )
        effective_tp = (
            proposal.take_profit_pct
            if proposal.take_profit_pct is not None
            else c.default_take_profit_pct
        )
        if (
            proposal.option is None
            and c.min_reward_risk_ratio > 0
            and effective_stop > 0
            and effective_tp / effective_stop < c.min_reward_risk_ratio
        ):
            return ValidationResult.reject(
                RejectionCode.REWARD_RISK_TOO_LOW,
                f"reward/risk {effective_tp / effective_stop:.2f} below "
                f"min {c.min_reward_risk_ratio:.2f} "
                f"(tp {effective_tp:.2%}, sl {effective_stop:.2%})",
            )

        # Option-specific gates. Plain stock trades skip this block.
        if proposal.option is not None:
            option_result = self._validate_option(proposal)
            if not option_result.approved:
                return option_result

        adjusted = proposal.with_defaults(
            stop_loss_pct=c.default_stop_loss_pct,
            take_profit_pct=c.default_take_profit_pct,
        )
        # Conviction-scaled sizing — shrink size on marginal confidence so
        # we bleed less when the thesis is weak. Linear from 0.5x at the
        # confidence floor to 1.0x at 0.85+. Options skip (size derives
        # from max_loss_usd). When confidence is missing, no scaling.
        if (
            adjusted.option is None
            and adjusted.confidence is not None
            and c.min_open_confidence > 0
        ):
            floor = c.min_open_confidence
            full = max(floor + 0.01, 0.85)
            if adjusted.confidence >= full:
                scale = 1.0
            else:
                span = full - floor
                scale = 0.5 + 0.5 * max(0.0, adjusted.confidence - floor) / span
            scaled_size = adjusted.size_usd * scale
            if scaled_size >= c.min_trade_size_usd and scale < 1.0:
                adjusted = replace(adjusted, size_usd=scaled_size)
        return ValidationResult.approve(adjusted)

    def _validate_option(self, proposal: TradeProposal) -> ValidationResult:
        """Gate defined-risk-only structures by tier + max-loss cap.

        Runs *after* the shared open-trade checks so callers can assume
        size/budget/etc are already validated. Order matters: we check
        tier gate before max-loss, so a bad structure gets a clearer
        rejection code than "loss too big".
        """
        c = self.config
        opt = proposal.option
        if opt is None:
            return ValidationResult.approve(proposal)

        allowed = c.allowed_structures()
        if opt.structure not in allowed:
            return ValidationResult.reject(
                RejectionCode.STRUCTURE_NOT_ALLOWED,
                f"structure {opt.structure.value} not permitted at tier "
                f"{c.risk_tier.value} (allowed: "
                f"{', '.join(s.value for s in sorted(allowed, key=lambda x: x.value))})",
            )

        if opt.max_loss_usd < 0:
            return ValidationResult.reject(
                RejectionCode.UNDEFINED_RISK,
                f"max_loss_usd must be >= 0 for defined-risk structures, "
                f"got {opt.max_loss_usd}",
            )

        # Covered calls + CSPs don't cap 'loss' the same way (loss = assignment
        # at strike, which is by design). For everything else enforce the
        # per-spread cap from config.
        capped_structures = {
            OptionStructure.LONG_CALL,
            OptionStructure.LONG_PUT,
            OptionStructure.VERTICAL_DEBIT,
            OptionStructure.VERTICAL_CREDIT,
            OptionStructure.IRON_CONDOR,
        }
        if opt.structure in capped_structures:
            cap = c.max_option_loss_per_spread_usd
            if opt.max_loss_usd > cap:
                return ValidationResult.reject(
                    RejectionCode.OPTION_MAX_LOSS_EXCEEDED,
                    f"max loss ${opt.max_loss_usd:.2f} exceeds per-spread cap "
                    f"${cap:.2f} (tier {c.risk_tier.value})",
                )

        # Expiry sanity — no 0dte, no stale expiries. We don't block weeklies
        # outright (tier-dependent would over-engineer), just reject anything
        # already expired or expiring today.
        try:
            expiry = date.fromisoformat(opt.expiry)
        except ValueError:
            return ValidationResult.reject(
                RejectionCode.EXPIRY_TOO_CLOSE,
                f"expiry {opt.expiry!r} is not a valid ISO date",
            )
        today = ny_today()
        if expiry <= today:
            return ValidationResult.reject(
                RejectionCode.EXPIRY_TOO_CLOSE,
                f"expiry {opt.expiry} is today or in the past — no 0dte",
            )

        return ValidationResult.approve(proposal)
