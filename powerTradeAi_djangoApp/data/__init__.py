"""Capa de datos de mercado."""
from __future__ import annotations

from django.conf import settings

from .base import (
    BAR_COLUMNS,
    MarketDataError,
    MarketDataProvider,
    Quote,
    candidate_expirations,
    occ_symbol,
    parse_occ,
)

__all__ = [
    "BAR_COLUMNS", "MarketDataError", "MarketDataProvider", "Quote",
    "candidate_expirations", "occ_symbol", "parse_occ", "get_provider",
]

_CACHE: dict[str, MarketDataProvider] = {}


def get_provider(name: str | None = None) -> MarketDataProvider:
    """Devuelve el proveedor configurado.

    Se elige con ``POWERTRADEAI["MARKET_DATA_PROVIDER"]``. Por defecto
    ``thetadata``, que es el feed con el que se validaron las reglas: cambiarlo
    a ``alpaca`` es legitimo, pero desvia de los backtests causales.
    """
    config = getattr(settings, "POWERTRADEAI", {})
    name = (name or config.get("MARKET_DATA_PROVIDER") or "thetadata").lower()
    if name in _CACHE:
        return _CACHE[name]

    if name == "thetadata":
        from .thetadata_cloud import ThetaDataCloudProvider
        provider = ThetaDataCloudProvider(config.get("THETADATA_API_KEY"))
    elif name == "alpaca":
        from .alpaca_provider import AlpacaProvider
        provider = AlpacaProvider(
            api_key=config.get("ALPACA_API_KEY"),
            api_secret=config.get("ALPACA_API_SECRET"),
            feed=config.get("ALPACA_FEED", "iex"),
        )
    elif name == "hybrid":
        from .hybrid import HybridProvider
        provider = HybridProvider(
            stock_provider=get_provider(
                config.get("HYBRID_STOCK_PROVIDER", "alpaca")),
            option_provider=get_provider(
                config.get("HYBRID_OPTION_PROVIDER", "thetadata")),
        )
    else:
        raise MarketDataError(f"Proveedor de datos desconocido: {name!r}")

    _CACHE[name] = provider
    return provider
