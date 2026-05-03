"""Runtime health probe for the configured AI provider.

Powers the dashboard status panel: which model is configured, is the
endpoint reachable, which models are loaded (LM Studio only), and GPU
memory/utilisation per card.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

import httpx

from app.api.schemas import AIModelInfo, AIStatusOut, GPUInfo
from app.models import utc_now

logger = logging.getLogger(__name__)


async def collect_ai_status(settings) -> AIStatusOut:
    provider = (settings.ai_provider or "openrouter").lower()
    if provider == "lmstudio":
        base_url = settings.lmstudio_base_url
        configured = settings.lmstudio_model
    else:
        base_url = "https://openrouter.ai/api/v1"
        configured = settings.claude_model

    reachable = False
    reachable_error: str | None = None
    models: list[AIModelInfo] = []
    loaded_id: str | None = None
    configured_state: str | None = None

    try:
        if provider == "lmstudio":
            models = await _fetch_lmstudio_models(base_url)
        else:
            # OpenRouter doesn't expose a "loaded model" concept, so we just
            # ping /models as a reachability check without populating the list.
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{base_url}/models")
                r.raise_for_status()
        reachable = True
    except Exception as exc:  # noqa: BLE001
        reachable_error = f"{type(exc).__name__}: {exc}"

    for m in models:
        if m.state == "loaded" and loaded_id is None:
            loaded_id = m.id
        if m.id == configured:
            configured_state = m.state

    gpus = await asyncio.to_thread(_collect_gpus)

    return AIStatusOut(
        provider=provider,
        model_configured=configured,
        base_url=base_url,
        reachable=reachable,
        reachable_error=reachable_error,
        loaded_model_id=loaded_id,
        configured_model_state=configured_state,
        models=models,
        gpus=gpus,
        checked_at=utc_now(),
    )


async def _fetch_lmstudio_models(base_url: str) -> list[AIModelInfo]:
    """LM Studio ships a REST extension at /api/v0/models with richer
    per-model metadata than the OpenAI-compatible /v1/models (it includes
    loaded state, quantisation, capabilities, etc)."""
    # base_url ends in "/v1"; swap to the v0 extension path
    root = base_url.rsplit("/v1", 1)[0]
    url = f"{root}/api/v0/models"
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        payload = r.json()
    out: list[AIModelInfo] = []
    for item in payload.get("data", []):
        if item.get("type") == "embeddings":
            continue
        out.append(
            AIModelInfo(
                id=str(item.get("id")),
                state=item.get("state"),
                arch=item.get("arch"),
                quantization=item.get("quantization"),
                max_context_length=item.get("max_context_length"),
                loaded_context_length=item.get("loaded_context_length"),
                capabilities=list(item.get("capabilities") or []),
            )
        )
    return out


def _collect_gpus() -> list[GPUInfo]:
    """Best-effort VRAM snapshot via nvidia-smi. Returns [] on non-NVIDIA
    hosts or if the binary isn't on PATH."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    import subprocess

    try:
        out = subprocess.check_output(
            [
                smi,
                "--query-gpu=index,name,memory.total,memory.used,memory.free,utilization.gpu",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=3.0,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("nvidia-smi failed: %s", exc)
        return []

    gpus: list[GPUInfo] = []
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            gpus.append(
                GPUInfo(
                    index=int(parts[0]),
                    name=parts[1],
                    total_mb=int(parts[2]),
                    used_mb=int(parts[3]),
                    free_mb=int(parts[4]),
                    utilization_pct=int(parts[5]) if parts[5].isdigit() else None,
                )
            )
        except ValueError:
            continue
    return gpus


__all__ = ["collect_ai_status"]
