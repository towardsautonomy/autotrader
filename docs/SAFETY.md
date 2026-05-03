# Safety

For *vulnerability reporting* see [SECURITY.md](../SECURITY.md). This
file is about *operational* safety: kill switch, secrets handling,
heartbeat watchdog, decide-timeout, and pre-commit hooks.

---

## Kill switch

`POST /api/kill-switch` or the big red button on the dashboard. Blocks
all new orders; open positions remain and can be closed manually from
`/trades` or via `POST /api/positions/close-all`.

To clear: `POST /api/unpause`.

The kill switch is independent of the soft "pause agents" toggle —
agent-pause stops *new* LLM calls without flipping `trading_enabled`,
which means manual closes still work. The kill switch is the harder
stop: nothing trades regardless of pause state.

---

## Secrets — hard rules

- `backend/.env` holds every secret. It is git-ignored.
- The app refuses to start for live trading if any required secret
  still reads `replace_me` (`settings.assert_secrets_configured()`).
  The gate fires from `lifespan` when `PAPER_MODE=false`; paper mode
  skips it so dev loops don't block on unset optionals. Required vars
  for live: `ALPACA_API_KEY / ALPACA_API_SECRET`, `JWT_SECRET`, the
  active provider's API key, and (when Polymarket is wired)
  `POLYMARKET_PRIVATE_KEY`.
- A pre-commit `gitleaks` hook blocks accidental key commits (setup
  below).
- **Never paste a private key into chat, an issue, or a PR.** If it
  happens, the key is compromised — move funds to a fresh wallet
  immediately and rotate.

---

## Scheduler heartbeat & watchdog

Every loop stamps `app/scheduler/heartbeat.py` after each successful
tick. `/api/system/status` (authenticated) surfaces the snapshot:

```json
{
  "status": "ok",
  "mode": "PAPER",
  "scheduler": {
    "loop[stocks]":               {"last_tick": "...", "seconds_ago": 42.1},
    "monitor[stocks]":            {"last_tick": "...", "seconds_ago": 12.0},
    "scout[stocks]":              {"last_tick": "...", "seconds_ago": 71.8},
    "position_review[stocks]":    {"last_tick": "...", "seconds_ago": 65.4},
    "reconciler[stocks]":         {"last_tick": "...", "seconds_ago": 18.3},
    "pending_reconciler[stocks]": {"last_tick": "...", "seconds_ago": 22.1},
    "safety[stocks]":             {"last_tick": "...", "seconds_ago": 12.4}
  }
}
```

A failing tick deliberately does NOT stamp, so a stale `seconds_ago`
distinguishes "silently stopped firing" from "alive but erroring".
Recommended: point an external watchdog (cron + curl, UptimeRobot,
Grafana, etc.) at `/system/status` (with the `X-API-Key` header) and
alert when any loop's `seconds_ago` exceeds ~3× its cadence. `/health`
is the unauthenticated liveness probe — use it to confirm the process
is up; use `/system/status` for tick-level monitoring. Without an
external watcher you only notice a stall when you open the dashboard.

The APScheduler itself is configured with `coalesce=True`,
`max_instances=1`, and a 60 s misfire grace time — a long tick
(especially an LLM-bound one) coalesces into one catch-up tick instead
of stranding the schedule.

---

## Decide-timeout guard

`TradingLoop` wraps the strategy's `decide()` call in
`asyncio.wait_for(..., timeout=180s)`. A hung LLM / provider connection
records a `decide_timeout` rejected decision and returns cleanly; the
next tick proceeds normally. The timeout is currently hardcoded in
`app/scheduler/loop.py` (`decide_timeout_sec: float = 180.0`). Raise it
only if you routinely see slow-but-legitimate decisions at 180 s — if
you raise it, raise `STOCK_DECISION_INTERVAL_MIN` too.

---

## Pre-commit setup

```bash
pipx install pre-commit            # or: pip install --user pre-commit
cd /path/to/autotrader
pre-commit install
pre-commit run --all-files         # optional first full sweep
```

Verify gitleaks is actually blocking leaks:

```bash
echo 'POLYMARKET_PRIVATE_KEY="0xabc123...64-hex-chars..."' > /tmp/leak.txt
git add /tmp/leak.txt              # should be refused by pre-commit
```
