# API endpoints

All endpoints are mounted under `/api`. All require an
`X-API-Key: <JWT_SECRET>` header except `/api/health`. The SSE streams
accept `?api_key=...` on the querystring (because `EventSource` can't
set custom headers).

| Method | Path                                  | Purpose                                                                                          |
|--------|---------------------------------------|--------------------------------------------------------------------------------------------------|
| GET    | `/health`                             | Liveness probe — `{"status":"ok"}`, unauthenticated                                              |
| GET    | `/system/status`                      | Mode + per-loop scheduler heartbeat (`last_tick`, `seconds_ago`) — hook an external watchdog here |
| GET    | `/account`                            | Cash, equity, positions, P&L, trading state                                                      |
| GET    | `/decisions`                          | Decision history (paginated via `limit`)                                                         |
| GET    | `/trades`                             | Trade history                                                                                    |
| POST   | `/trades/{id}/close`                  | Close one open position at market                                                                |
| POST   | `/positions/close-all`                | Close every open position                                                                        |
| POST   | `/orders/cancel-all`                  | Cancel every open order                                                                          |
| GET    | `/risk-config`                        | Active risk config                                                                               |
| PUT    | `/risk-config`                        | Replace active risk config                                                                       |
| POST   | `/mode`                               | Flip paper ↔ live (requires typed confirmation)                                                  |
| POST   | `/kill-switch`                        | `{"confirm":"KILL","reason":"..."}` — halts trading                                              |
| POST   | `/unpause`                            | Clear the halt                                                                                   |
| POST   | `/agents/pause`                       | Soft-pause the agent loops (kill switch stays off)                                               |
| POST   | `/agents/resume`                      | Resume the agent loops                                                                           |
| POST   | `/agents/pause-when-closed`           | Toggle: when on, decision + scout loops skip while market is closed                              |
| GET    | `/activity`                           | Recent persisted activity events                                                                 |
| GET    | `/events`                             | SSE live activity stream                                                                         |
| GET    | `/ai/status`                          | AI provider reachability, loaded models, GPU usage                                               |
| GET    | `/intel`                              | Aggregated context (watchlist quotes, news, movers, last verdicts)                               |
| GET    | `/analytics`                          | Time-series for dashboard graphs                                                                 |
| GET    | `/options/{symbol}`                   | Options chain for symbol                                                                         |
| GET    | `/agents/overview`                    | Per-agent last cycle + status                                                                    |
| GET    | `/agents/roster`                      | Static agent definitions + descriptions                                                          |
| GET    | `/agents/cycles`                      | Recent cycle summaries across all agents                                                         |
| GET    | `/agents/llm-calls`                   | Recent LLM invocations (paginated)                                                               |
| GET    | `/agents/llm-calls/{id}`              | Prompt + response for one LLM call                                                               |
| GET    | `/agents/rate-card`                   | Active LLM pricing table                                                                         |
| PUT    | `/agents/rate-card`                   | Update LLM pricing                                                                               |
| GET    | `/agents/usage`                       | Rolling LLM spend summary                                                                        |
| GET    | `/scout/queue`                        | Current scout queue snapshot                                                                     |
| GET    | `/research/conversations`             | Researcher chat sessions (list, create, GET / DELETE)                                            |
| GET    | `/research/conversations/{id}/stream` | SSE stream of a researcher chat turn                                                             |
