# Going live (checklist)

Do not flip `PAPER_MODE=false` until every box below is checked.

- [ ] `pytest` passes end-to-end — especially `test_risk_engine.py`.
- [ ] Pre-commit / gitleaks is installed; `git check-ignore backend/.env`
      returns the file (i.e. the real `.env` is ignored). See
      [SAFETY.md](SAFETY.md).
- [ ] 4–8 weeks of paper trading have run and you've reviewed
      `/analytics` — net P&L, Sharpe, max drawdown, win rate, fees.
- [ ] Paper Sharpe is positive **and** drawdown is something you can
      actually stomach in dollar terms.
- [ ] Kill switch tested: deliberate trigger during a paper session
      confirmed no new orders land.
- [ ] Daily-loss-cap HALT tested: forced losses in paper tripped the
      halt and stopped further trading.
- [ ] The live-mode toggle has been used once to flip PAPER → LIVE and
      immediately back — confirming the typed-confirmation gate works.
- [ ] An external watchdog is hitting `/api/system/status` (with the
      `X-API-Key` header) and will page you if any loop's `seconds_ago`
      goes stale (≥3× its cadence).
- [ ] You've decided the maximum amount you can lose entirely, and the
      live `budget_cap_usd` is set at or below that number.

If any box is unchecked, paper longer.
