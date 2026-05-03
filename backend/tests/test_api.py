"""Smoke tests for FastAPI routes covering auth + kill switch + risk config."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import _SessionLocal  # noqa: F401  (import to allow patching)
from app.main import create_app
from app.models import Base, RiskConfigRow, SystemState

TEST_API_KEY = "test-secret-jwt"


@pytest.fixture(autouse=True)
def patch_settings(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", TEST_API_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-xxx")
    monkeypatch.setenv("ALPACA_API_KEY", "alpaca-k")
    monkeypatch.setenv("ALPACA_API_SECRET", "alpaca-s")
    # reset cached settings
    from app.config import get_settings
    get_settings.cache_clear()
    yield


@pytest.fixture
async def client(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as s:
        s.add(SystemState(id=1, trading_enabled=True))
        from app.risk import RiskConfig
        s.add(RiskConfigRow.from_dataclass(RiskConfig(), changed_by="seed"))
        await s.flush()
        row = (await s.execute(select(RiskConfigRow).limit(1))).scalars().first()
        row.is_active = True
        await s.commit()

    import app.db as app_db
    app_db._engine = engine
    app_db._SessionLocal = SessionLocal

    app = create_app()
    # Skip the init_db/seed hook — we've already seeded
    app.router.lifespan_context = None  # type: ignore[attr-defined]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c
    await engine.dispose()


async def test_health_is_unauthenticated(client):
    r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {"status": "ok"}


async def test_system_status_requires_api_key(client):
    r = await client.get("/api/system/status")
    assert r.status_code == 401


async def test_system_status_with_api_key(client):
    hdr = {"X-API-Key": TEST_API_KEY}
    r = await client.get("/api/system/status", headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["mode"] in {"PAPER", "LIVE"}
    assert "scheduler" in body


async def test_unauthorized_without_api_key(client):
    r = await client.get("/api/risk-config")
    assert r.status_code == 401


async def test_risk_config_roundtrip(client):
    hdr = {"X-API-Key": TEST_API_KEY}
    r = await client.get("/api/risk-config", headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["budget_cap"] > 0

    new_body = {
        "budget_cap": 500,
        "max_position_pct": 0.05,
        "max_concurrent_positions": 3,
        "max_daily_trades": 5,
        "daily_loss_cap_pct": 0.02,
        "max_drawdown_pct": 0.10,
        "default_stop_loss_pct": 0.03,
        "default_take_profit_pct": 0.06,
        "min_trade_size_usd": 1.0,
        "blacklist": ["GME", "SPCE"],
    }
    r = await client.put("/api/risk-config", headers=hdr, json=new_body)
    assert r.status_code == 200
    assert r.json()["budget_cap"] == 500
    assert r.json()["blacklist"] == ["GME", "SPCE"]


async def test_llm_call_log_round_trip(client):
    """Seed a usage row and verify /llm/calls + /llm/calls/{id} return it."""
    hdr = {"X-API-Key": TEST_API_KEY}

    import app.db as app_db
    from app.models import LlmUsageRow

    assert app_db._SessionLocal is not None
    async with app_db._SessionLocal() as s:
        row = LlmUsageRow(
            provider="fake",
            model="fake-model",
            purpose="research_agent",
            agent_id="research-aapl",
            round_idx=0,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            cost_usd=0.01,
            prompt_messages=[{"role": "user", "content": "hi"}],
            response_body={"choices": [{"message": {"content": "ok"}}]},
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        row_id = row.id

    r = await client.get(
        "/api/llm/calls", headers=hdr, params={"agent_id": "research-aapl"}
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["id"] == row_id
    assert body[0]["agent_id"] == "research-aapl"
    # List endpoint does not include the bodies.
    assert "prompt_messages" not in body[0]

    r = await client.get(f"/api/llm/calls/{row_id}", headers=hdr)
    assert r.status_code == 200
    detail = r.json()
    assert detail["prompt_messages"] == [{"role": "user", "content": "hi"}]
    assert detail["response_body"]["choices"][0]["message"]["content"] == "ok"

    r = await client.get("/api/llm/calls/9999", headers=hdr)
    assert r.status_code == 404


async def test_kill_switch_requires_confirm_string(client):
    hdr = {"X-API-Key": TEST_API_KEY}
    r = await client.post("/api/kill-switch", headers=hdr, json={"confirm": "yes"})
    assert r.status_code == 400

    r = await client.post(
        "/api/kill-switch", headers=hdr, json={"confirm": "KILL", "reason": "test"}
    )
    assert r.status_code == 200
    assert r.json()["trading_enabled"] is False

    r = await client.post("/api/unpause", headers=hdr)
    assert r.status_code == 200
    assert r.json()["trading_enabled"] is True
