# Dashboard pages

Frontend lives at `http://127.0.0.1:3010` (default).

| Route           | Shows                                                                                                |
|-----------------|------------------------------------------------------------------------------------------------------|
| `/`             | Overview — cash / equity / P&L / open positions / AI provider health / recent activity / kill switch |
| `/analytics`    | Equity curve, daily P&L bars, decision throughput, trade-outcome scatter, win-rate summary           |
| `/intel`        | What the AI sees each cycle — watchlist quotes, company news, market news, discovery (top gainers/losers/most-active), last AI verdict per symbol |
| `/research`     | Chat-style researcher agent. Ask it about any ticker, see the tool-call trail, compare side-by-side  |
| `/agents`       | The agent mesh: last cycle per agent, recent LLM calls with token counts, scout queue, rate card     |
| `/decisions`    | Every AI decision with rationale, risk-engine verdict, execution result                              |
| `/trades`       | Filled orders, filterable by market / status, inline close button                                    |
| `/risk-config`  | Edit risk caps live (budget cap, per-position %, daily loss cap, drawdown, blacklist)                |

The top-right has the kill switch (requires a `KILL` typed
confirmation) and the pause-agents toggle (soft-pause without tripping
the kill switch).

For the full agent inventory behind these pages, see
[AGENTS.md](AGENTS.md).
