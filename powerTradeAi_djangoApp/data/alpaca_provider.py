"""Proveedor Alpaca para el SUBYACENTE.

Este es el feed con el que se validaron las reglas: ``research/`` construye las
velas del subyacente con ``proxy/provider.py``, que es Alpaca con ``FEED="iex"``,
y las quotes de opciones con ThetaData. Mantener esa division —acciones aqui,
opciones en ThetaData— es lo que conserva la paridad con los backtests; usarlo
como proveedor unico, no.

El feed ``iex`` no es el consolidado SIP y esa diferencia ya produjo un veredicto
falso en este proyecto. Se mantiene ``iex`` por defecto justamente porque es el
que produjo los artefactos causales: cambiarlo a SIP mejoraria el dato pero
dejaria de reproducirlos.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pandas as pd

from .base import BAR_COLUMNS, MarketDataError, Quote

log = logging.getLogger(__name__)
NY = ZoneInfo("America/New_York")

# Timeframes canonicos -> (cantidad, unidad de alpaca-py).
_ALPACA_TIMEFRAME = {
    "1m": (1, "Minute"), "15m": (15, "Minute"),
    "1h": (1, "Hour"), "1d": (1, "Day"), "1w": (1, "Week"),
}


class AlpacaProvider:
    name = "alpaca"

    def __init__(self, api_key: str | None, api_secret: str | None, feed: str = "iex"):
        self._api_key = api_key
        self._api_secret = api_secret
        self._feed = feed
        self._stock = None
        self._option = None

    def _clients(self):
        if self._stock is None:
            try:
                from alpaca.data.historical import (
                    OptionHistoricalDataClient, StockHistoricalDataClient,
                )
            except ImportError as exc:  # pragma: no cover
                raise MarketDataError(
                    "Falta 'alpaca-py'. Instalalo con: pip install alpaca-py") from exc
            self._stock = StockHistoricalDataClient(self._api_key, self._api_secret)
            self._option = OptionHistoricalDataClient(self._api_key, self._api_secret)
        return self._stock, self._option

    def bars_1m(self, symbol: str, session_date: date) -> pd.DataFrame:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        stock, _ = self._clients()
        start = datetime.combine(session_date, time(9, 30), tzinfo=NY)
        end = datetime.combine(session_date, time(16, 0), tzinfo=NY)
        try:
            response = stock.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol.upper(),
                timeframe=TimeFrame.Minute,
                start=start, end=end, feed=self._feed,
            ))
        except Exception as exc:
            raise MarketDataError(
                f"Alpaca: fallo al pedir velas de {symbol} {session_date}: {exc}"
            ) from exc

        frame = response.df
        if frame is None or frame.empty:
            idx = pd.DatetimeIndex([], tz="UTC")
            return pd.DataFrame(columns=list(BAR_COLUMNS), index=idx)
        if isinstance(frame.index, pd.MultiIndex):
            frame = frame.xs(symbol.upper(), level="symbol")

        out = frame[[c for c in BAR_COLUMNS if c in frame.columns]].copy()
        out.index = pd.DatetimeIndex(out.index).tz_convert("UTC")
        # Alpaca puede devolver el ultimo bar del rango; el replay causal trata
        # el timestamp como inicio de vela, que es la convencion de Alpaca.
        return out[~out.index.duplicated(keep="first")].sort_index()

    def bars(self, symbol: str, start: date, end: date,
             timeframe: str = "1m") -> pd.DataFrame:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

        try:
            amount, unit = _ALPACA_TIMEFRAME[timeframe]
        except KeyError:
            raise MarketDataError(
                f"Timeframe no soportado: {timeframe!r}. "
                f"Validos: {sorted(_ALPACA_TIMEFRAME)}") from None

        stock, _ = self._clients()
        try:
            response = stock.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=symbol.upper(),
                timeframe=TimeFrame(amount, getattr(TimeFrameUnit, unit)),
                start=datetime.combine(start, time(0, 0), tzinfo=NY),
                end=datetime.combine(end, time(23, 59), tzinfo=NY),
                feed=self._feed,
            ))
        except Exception as exc:
            raise MarketDataError(
                f"Alpaca: fallo el historial {timeframe} de {symbol} "
                f"{start}..{end}: {exc}") from exc

        frame = response.df
        if frame is None or frame.empty:
            idx = pd.DatetimeIndex([], tz="UTC")
            return pd.DataFrame(columns=list(BAR_COLUMNS), index=idx)
        if isinstance(frame.index, pd.MultiIndex):
            frame = frame.xs(symbol.upper(), level="symbol")
        out = frame[[c for c in BAR_COLUMNS if c in frame.columns]].copy()
        out.index = pd.DatetimeIndex(out.index).tz_convert("UTC")
        return out[~out.index.duplicated(keep="first")].sort_index()

    def latest_price(self, symbol: str) -> float:
        from alpaca.data.requests import StockLatestQuoteRequest

        stock, _ = self._clients()
        try:
            data = stock.get_stock_latest_quote(StockLatestQuoteRequest(
                symbol_or_symbols=symbol.upper(), feed=self._feed))
        except Exception as exc:
            raise MarketDataError(f"Alpaca: sin quote de {symbol}: {exc}") from exc
        quote = data[symbol.upper()]
        bid, ask = float(quote.bid_price or 0), float(quote.ask_price or 0)
        if bid <= 0 or ask <= 0:
            raise MarketDataError(f"Alpaca: quote no utilizable para {symbol}")
        return (bid + ask) / 2

    def trades(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        from alpaca.data.requests import StockTradesRequest

        stock, _ = self._clients()
        try:
            response = stock.get_stock_trades(StockTradesRequest(
                symbol_or_symbols=symbol.upper(),
                start=start, end=end, feed=self._feed,
            ))
        except Exception as exc:
            raise MarketDataError(
                f"Alpaca: fallo el tape de {symbol}: {exc}") from exc

        frame = response.df
        if frame is None or frame.empty:
            return pd.DataFrame(
                columns=["price", "size"], index=pd.DatetimeIndex([], tz="UTC"))
        if isinstance(frame.index, pd.MultiIndex):
            frame = frame.xs(symbol.upper(), level="symbol")
        out = frame[["price", "size"]].astype("float64").dropna()
        out.index = pd.DatetimeIndex(out.index).tz_convert("UTC")
        return out.sort_index()

    # -- Limitacion de Alpaca --------------------------------------------
    #
    # alpaca-py NO expone quotes historicas de opciones: en 0.43.4 solo existen
    # OptionLatestQuoteRequest, OptionBarsRequest, OptionTradesRequest y
    # OptionChainRequest. No hay equivalente a "dame el NBBO de este contrato a
    # las 10:21:00".
    #
    # Se podria aproximar con OptionBarsRequest (precios de operacion) o con el
    # ultimo trade, pero la convencion de P&L de este proyecto liquida la salida
    # al BID. Sustituir un bid por un precio de operacion cambia el resultado sin
    # avisar, que es justo el tipo de error que ya produjo un veredicto falso
    # aqui. Preferimos fallar con un mensaje explicito.

    _HISTORICAL_OPTION_MSG = (
        "Alpaca no sirve quotes historicas de opciones (alpaca-py no expone "
        "ningun endpoint para ello). Las alertas no se pueden resolver ni "
        "auditar con este proveedor: usa MARKET_DATA_PROVIDER='thetadata'."
    )

    def option_quotes(self, occ: str, start: datetime, end: datetime,
                      interval: str = "1s") -> pd.DataFrame:
        raise MarketDataError(self._HISTORICAL_OPTION_MSG)

    def option_quote(self, occ: str, at: datetime | None = None) -> Quote | None:
        from alpaca.data.requests import OptionLatestQuoteRequest

        if at is not None:
            raise MarketDataError(self._HISTORICAL_OPTION_MSG)

        _, option = self._clients()
        # El OCC de Alpaca no lleva el relleno de espacios del formato de 21 chars.
        alpaca_symbol = occ.replace(" ", "")
        try:
            data = option.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=alpaca_symbol))
        except Exception as exc:
            log.warning("Alpaca: sin quote para %s: %s", occ, exc)
            return None
        quote = data.get(alpaca_symbol)
        if quote is None:
            return None
        return Quote(
            bid=float(quote.bid_price or 0),
            ask=float(quote.ask_price or 0),
            ts=getattr(quote, "timestamp", None),
        )
