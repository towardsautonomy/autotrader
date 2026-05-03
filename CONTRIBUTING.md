# Contributing to autotrader

Thanks for considering a contribution. This is a personal-grade trading
system that touches real-money credentials, so the bar for contributions
is higher than usual.

## Ground rules

1. **Paper-mode only.** Every PR must be tested with `PAPER_MODE=true`.
   We will not merge code that was only validated against live brokerage
   accounts. CI never runs against live endpoints.
2. **Risk engine = test required.** Any change inside `backend/app/risk/`
   needs a covering test in `backend/tests/test_risk_engine.py`. The risk
   engine is the last thing standing between the LLM and your money.
3. **No secrets in the repo, ever.** `.env`, wallet keys, API tokens,
   bearer headers — none of it. The pre-commit `gitleaks` hook is there
   to catch accidents; please keep it green. If you ever leak a key,
   rotate it (see [SECURITY.md](SECURITY.md)) — `git filter-branch`
   cannot put toothpaste back in the tube.
4. **Architecture changes start with an issue.** Anything that changes
   how loops, agents, or brokers compose deserves a discussion before
   the diff. Open an issue first; we'll flag whether to PR or design.
5. **Keep the README honest.** If you change setup, env vars, ports, or
   pages, update README.md and `docs/ARCHITECTURE.md` in the same PR.

## Dev setup

See [README.md](README.md) for the full quickstart. Short version:

```bash
git clone <fork>
cd autotrader

# backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # then fill in
pre-commit install

# frontend
cd ../frontend
npm install
cp .env.local.example .env.local  # set NEXT_PUBLIC_API_KEY = backend JWT_SECRET
```

## Before you push

```bash
# backend
cd backend
ruff check .
pytest

# frontend
cd ../frontend
npx tsc --noEmit
npm run lint
```

`pre-commit run --all-files` covers most of the above plus `gitleaks`.

## PR checklist

- [ ] Tested with `PAPER_MODE=true` on Alpaca paper.
- [ ] `pytest` passes locally (192+ tests at time of writing).
- [ ] `npx tsc --noEmit` passes for any frontend change.
- [ ] If you touched `backend/app/risk/`, you added or updated a test.
- [ ] If you touched setup, env vars, or pages, README is updated.
- [ ] No `.env`, `*.sqlite`, `*.log`, or wallet files in the diff.
- [ ] Commit messages follow the `area: short summary` pattern used in
      `git log` (`feat(risk):`, `fix(scheduler):`, `chore:`, …).

## Reporting bugs

Use the bug-report template in [.github/ISSUE_TEMPLATE/](.github/ISSUE_TEMPLATE/).
Include the exact `PAPER_MODE` you ran with, broker (Alpaca / Polymarket),
the relevant scheduler tick log lines, and the minimum repro.

For security issues, see [SECURITY.md](SECURITY.md) — do **not** file
a public issue.

## Style

- Python: `ruff` + the rules in `backend/pyproject.toml`. Type-annotate
  public functions. Prefer `dataclass` / `pydantic` over loose dicts at
  module boundaries.
- TypeScript: `tsc --strict`, `eslint`. Prefer functional components and
  hooks. Don't add a state library when `useState` will do.
- Comments: explain *why* the non-obvious thing is the way it is. Don't
  paraphrase the code. Trust the reader.
