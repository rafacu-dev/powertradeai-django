"""Proveedor compuesto: un origen para acciones y otro para opciones.

Existe por una razon concreta y verificada contra las suscripciones reales de
este proyecto (19-jul-2026):

  * Alpaca sirve acciones (velas, precio, tape) pero NO tiene ningun endpoint de
    quotes historicas de opciones, asi que no puede resolver una alerta.
  * ThetaData en tier FREE sirve TODO el lado de opciones (snapshot, at_time e
    history) pero deniega los endpoints de acciones: las velas intradia exigen
    tier ``value`` y el tape exige ``standard``.

Ninguno de los dos cubre el ciclo completo por separado; combinados, si.

Aviso de coherencia: el subyacente y la opcion vienen de feeds distintos. Para
decidir la senal eso da igual (solo se mira el subyacente) y para el P&L tambien
(solo se mira la prima), pero si algun dia una regla compara precio de accion
contra precio de opcion en el mismo instante, esa comparacion cruzaria dos
relojes distintos.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from .base import Quote


class HybridProvider:
    """Delega cada llamada al proveedor que puede servirla."""

    def __init__(self, stock_provider, option_provider):
        self.stock = stock_provider
        self.option = option_provider
        self.name = f"hybrid({stock_provider.name}+{option_provider.name})"

    # --- Subyacente: al proveedor de acciones ---------------------------

    def bars_1m(self, symbol: str, session_date: date) -> pd.DataFrame:
        return self.stock.bars_1m(symbol, session_date)

    def bars(self, symbol: str, start: date, end: date,
             timeframe: str = "1m") -> pd.DataFrame:
        return self.stock.bars(symbol, start, end, timeframe)

    def latest_price(self, symbol: str) -> float:
        return self.stock.latest_price(symbol)

    def trades(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        return self.stock.trades(symbol, start, end)

    # --- Opciones: al proveedor de opciones -----------------------------

    def option_quote(self, occ_symbol: str,
                     at: datetime | None = None) -> Quote | None:
        return self.option.option_quote(occ_symbol, at=at)

    def option_quotes(self, occ_symbol: str, start: datetime, end: datetime,
                      interval: str = "1s") -> pd.DataFrame:
        return self.option.option_quotes(occ_symbol, start, end, interval)
