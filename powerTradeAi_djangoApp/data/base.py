"""Contrato de datos de mercado.

Las reglas se escriben contra esta interfaz, nunca contra un proveedor
concreto. Es lo que permite correr el mismo codigo con ThetaData (el feed que
valido las reglas en research/) o con Alpaca, y que un golden test lo alimente
desde un CSV sin tocar red.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable

import pandas as pd

BAR_COLUMNS = ("open", "high", "low", "close", "volume")


@dataclass(frozen=True)
class Quote:
    """Quote de un contrato. ``bid``/``ask`` en dolares por accion."""

    bid: float
    ask: float
    ts: datetime | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def is_live(self) -> bool:
        return self.bid > 0 and self.ask > 0

    @property
    def spread_pct(self) -> float | None:
        if self.ask <= 0:
            return None
        return (self.ask - self.bid) / self.ask


class MarketDataError(RuntimeError):
    pass


@runtime_checkable
class MarketDataProvider(Protocol):
    """Lo minimo que necesita cualquier regla para decidir y para resolverse."""

    name: str

    def bars_1m(self, symbol: str, session_date: date) -> pd.DataFrame:
        """Velas de 1 minuto de la sesion, indice UTC tz-aware, ordenado.

        El timestamp es el INICIO de la vela, igual que el replay causal. Las
        columnas son ``BAR_COLUMNS``. Devuelve un frame vacio si no hay datos.
        """
        ...

    def bars(self, symbol: str, start: date, end: date,
             timeframe: str = "1m") -> pd.DataFrame:
        """Historial multi-dia. ``timeframe`` en {'1m', '15m', '1h', '1d'}.

        Lo necesitan las reglas que miran mas alla de la sesion en curso: el
        cierre RTH previo (prev-close), la mediana de volumen de 20 sesiones, o
        los pivotes de soporte/resistencia de 30 dias (BB).
        """
        ...

    def trades(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Tape de operaciones: columnas ``price`` y ``size``, indice UTC.

        Lo necesitan las reglas de agresividad, que trabajan sobre barras de un
        segundo construidas desde el tape — no sobre velas de un minuto.
        """
        ...

    def option_quotes(self, occ_symbol: str, start: datetime, end: datetime,
                      interval: str = "1s") -> pd.DataFrame:
        """Serie de quotes del contrato: columnas ``bid`` y ``ask``, indice UTC.

        Permite reconstruir lo que ocurrio en una ventana corta (el gate
        ``confirm10`` mira los 10 segundos posteriores a la entrada), algo que
        un poll de 30 segundos no puede observar en vivo.
        """
        ...

    def option_quote(self, occ_symbol: str, at: datetime | None = None) -> Quote | None:
        """Quote del contrato. ``at=None`` significa el mas reciente.

        Devuelve None si no hay quote utilizable, nunca una quote inventada.
        """
        ...

    def latest_price(self, symbol: str) -> float:
        ...


# --- OCC ---------------------------------------------------------------

def occ_symbol(symbol: str, expiration: date, direction: str, strike: float) -> str:
    """Simbolo OCC de 21 caracteres: SPY   260102C00686000."""
    right = "C" if direction.upper() in {"CALL", "C"} else "P"
    strike_int = int(round(float(strike) * 1000))
    return f"{symbol.upper():<6}{expiration:%y%m%d}{right}{strike_int:08d}"


def parse_occ(occ: str) -> tuple[str, date, str, float]:
    """Inverso de :func:`occ_symbol`."""
    if len(occ) != 21:
        raise ValueError(f"OCC invalido (se esperaban 21 chars): {occ!r}")
    symbol = occ[:6].strip()
    expiration = datetime.strptime(occ[6:12], "%y%m%d").date()
    direction = "CALL" if occ[12].upper() == "C" else "PUT"
    strike = int(occ[13:]) / 1000
    return symbol, expiration, direction, strike


def candidate_expirations(day: date, max_dte: int = 2) -> list[date]:
    """Vencimientos habiles con DTE calendario 0..max_dte.

    Identico a ``paper.orb15_paper.candidate_expirations``: SPY tiene
    vencimiento diario, y el replay causal nunca miro mas alla de DTE 2.
    """
    out = []
    for k in range(max_dte + 1):
        candidate = day + timedelta(days=k)
        if candidate.weekday() < 5:
            out.append(candidate)
    return out
