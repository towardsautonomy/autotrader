# Day-one playbook

A concrete first-session walkthrough after [setup](SETUP.md) is done.

1. **Boot both processes.** LM Studio (or rely on OpenRouter), backend
   (`python -m app.main`), frontend (`npm run dev`).
2. **Open `http://127.0.0.1:3010/`.** You should see a `PAPER` banner
   at the top, non-zero cash (the Alpaca paper account seeds $100 K),
   and an AI-provider status pill that says `ready`.
3. **Visit `/risk-config`.** Tighten the defaults to something you're
   actually comfortable with for the paper run. Smaller is better.
   Recommended starting point for a $100 K paper account:
   - `budget_cap_usd`: 10 000 (use a *slice* of the paper balance, not all of it)
   - `max_position_pct`: 0.03
   - `daily_loss_cap_pct`: 0.02
   - `max_concurrent_positions`: 3
   - `stop_loss_pct`: 0.03
4. **Wait for the first cycle.** The scout runs every 2 min, the
   decision loop every 5 min. Within ~10 min you should see events flow
   into `/activity` and candidates in the scout queue widget on the
   dashboard.
5. **Watch `/decisions`.** The first few decisions are usually
   `REJECTED` or `WAIT-FOR-X` while the system learns the market state.
   That's fine — the risk engine is doing its job.
6. **Use `/research` to sanity-check.** Type in a ticker the AI just
   rejected ("why did you skip NVDA?") and see the reasoning. This is
   how you build conviction on whether the AI is any good.
7. **Let it run overnight.** Check `/analytics` the next morning —
   cumulative P&L, win rate, drawdown, per-cycle latency. If the system
   is paying for its LLM cost in paper P&L, great. If not, tune.
8. **Repeat for at least 4–8 weeks** before even considering live. See
   [GOING_LIVE.md](GOING_LIVE.md).
