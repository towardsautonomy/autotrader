from __future__ import annotations

from fastapi import Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_session_factory


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    # Use JWT_SECRET as the API token — single-user personal app, so one
    # shared secret is fine. Rotate via backend/.env.
    expected = get_settings().jwt_secret
    if not x_api_key or x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key"
        )


async def get_db() -> AsyncSession:
    factory = get_session_factory()
    async with factory() as session:
        yield session
