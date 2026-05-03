<!--
Thanks for the PR! A few things to confirm before review.
Security issues — see SECURITY.md, do not put exploit details here.
-->

## What and why

<!-- One paragraph. What changes, and the motivating problem. -->

## How to verify

<!-- Steps a reviewer can run to see the change behave. -->

## Risk-engine impact

<!--
"None" is a fine answer if you didn't touch backend/app/risk/.
If you did, link the test you added or updated in
backend/tests/test_risk_engine.py.
-->

## Checklist

- [ ] Tested on `PAPER_MODE=true` (Alpaca paper). No live-money runs.
- [ ] `pytest` is green locally.
- [ ] `npx tsc --noEmit` is green for any frontend change.
- [ ] `pre-commit run --all-files` passes (gitleaks, ruff, …).
- [ ] No `.env`, `*.sqlite`, `*.log`, or wallet files in the diff.
- [ ] If setup / env / pages changed, README is updated in this PR.
- [ ] If `backend/app/risk/` changed, a covering test is included.
