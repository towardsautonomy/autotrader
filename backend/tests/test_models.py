from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models import (
    AuditLog,
    Base,
    Decision,
    Halt,
    RiskConfigRow,
    SystemState,
    Trade,
    TradeStatus,
    utc_now,
)
from app.risk import RiskConfig


@pytest.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    async with SessionLocal() as s:
        yield s
    await engine.dispose()


async def test_risk_config_roundtrip(session):
    dc = RiskConfig(budget_cap=500, blacklist=("GME",))
    row = RiskConfigRow.from_dataclass(dc, changed_by="tester")
    row.is_active = True
    session.add(row)
    await session.commit()

    reloaded = row.to_dataclass()
    assert reloaded.budget_cap == 500
    assert reloaded.blacklist == ("GME",)


async def test_trade_and_decision_fk(session):
    decision = Decision(
        market="stocks",
        model="claude-opus-4-7",
        prompt_json={"system": "test"},
        response_json={"output": "buy SPY"},
        proposal_json={"symbol": "SPY", "size_usd": 50},
        approved=True,
        executed=True,
    )
    session.add(decision)
    await session.flush()

    trade = Trade(
        decision_id=decision.id,
        market="stocks",
        symbol="SPY",
        action="open_long",
        size_usd=50.0,
        entry_price=500.0,
        status=TradeStatus.OPEN,
        paper_mode=True,
        opened_at=utc_now(),
    )
    session.add(trade)
    await session.commit()

    assert trade.id is not None
    assert trade.decision_id == decision.id


async def test_system_state_singleton(session):
    session.add(SystemState(id=1, trading_enabled=True))
    await session.commit()


async def test_halt_roundtrip(session):
    h = Halt(reason_code="daily_loss_halt", reason="test", started_at=utc_now())
    session.add(h)
    await session.commit()
    assert h.id is not None


async def test_audit_log(session):
    log = AuditLog(event_type="kill_switch", message="user pressed kill", payload={"who": "user"})
    session.add(log)
    await session.commit()
    assert log.id is not None
