"""Fail-fast validation: LIVE mode must refuse placeholder creds.

Paper mode is permissive (so the UI boots without real keys during dev),
but a LIVE flip with `replace_me` placeholders would silently connect to
paper endpoints while the user thinks they're trading real money. Prefer
a loud RuntimeError on boot.
"""

from __future__ import annotations

import pytest

from app.config import Settings


def _live_settings(**overrides) -> Settings:
    base: dict = {
        "paper_mode": False,
        "ai_provider": "openrouter",
        "openrouter_api_key": "sk-or-real",
        "alpaca_api_key": "AK_real",
        "alpaca_api_secret": "AS_real",
        "jwt_secret": "a" * 64,
        "polymarket_private_key": "0xrealkey",
    }
    base.update(overrides)
    return Settings(**base)


def test_assert_passes_when_all_secrets_set():
    settings = _live_settings()
    settings.assert_secrets_configured()  # no raise


def test_assert_refuses_placeholder_openrouter_key():
    settings = _live_settings(openrouter_api_key="replace_me")
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        settings.assert_secrets_configured()


def test_assert_refuses_placeholder_alpaca():
    settings = _live_settings(alpaca_api_key="replace_me")
    with pytest.raises(RuntimeError, match="ALPACA_API_KEY"):
        settings.assert_secrets_configured()


def test_assert_refuses_placeholder_jwt():
    settings = _live_settings(jwt_secret="replace_me_with_something")
    with pytest.raises(RuntimeError, match="JWT_SECRET"):
        settings.assert_secrets_configured()


def test_assert_ignores_polymarket_by_default():
    # Stocks-only user doesn't need a Polymarket key to go live.
    settings = _live_settings(polymarket_private_key="replace_me")
    settings.assert_secrets_configured()


def test_assert_requires_polymarket_when_asked():
    settings = _live_settings(polymarket_private_key="replace_me")
    with pytest.raises(RuntimeError, match="POLYMARKET_PRIVATE_KEY"):
        settings.assert_secrets_configured(require_polymarket=True)


def test_openrouter_key_ignored_when_using_lmstudio():
    settings = _live_settings(
        ai_provider="lmstudio", openrouter_api_key="replace_me"
    )
    settings.assert_secrets_configured()  # no raise
