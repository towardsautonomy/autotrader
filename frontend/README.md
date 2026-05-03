# autotrader — frontend

Next.js 16 App Router dashboard for the autotrader backend. Terminal-
inspired green-on-black theme, no auth UI (uses a shared API key that
matches the backend `JWT_SECRET`), no auto-refresh magic — explicit
polling and SSE streaming only.

For project-wide setup, see the top-level [README](../README.md).
Read it before pointing this UI at a live broker — the disclaimer at
the top of that file applies here too.

---

## Dev

```bash
npm install
cp .env.local.example .env.local
# Edit .env.local:
#   NEXT_PUBLIC_API_URL=http://127.0.0.1:3003/api
#   NEXT_PUBLIC_API_KEY=<same value as backend JWT_SECRET>

npm run dev
# → http://127.0.0.1:3010
```

`npm run build` / `npm start` for production. `npm run lint` runs ESLint.
TypeScript strict mode; typecheck with `npx tsc --noEmit`.

---

## Pages

| Path            | File                                | What it shows                                                   |
|-----------------|-------------------------------------|-----------------------------------------------------------------|
| `/`             | `src/app/page.tsx`                  | Overview: cash, equity, positions, P&L, AI status, activity    |
| `/analytics`    | `src/app/analytics/page.tsx`        | Equity curve, daily P&L, decision throughput, outcome scatter   |
| `/intel`        | `src/app/intel/page.tsx`            | What the AI sees: watchlist quotes, news, movers, last verdicts |
| `/research`     | `src/app/research/page.tsx`         | Researcher chat with tool-call trail, compare-side-by-side      |
| `/agents`       | `src/app/agents/page.tsx`           | Agent roster, recent cycles, LLM-call log, rate card            |
| `/decisions`    | `src/app/decisions/page.tsx`        | Full decision log with rationale + verdicts                     |
| `/trades`       | `src/app/trades/page.tsx`           | Trade history, close button                                     |
| `/risk-config`  | `src/app/risk-config/page.tsx`      | Edit risk caps live                                             |

---

## Components worth knowing

| File                                        | Role                                                         |
|---------------------------------------------|--------------------------------------------------------------|
| `components/NavBar.tsx`                     | Top nav + active-link state                                  |
| `components/ModeBanner.tsx`                 | PAPER / LIVE banner (top of layout)                          |
| `components/AIStatus.tsx`                   | Provider health pill; polls `/api/ai/status`                 |
| `components/ActivityLog.tsx`                | Live SSE log; pause-to-paginate; filter by severity          |
| `components/KillSwitchButton.tsx`           | `KILL` typed confirm → `/api/kill-switch`                    |
| `components/AgentPauseButton.tsx`           | Soft pause + `idle_when_market_closed` toggle                |
| `components/Pager.tsx`                      | `usePagination<T>()` hook + `PagerControls`                  |
| `components/ScoutQueuePanel.tsx`            | Live scout queue snapshot                                    |
| `components/AgentSwarm.tsx`                 | Per-agent status grid on the dashboard                       |
| `components/research/ToolResultCards.tsx`   | Per-tool render cards for researcher chat                    |

---

## Conventions

- **Client components** (`"use client"`) for anything with state or SSE.
  Everything else defaults to server components.
- **API client** lives in `src/lib/api.ts`. All calls go through it —
  never `fetch` directly from a page.
- **SSE** streams use `EventSource` with the API key on the querystring
  (`?api_key=...`) because `EventSource` can't set custom headers.
- **Tailwind v4** with a CSS-variable theme (`globals.css`). Palette:
  `bg`, `bg-panel`, `bg-raised`, `text`, `text-dim`, `text-faint`,
  `accent` (green), `warn` (amber), `danger` (red). Use these tokens, not
  raw hex.
- **Pagination**: prefer `usePagination(items, { perPage, maxPages })`
  over rolling your own.

---

## Environment

```
NEXT_PUBLIC_API_URL=http://127.0.0.1:3003/api
NEXT_PUBLIC_API_KEY=<same as backend JWT_SECRET>
NEXT_DEV_ORIGINS=                  # optional: comma-separated LAN hosts
```

`NEXT_PUBLIC_*` is baked into the bundle at build time — restart
`npm run dev` after editing `.env.local`. The API key is visible in the
browser, so it is *not* a secret; it's a shared password that scopes the
backend to your machine. Don't expose the backend publicly.

`NEXT_DEV_ORIGINS` lets you reach the dev server from another device on
your LAN (phone, tablet) — set it to e.g. `http://192.168.1.10:3010` and
add the same host to the backend's `CORS_ORIGINS`. Leave blank for
localhost-only.

---

## AGENTS.md

This repo also has an `AGENTS.md` that tells AI coding assistants how
the Next.js version here differs from their training data. Read it if
you're delegating changes to a coding agent.
