"""Proveedor ThetaData via la libreria Python v3 (cloud, sin Theta Terminal).

Importa: este es el feed con el que se validaron las reglas en ``research/``.
Usarlo en produccion es lo que mantiene la paridad con los backtests causales.

La libreria se conecta directo a los servidores de ThetaData por HTTPS/gRPC, asi
que funciona en Render sin ningun proceso local. Requiere Python 3.12+.

ADVERTENCIA sobre nombres de columna: las firmas de los metodos estan
verificadas contra la libreria instalada, pero los nombres exactos de las
columnas del DataFrame NO pudieron verificarse sin una cuenta activa. Por eso
todo pasa por ``_normalize_*``, que acepta los alias plausibles y falla con un
error explicito si no encuentra lo que necesita — en vez de devolver silencio o
un numero equivocado.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from functools import lru_cache

import pandas as pd

from .base import BAR_COLUMNS, MarketDataError, Quote, parse_occ

log = logging.getLogger(__name__)

NY = "America/New_York"

# Alias aceptados por columna canonica.
_BAR_ALIASES = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "volume": ("volume", "v", "size"),
}
_TS_ALIASES = ("ms_of_day", "timestamp", "ts", "datetime", "date_time", "time")
_DATE_ALIASES = ("date", "trade_date", "session_date")

# Timeframes canonicos -> intervalo de la API.
_THETA_INTERVAL = {"1m": "1m", "15m": "15m", "1h": "1h", "1d": "1d"}
_BID_ALIASES = ("bid", "bid_price", "best_bid")
_ASK_ALIASES = ("ask", "ask_price", "best_ask")


def _pick(frame: pd.DataFrame, aliases: tuple[str, ...]) -> str | None:
    lowered = {str(c).lower(): c for c in frame.columns}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    return None


class ThetaDataCloudProvider:
    name = "thetadata_cloud"

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                from thetadata import ThetaClient
            except ImportError as exc:  # pragma: no cover - entorno sin la lib
                raise MarketDataError(
                    "Falta la libreria 'thetadata'. Instalala con: pip install thetadata"
                ) from exc
            kwargs = {"dataframe_type": "pandas"}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            # Sin api_key la libreria lee THETADATA_API_KEY del entorno.
            self._client = ThetaClient(**kwargs)
        return self._client

    # --- Subyacente -----------------------------------------------------

    def bars_1m(self, symbol: str, session_date: date) -> pd.DataFrame:
        try:
            raw = self.client.stock_history_ohlc(
                symbol=symbol.upper(),
                interval="1m",
                date=session_date,
                start_time=time(9, 30),
                end_time=time(16, 0),
            )
        except Exception as exc:
            if type(exc).__name__ == "NoDataFoundError":
                return _empty_bars()
            raise MarketDataError(
                f"ThetaData: fallo al pedir velas 1m de {symbol} {session_date}: {exc}"
            ) from exc
        return _normalize_bars(raw, session_date)

    def bars(self, symbol: str, start: date, end: date,
             timeframe: str = "1m") -> pd.DataFrame:
        try:
            raw = self.client.stock_history_ohlc(
                symbol=symbol.upper(),
                interval=_THETA_INTERVAL[timeframe],
                start_date=start, end_date=end,
                start_time=time(9, 30), end_time=time(16, 0),
            )
        except KeyError:
            raise MarketDataError(
                f"Timeframe no soportado: {timeframe!r}. "
                f"Validos: {sorted(_THETA_INTERVAL)}") from None
        except Exception as exc:
            if type(exc).__name__ == "NoDataFoundError":
                return _empty_bars()
            raise MarketDataError(
                f"ThetaData: fallo el historial {timeframe} de {symbol} "
                f"{start}..{end}: {exc}") from exc
        # Con rango multi-dia el ms_of_day ya no basta para fechar la vela: se
        # exige una columna de fecha explicita.
        return _normalize_bars(raw, start, multi_day=True)

    def latest_price(self, symbol: str) -> float:
        try:
            raw = self.client.stock_snapshot_quote(symbol=symbol.upper())
        except Exception as exc:
            raise MarketDataError(
                f"ThetaData: fallo el snapshot de {symbol}: {exc}") from exc
        quote = _normalize_quote(raw)
        if quote is None or not quote.is_live:
            raise MarketDataError(f"ThetaData: sin quote viva para {symbol}")
        return quote.mid

    def trades(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        start_ny = _to_ny(start)
        end_ny = _to_ny(end)
        try:
            raw = self.client.stock_history_trade(
                symbol=symbol.upper(),
                start_date=start_ny.date(), end_date=end_ny.date(),
                start_time=start_ny.time(), end_time=end_ny.time(),
            )
        except Exception as exc:
            if type(exc).__name__ == "NoDataFoundError":
                return _empty_trades()
            raise MarketDataError(
                f"ThetaData: fallo el tape de {symbol}: {exc}") from exc
        return _normalize_trades(raw, start_ny.date())

    # --- Opciones -------------------------------------------------------

    def option_quotes(self, occ: str, start: datetime, end: datetime,
                      interval: str = "1s") -> pd.DataFrame:
        symbol, expiration, direction, strike = parse_occ(occ)
        start_ny, end_ny = _to_ny(start), _to_ny(end)
        try:
            raw = self.client.option_history_quote(
                symbol=symbol, expiration=expiration,
                strike=f"{strike:g}",
                right="call" if direction == "CALL" else "put",
                interval=interval,
                start_date=start_ny.date(), end_date=end_ny.date(),
                start_time=start_ny.time(), end_time=end_ny.time(),
            )
        except Exception as exc:
            if type(exc).__name__ == "NoDataFoundError":
                return _empty_quotes()
            raise MarketDataError(
                f"ThetaData: fallo la serie de quotes de {occ}: {exc}") from exc
        return _normalize_quote_series(raw, start_ny.date())

    def option_quote(self, occ: str, at: datetime | None = None) -> Quote | None:
        symbol, expiration, direction, strike = parse_occ(occ)
        right = "call" if direction == "CALL" else "put"
        strike_arg = f"{strike:g}"

        if at is None:
            try:
                raw = self.client.option_snapshot_quote(
                    symbol=symbol, expiration=expiration,
                    strike=strike_arg, right=right,
                )
            except Exception as exc:
                if type(exc).__name__ == "NoDataFoundError":
                    return None
                raise MarketDataError(
                    f"ThetaData: fallo el snapshot de {occ}: {exc}") from exc
            return _normalize_quote(raw)

        # Quote historica: PRIMER NBBO valido POSTERIOR al instante pedido,
        # que es la convencion de los replays causales ("primera quote
        # posterior a la decision", tolerancia 90s). ``option_at_time``
        # devolveria el ultimo NBBO conocido, que puede ser anterior al
        # instante de decision — un precio del pasado no es look-ahead, pero
        # en el segundo del quiebre es sistematicamente mas favorable.
        at_ny = pd.Timestamp(at).tz_convert(NY) if pd.Timestamp(at).tzinfo \
            else pd.Timestamp(at).tz_localize(NY)
        window_end = at_ny + pd.Timedelta(seconds=90)
        frame = self.option_quotes(
            occ, at_ny.to_pydatetime(), window_end.to_pydatetime(), interval="1s")
        if frame.empty:
            return None
        valid = frame[
            (frame["bid"] > 0) & (frame["ask"] > 0) & (frame["ask"] >= frame["bid"])
        ]
        if valid.empty:
            return None
        first = valid.iloc[0]
        return Quote(
            bid=float(first["bid"]), ask=float(first["ask"]),
            ts=valid.index[0].to_pydatetime(),
        )


# --- Normalizacion ------------------------------------------------------

def _empty_bars() -> pd.DataFrame:
    idx = pd.DatetimeIndex([], tz="UTC")
    return pd.DataFrame(columns=list(BAR_COLUMNS), index=idx)


def _normalize_bars(raw, session_date: date, multi_day: bool = False) -> pd.DataFrame:
    frame = _to_pandas(raw)
    if frame is None or frame.empty:
        return _empty_bars()

    ts_col = _pick(frame, _TS_ALIASES)
    if ts_col is None:
        raise MarketDataError(
            f"ThetaData: no encuentro columna de tiempo en {list(frame.columns)}")

    if str(ts_col).lower() == "ms_of_day":
        # ThetaData expresa el intradia como ms desde medianoche ET. El
        # ms_of_day por si solo no dice de que dia es la vela.
        date_col = _pick(frame, _DATE_ALIASES)
        if date_col is not None:
            base = pd.to_datetime(frame[date_col].astype(str)).dt.tz_localize(NY)
        elif multi_day:
            raise MarketDataError(
                "ThetaData: historial multi-dia con 'ms_of_day' pero sin columna "
                f"de fecha en {list(frame.columns)}; no puedo fechar las velas "
                "sin adivinar")
        else:
            base = pd.Timestamp(session_date).tz_localize(NY)
        index = base + pd.to_timedelta(frame[ts_col].astype("int64"), unit="ms")
    else:
        index = pd.to_datetime(frame[ts_col])
        if getattr(index, "dt", None) is not None:
            index = index.dt.tz_localize(NY) if index.dt.tz is None else index
    index = pd.DatetimeIndex(index).tz_convert("UTC")

    out = pd.DataFrame(index=index)
    for canonical, aliases in _BAR_ALIASES.items():
        col = _pick(frame, aliases)
        if col is None:
            if canonical == "volume":
                out[canonical] = 0.0
                continue
            raise MarketDataError(
                f"ThetaData: falta la columna '{canonical}' en {list(frame.columns)}")
        out[canonical] = pd.to_numeric(frame[col].to_numpy(), errors="coerce")

    out = out[~out.index.duplicated(keep="first")].sort_index()
    return out


def _normalize_quote(raw, ts: datetime | None = None) -> Quote | None:
    frame = _to_pandas(raw)
    if frame is None or frame.empty:
        return None
    bid_col = _pick(frame, _BID_ALIASES)
    ask_col = _pick(frame, _ASK_ALIASES)
    if bid_col is None or ask_col is None:
        raise MarketDataError(
            f"ThetaData: no encuentro bid/ask en {list(frame.columns)}")
    row = frame.iloc[-1]
    try:
        bid = float(row[bid_col])
        ask = float(row[ask_col])
    except (TypeError, ValueError):
        return None
    return Quote(bid=bid, ask=ask, ts=ts or datetime.now(timezone.utc))


def _to_ny(moment: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(moment)
    return ts.tz_localize(NY) if ts.tzinfo is None else ts.tz_convert(NY)


def _empty_trades() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["price", "size"], index=pd.DatetimeIndex([], tz="UTC"))


def _empty_quotes() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["bid", "ask"], index=pd.DatetimeIndex([], tz="UTC"))


def _series_index(frame: pd.DataFrame, session_date) -> pd.DatetimeIndex:
    """Indice UTC a partir de ms_of_day (+ fecha) o de una columna temporal."""
    ts_col = _pick(frame, _TS_ALIASES)
    if ts_col is None:
        raise MarketDataError(
            f"ThetaData: no encuentro columna de tiempo en {list(frame.columns)}")
    if str(ts_col).lower() == "ms_of_day":
        date_col = _pick(frame, _DATE_ALIASES)
        base = (pd.to_datetime(frame[date_col].astype(str)).dt.tz_localize(NY)
                if date_col is not None
                else pd.Timestamp(session_date).tz_localize(NY))
        index = base + pd.to_timedelta(frame[ts_col].astype("int64"), unit="ms")
    else:
        index = pd.to_datetime(frame[ts_col])
        if getattr(index, "dt", None) is not None and index.dt.tz is None:
            index = index.dt.tz_localize(NY)
    return pd.DatetimeIndex(index).tz_convert("UTC")


def _normalize_trades(raw, session_date) -> pd.DataFrame:
    frame = _to_pandas(raw)
    if frame is None or frame.empty:
        return _empty_trades()
    price_col = _pick(frame, ("price", "trade_price", "last"))
    size_col = _pick(frame, ("size", "volume", "quantity"))
    if price_col is None or size_col is None:
        raise MarketDataError(
            f"ThetaData: no encuentro price/size en {list(frame.columns)}")
    out = pd.DataFrame({
        "price": pd.to_numeric(frame[price_col].to_numpy(), errors="coerce"),
        "size": pd.to_numeric(frame[size_col].to_numpy(), errors="coerce"),
    }, index=_series_index(frame, session_date))
    return out.dropna().sort_index()


def _normalize_quote_series(raw, session_date) -> pd.DataFrame:
    frame = _to_pandas(raw)
    if frame is None or frame.empty:
        return _empty_quotes()
    bid_col = _pick(frame, _BID_ALIASES)
    ask_col = _pick(frame, _ASK_ALIASES)
    if bid_col is None or ask_col is None:
        raise MarketDataError(
            f"ThetaData: no encuentro bid/ask en {list(frame.columns)}")
    out = pd.DataFrame({
        "bid": pd.to_numeric(frame[bid_col].to_numpy(), errors="coerce"),
        "ask": pd.to_numeric(frame[ask_col].to_numpy(), errors="coerce"),
    }, index=_series_index(frame, session_date))
    return out.dropna().sort_index()


def _to_pandas(raw) -> pd.DataFrame | None:
    """Acepta pandas o polars: la libreria devuelve polars por defecto."""
    if raw is None:
        return None
    if isinstance(raw, pd.DataFrame):
        return raw
    to_pandas = getattr(raw, "to_pandas", None)
    if callable(to_pandas):
        return to_pandas()
    return pd.DataFrame(raw)


@lru_cache(maxsize=1)
def default_provider(api_key: str | None = None) -> ThetaDataCloudProvider:
    return ThetaDataCloudProvider(api_key)
