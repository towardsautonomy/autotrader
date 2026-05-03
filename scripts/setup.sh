#!/usr/bin/env bash
# One-shot installer for autotrader.
#
# Creates backend venv, installs Python deps, scaffolds backend/.env with a
# fresh JWT_SECRET, runs `npm install`, and scaffolds frontend/.env.local
# pointed at the local backend with the same secret.
#
# Re-running is safe: existing .env files are preserved.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

say()  { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ---- prerequisite checks -----------------------------------------------------
command -v python3 >/dev/null || die "python3 not found — install Python 3.11+"
command -v node    >/dev/null || die "node not found — install Node 20+"
command -v npm     >/dev/null || die "npm not found"

PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJ=${PY_VER%%.*}; PY_MIN=${PY_VER##*.}
if (( PY_MAJ < 3 )) || { (( PY_MAJ == 3 )) && (( PY_MIN < 11 )); }; then
    die "Python $PY_VER detected — need 3.11+"
fi

NODE_MAJ=$(node -p 'process.versions.node.split(".")[0]')
(( NODE_MAJ >= 20 )) || die "Node $NODE_MAJ detected — need 20+"

# ---- secret generation -------------------------------------------------------
if command -v openssl >/dev/null; then
    SECRET=$(openssl rand -hex 32)
else
    SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
fi

# ---- backend -----------------------------------------------------------------
say "backend: creating venv"
cd "$ROOT/backend"
[[ -d .venv ]] || python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate

say "backend: installing Python deps (this takes a minute)"
pip install --upgrade pip --quiet
pip install -e '.[dev]' --quiet

if [[ -f .env ]]; then
    warn "backend/.env already exists — leaving it alone"
else
    say "backend: writing .env from .env.example"
    cp .env.example .env
    # Inject a fresh JWT_SECRET and default Alpaca paper URL.
    python3 - <<PY
import pathlib, re
p = pathlib.Path(".env"); t = p.read_text()
t = re.sub(r"^JWT_SECRET=.*$",     f"JWT_SECRET=$SECRET", t, flags=re.M)
t = re.sub(r"^ALPACA_BASE_URL=.*$", "ALPACA_BASE_URL=https://paper-api.alpaca.markets", t, flags=re.M)
p.write_text(t)
PY
    warn "backend/.env still needs your Alpaca + AI provider keys before the scheduler will start"
fi

# ---- frontend ----------------------------------------------------------------
say "frontend: installing npm deps"
cd "$ROOT/frontend"
npm install --silent

if [[ -f .env.local ]]; then
    warn "frontend/.env.local already exists — leaving it alone"
else
    say "frontend: writing .env.local"
    cp .env.local.example .env.local
    python3 - <<PY
import pathlib, re
p = pathlib.Path(".env.local"); t = p.read_text()
t = re.sub(r"^NEXT_PUBLIC_API_URL=.*$", "NEXT_PUBLIC_API_URL=http://127.0.0.1:3003/api", t, flags=re.M)
t = re.sub(r"^NEXT_PUBLIC_API_KEY=.*$", f"NEXT_PUBLIC_API_KEY=$SECRET", t, flags=re.M)
p.write_text(t)
PY
fi

# ---- done --------------------------------------------------------------------
cat <<'EOF'

────────────────────────────────────────────────────────────
✓ setup complete

Next steps:
  1. Edit backend/.env — add your Alpaca paper keys and either
     OPENROUTER_API_KEY or LM Studio settings (AI_PROVIDER=lmstudio).
     See docs/SETUP.md for the LM Studio path.

  2. Start it:
       cd backend && source .venv/bin/activate && python -m app.main
       cd frontend && npm run dev

  3. Open http://127.0.0.1:3010/ — you should see a PAPER banner.

PAPER_MODE is the default. Read the README disclaimer before flipping it.
EOF
