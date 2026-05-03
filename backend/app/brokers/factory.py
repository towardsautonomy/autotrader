from __future__ import annotations

from app.config import Settings
from app.risk import Market

from .base import BrokerAdapter


def _is_placeholder(value: str) -> bool:
    return not value or "replace_me" in value


def build_broker(market: Market, settings: Settings) -> BrokerAdapter:
    if market == Market.STOCKS:
        if _is_placeholder(settings.alpaca_api_key) or _is_placeholder(settings.alpaca_api_secret):
            from .null import NullBroker

            return NullBroker(Market.STOCKS, "ALPACA_API_KEY/SECRET not configured")

        from .alpaca import AlpacaBroker

        return AlpacaBroker(
            api_key=settings.alpaca_api_key,
            api_secret=settings.alpaca_api_secret,
            paper=settings.paper_mode,
            data_url=settings.alpaca_data_url,
        )
    if market == Market.POLYMARKET:
        if not settings.polymarket_enabled:
            from .null import NullBroker

            return NullBroker(
                Market.POLYMARKET,
                "POLYMARKET_ENABLED=false (adapter is experimental)",
            )
        if _is_placeholder(settings.polymarket_private_key):
            from .null import NullBroker

            return NullBroker(Market.POLYMARKET, "POLYMARKET_PRIVATE_KEY not configured")

        from .polymarket import PolymarketBroker

        return PolymarketBroker(
            private_key=settings.polymarket_private_key,
            clob_api_key=settings.polymarket_clob_api_key,
            clob_secret=settings.polymarket_clob_secret,
            clob_passphrase=settings.polymarket_clob_passphrase,
            host=settings.polymarket_clob_url,
            chain_id=settings.polymarket_chain_id,
            paper=settings.paper_mode,
        )
    raise ValueError(f"no broker for market {market}")
