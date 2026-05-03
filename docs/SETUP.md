# Setup — detailed

The top-level [README](../README.md) has the minimal install path. This
doc covers the parts that need more nuance: the LM Studio recipe,
context sizing, and the frontend `.env.local`.

---

## LM Studio (local AI) — full recipe

LM Studio runs an OpenAI-compatible server on port 1234. Install from
[lmstudio.ai](https://lmstudio.ai), then use the `lms` CLI — everything
below is terminal-only.

### 1. Install the CLI and update the CUDA runtime

```bash
# The CLI is bundled with the desktop app. Either launch the app once, or:
~/.lmstudio/bin/lms bootstrap
lms runtime ls                    # should list llama.cpp CUDA runtime
lms runtime update --yes          # critical — newer MoE archs need >= 2.13
```

If `lms runtime ls` shows a runtime older than `2.13.0`, `lms load` will
fail silently with `(X) CAUSE Failed to load model`. Update first.

### 2. Download a tool-calling model

The backend *requires* a model that supports OpenAI-style tool/function
calling. Tested defaults:

| Model                                            | Size (Q5) | Context | Notes                                                       |
|--------------------------------------------------|-----------|---------|-------------------------------------------------------------|
| `unsloth/Qwen3.6-35B-A3B-GGUF`                   | ~27 GB    | 256 K   | 35 B MoE, 3 B active. Recommended — fast, reliable tools.   |
| `qwen/qwen3-coder-30b`                           | ~18 GB    | 256 K   | Smaller fallback. Slightly weaker tool use.                 |
| `lmstudio-community/Llama-3.3-70B-Instruct-GGUF` | ~48 GB    | 128 K   | Slow on a single A6000 but very strong reasoning.           |

Non-tool-calling models (most "chat" models) *will not work* — the agent
will appear to run but never emit a trade proposal.

```bash
# Option A — LM Studio catalog
lms get qwen/qwen3-coder-30b

# Option B — direct HuggingFace pull (for models not in the catalog)
mkdir -p ~/.lmstudio/models/unsloth/Qwen3.6-35B-A3B-GGUF
curl -L --continue-at - \
  https://huggingface.co/unsloth/Qwen3.6-35B-A3B-GGUF/resolve/main/Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf \
  -o ~/.lmstudio/models/unsloth/Qwen3.6-35B-A3B-GGUF/Qwen3.6-35B-A3B-UD-Q5_K_XL.gguf
lms ls                            # confirm LM Studio indexed it
```

### 3. Load the model

**Max-out recipe (48 GB GPU, Qwen3 family)** — run in order:

```bash
# 1. Kill anything already loaded so VRAM is clean.
lms unload --all

# 2. Load at 256 K (Qwen3's YaRN-extended ceiling — loading beyond this
#    works but quality degrades past the trained window).
lms load qwen3.6-35b-a3b \
  --context-length 262144 \
  --gpu 1.0 \
  --identifier qwen3.6-35b-a3b \
  -y

# 3. Confirm it loaded at the requested size.
lms ps        # `loaded_context_length` should read 262144

# 4. Pair the backend's per-result cap to the loaded context.
#    Edit backend/.env:
#       RESEARCH_TOOL_RESULT_CHARS=131072
#    Then restart the backend (Ctrl-C + restart). pydantic-settings
#    caches at import, so --reload alone is not enough.

# 5. Sanity check in the frontend. The AIStatus badge (top-right of any
#    page) should read `ctx 256k`. Open /research and ask about a
#    ticker — you should see multi-tool rounds complete without any
#    `[Context trimmed]` markers.
```

**Context-size guide** for other GPU sizes:

| Loaded context | Command flag              | GPU headroom needed | `RESEARCH_TOOL_RESULT_CHARS` |
|----------------|---------------------------|---------------------|------------------------------|
|  32 K          | `--context-length 32768`  | 8 GB                | 8000                         |
|  64 K          | `--context-length 65536`  | 16 GB               | 16000                        |
| 128 K          | `--context-length 131072` | 32 GB               | 65536 (default)              |
| 256 K          | `--context-length 262144` | 40 GB               | 131072 (max recipe above)    |

Sizes assume full-precision K/V cache. Numbers scale down roughly 2×
with Q8 KV-cache quantization, 4× with Q4 — enable in the LM Studio
desktop app under model settings if you're VRAM-tight. Don't push past
262144 even if VRAM allows: Qwen3 was YaRN-extended to that ceiling,
and attention quality drops noticeably beyond it.

**To change context later**: `lms unload --all` then `lms load ...
--context-length <N>`. The app reads context size from LM Studio at
runtime via `/api/v0/models`, so no backend restart is needed for context
changes alone — but if the model *identifier* changes you must update
`LMSTUDIO_MODEL` in `backend/.env` and restart. After a reload, also
revisit `RESEARCH_TOOL_RESULT_CHARS` (see table above) so per-tool-result
payloads scale with the new ceiling.

**Keep the LM Studio server running.** Either leave the desktop app open
or run `lms server start` in the background. The backend talks to
`http://localhost:1234/v1` by default.

### 4. Point the backend at LM Studio

In `backend/.env`:

```
AI_PROVIDER=lmstudio
LMSTUDIO_BASE_URL=http://localhost:1234/v1
LMSTUDIO_MODEL=qwen3.6-35b-a3b    # exact identifier from `lms ps`
```

Then start the backend (`python -m app.main`). Restart the backend any
time `.env` changes — pydantic-settings caches at process startup.

---

## Frontend `.env.local`

```
NEXT_PUBLIC_API_URL=http://127.0.0.1:3003/api
NEXT_PUBLIC_API_KEY=<same value as backend JWT_SECRET>

# Optional — comma-separated LAN hosts allowed to load the dev server.
# Set this if you want to open the dashboard from a phone or tablet on
# your LAN. Add the same host to backend CORS_ORIGINS as well.
NEXT_DEV_ORIGINS=
```

`NEXT_PUBLIC_*` is baked into the bundle at build time — restart
`npm run dev` after editing `.env.local`. The API key is visible in the
browser, so it is *not* a secret; it's a shared password that scopes the
backend to your machine. Don't expose the backend publicly.

---

## Backend `.env` — minimum + LAN

Minimum for paper stocks + local AI:

```
PAPER_MODE=true
APP_HOST=0.0.0.0
APP_PORT=3003
JWT_SECRET=<openssl rand -hex 32>

AI_PROVIDER=lmstudio
LMSTUDIO_MODEL=<id from `lms ps`>

ALPACA_API_KEY=<paper key>
ALPACA_API_SECRET=<paper secret>
ALPACA_BASE_URL=https://paper-api.alpaca.markets

# Add your LAN IP here when running the dashboard from a phone/tablet:
CORS_ORIGINS=http://localhost:3010,http://127.0.0.1:3010
```

Required env for **OpenRouter** instead:

```
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-...
CLAUDE_MODEL=anthropic/claude-sonnet-4.5
```

See [CONFIGURATION.md](CONFIGURATION.md) for every other knob.
