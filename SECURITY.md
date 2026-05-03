# Security policy

`autotrader` handles **money-controlling credentials**: brokerage API keys
and on-chain wallet private keys. Treat security incidents accordingly.

## Reporting a vulnerability

**Do not file a public issue for security problems.**

Open a [private security advisory](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on the GitHub repo (the *Security* tab → *Report a vulnerability*).
Include:

- Affected file(s) and a short description of the issue.
- A proof-of-concept or repro steps if you have one.
- The impact you believe it has (info disclosure, key exfiltration,
  unauthorized trade execution, etc.).

We aim to acknowledge within **3 business days** and ship a fix or
mitigation within **14 days** for high-severity issues.

## What is in scope

- Code paths that could log, persist, or transmit a secret
  (`JWT_SECRET`, `ALPACA_API_SECRET`, `POLYMARKET_PRIVATE_KEY`,
  `OPENROUTER_API_KEY`, etc.).
- Authentication / authorization on the FastAPI routes.
- Risk-engine bypasses that could cause trades to execute outside
  configured caps.
- Broker-adapter code that could submit unintended orders.
- The activity bus / audit log if it could leak secrets.
- Dependency vulnerabilities (we'll evaluate severity case-by-case).

## What is out of scope

- Self-inflicted leaks (you committed your `.env`, you posted your wallet
  key, etc.). Rotate the key immediately — see below.
- Loss of money from legitimate trades: that is on the operator, not a
  security issue.
- Generic "broker API rejected my order" reports — file a regular issue.

## If you leaked a secret

`git filter-branch` / BFG can remove the key from history, but assume
**the key is already compromised** the moment it lands anywhere outside
your machine. The only real remediation is rotation.

**Brokerage keys (Alpaca, Polymarket CLOB, OpenRouter, Finnhub, etc.):**

1. Revoke the key in the provider's dashboard.
2. Generate a new key.
3. Update `backend/.env`.
4. Force-restart the backend so the new key takes effect.

**Polygon wallet private key (`POLYMARKET_PRIVATE_KEY`):**

1. Treat the wallet as drained-pending. Move any USDC, MATIC, or
   open Polymarket positions to a fresh wallet **immediately**.
2. Generate a new wallet (Rabby / MetaMask / `cast wallet new`).
3. Re-derive new CLOB API creds against the new wallet.
4. Update `backend/.env`. Never reuse the leaked address.

## Hardening checklist for operators

- [ ] `gitleaks` runs as a pre-commit hook (`pre-commit install`).
- [ ] `backend/.env` is git-ignored — verify with `git check-ignore backend/.env`.
- [ ] `JWT_SECRET` is generated with `openssl rand -hex 32`, not a guess.
- [ ] `PAPER_MODE=true` until paper results justify the flip.
- [ ] When in `LIVE` mode, the dashboard banner is loud and red.
- [ ] No screenshots or recordings of the dashboard contain unredacted
      keys (the `Authorization` header is a common slip).
- [ ] CORS origins are restricted to the hosts you actually serve from
      (`CORS_ORIGINS` in `backend/.env`).
- [ ] If you publish a fork or share logs, scrub `.env`, `*.sqlite`,
      `*.log` first — `.gitignore` covers commits, not other channels.

## Threat model summary

| Asset | Worst-case impact | Defense |
| --- | --- | --- |
| Polygon wallet privkey | Wallet drained | Env-only storage, gitleaks, rotation guidance above |
| Alpaca live keys | Unauthorized trades on live account | Same + `PAPER_MODE` default, kill switch |
| `JWT_SECRET` | Anyone on your network can call the API | Restrict `CORS_ORIGINS`, run on `127.0.0.1` unless needed |
| LLM provider key | Bill run-up | Per-provider rate limits, daily LLM budget cap |
| SQLite DB on disk | Audit log + open positions visible | OS-level file permissions on the host |

The risk engine is the last line of defense between an LLM and your money.
Treat changes to `backend/app/risk/` with extra scrutiny — every change
needs a covering test in `backend/tests/test_risk_engine.py`.
