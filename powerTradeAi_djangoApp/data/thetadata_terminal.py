"""Proveedor local de opciones mediante Theta Terminal v3."""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from .base import MarketDataError, Quote, parse_occ


NY = "America/New_York"


class ThetaDataTerminalProvider:
    name = "thetadata_terminal"

    def __init__(self, base_url: str = "http://127.0.0.1:25503/v3"):
        self.base_url = base_url.rstrip("/")

    def option_quotes(self, occ: str, start: datetime, end: datetime,
                      interval: str = "1s") -> pd.DataFrame:
        symbol, expiration, direction, strike = parse_occ(occ)
        start_ny = _to_ny(start)
        end_ny = _to_ny(end)
        if start_ny.date() != end_ny.date():
            raise MarketDataError("Theta Terminal local requiere una sola fecha por consulta")
        params = {
            "symbol": symbol,
            "expiration": expiration.strftime("%Y%m%d"),
            "strike": f"{strike:g}",
            "right": "C" if direction == "CALL" else "P",
            "date": start_ny.strftime("%Y%m%d"),
            "interval": interval,
            "format": "csv",
        }
        try:
            response = requests.get(
                f"{self.base_url}/option/history/quote", params=params, timeout=120)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise MarketDataError(f"Theta Terminal: fallo al pedir {occ}: {exc}") from exc
        frame = pd.read_csv(io.StringIO(response.text))
        if frame.empty or "timestamp" not in frame.columns:
            return _empty_quotes()
        timestamps = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame.index = pd.DatetimeIndex(timestamps).tz_localize(NY).tz_convert("UTC")
        frame["bid"] = pd.to_numeric(frame.get("bid"), errors="coerce")
        frame["ask"] = pd.to_numeric(frame.get("ask"), errors="coerce")
        frame = frame[["bid", "ask"]].dropna().sort_index()
        lo, hi = pd.Timestamp(start).tz_convert("UTC"), pd.Timestamp(end).tz_convert("UTC")
        return frame[(frame.index >= lo) & (frame.index <= hi)]

    def option_quote(self, occ: str, at: datetime | None = None) -> Quote | None:
        if at is None:
            raise MarketDataError("Theta Terminal local requiere un instante historico")
        frame = self.option_quotes(occ, at, at + timedelta(seconds=90), interval="1s")
        valid = frame[(frame["bid"] > 0) & (frame["ask"] >= frame["bid"])]
        if valid.empty:
            return None
        row = valid.iloc[0]
        return Quote(float(row["bid"]), float(row["ask"]), valid.index[0].to_pydatetime())

    def bars_1m(self, symbol: str, session_date: date) -> pd.DataFrame:
        raise MarketDataError("Theta Terminal local se usa solo para opciones")

    def bars(self, symbol: str, start: date, end: date,
             timeframe: str = "1m") -> pd.DataFrame:
        raise MarketDataError("Theta Terminal local se usa solo para opciones")

    def trades(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        raise MarketDataError("Theta Terminal local se usa solo para opciones")

    def latest_price(self, symbol: str) -> float:
        raise MarketDataError("Theta Terminal local se usa solo para opciones")


def _to_ny(value: datetime) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(NY)
    return timestamp.tz_convert(NY)


def _empty_quotes() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["bid", "ask"], index=pd.DatetimeIndex([], tz="UTC"))
