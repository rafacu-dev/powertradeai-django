"""Contrato que cumple toda regla, y el catalogo donde se registran.

Principio no negociable: una regla solo puede mirar barras cuyo cierre ya es
observable en el instante de decidir. El proyecto ya produjo un veredicto falso
por entrar un minuto antes de tiempo; :meth:`ScanContext.causal_bars` existe
para que eso no se pueda repetir por descuido.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

NY = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Signal:
    """Lo que emite una regla cuando decide abrir."""

    direction: str                 # CALL | PUT
    signal_ts: datetime            # cierre de la vela que disparo
    underlying: float
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ExitDecision:
    """Lo que decide una regla sobre una alerta viva."""

    should_exit: bool
    reason: str = ""
    at: datetime | None = None


def target_stop_exit(bars, direction: str, target: float, stop: float,
                     not_before) -> ExitDecision:
    """Primera vela cerrada que toca target o stop en el SUBYACENTE.

    El P&L se liquida siempre sobre la prima de la opcion; el subyacente solo
    decide CUANDO se cierra. Si una misma vela toca ambos niveles se asume el
    stop: es el supuesto conservador, y el unico honesto sin datos de tick.
    """
    if bars is None or bars.empty:
        return ExitDecision(should_exit=False)
    cut = pd.Timestamp(not_before).tz_convert("UTC")
    for ts, bar in bars[bars.index >= cut].iterrows():
        high, low = float(bar["high"]), float(bar["low"])
        at = (ts + pd.Timedelta(minutes=1)).to_pydatetime()
        if direction == "CALL":
            if low <= stop:
                return ExitDecision(True, "stop", at)
            if high >= target:
                return ExitDecision(True, "target", at)
        else:
            if high >= stop:
                return ExitDecision(True, "stop", at)
            if low <= target:
                return ExitDecision(True, "target", at)
    return ExitDecision(should_exit=False)


@dataclass
class ScanContext:
    """Todo lo que una regla puede mirar, y nada mas."""

    provider: object
    symbol: str
    session_date: date
    now: datetime                  # tz-aware NY
    bars: pd.DataFrame             # velas 1m de la sesion, indice UTC
    _history_cache: dict = field(default_factory=dict, repr=False)

    def causal_bars(self, timeframe_minutes: int = 1) -> pd.DataFrame:
        """Barras cuyo cierre ya ocurrio en ``now``.

        Condicion: ``inicio_barra + timeframe <= now``. Una barra de 1m con
        inicio 09:49 solo es observable a partir de las 09:50:00.
        """
        if self.bars.empty:
            return self.bars
        cutoff = pd.Timestamp(self.now).tz_convert("UTC") - pd.Timedelta(
            minutes=timeframe_minutes)
        return self.bars[self.bars.index <= cutoff]

    def bars_between(self, start_et: str, end_et: str) -> pd.DataFrame:
        """Barras de la sesion entre dos horas ET, ambas inclusive por inicio."""
        if self.bars.empty:
            return self.bars
        local = self.bars.tz_convert(NY)
        mask = (local.index.time >= _parse_time(start_et)) & \
               (local.index.time <= _parse_time(end_et))
        return self.bars[mask]

    def et(self, ts) -> pd.Timestamp:
        return pd.Timestamp(ts).tz_convert(NY)

    def history(self, timeframe: str = "1m", days: int = 30) -> pd.DataFrame:
        """Historial que TERMINA el dia anterior a la sesion en curso.

        Deliberadamente excluye el dia de hoy: estos datos alimentan contexto
        (cierre previo, mediana de volumen, pivotes S/R) y colar la sesion viva
        ahi es la via mas facil de leer el futuro sin darse cuenta. Para el dia
        de hoy esta ``causal_bars``.
        """
        key = (self.symbol, timeframe, days)
        if key not in self._history_cache:
            end = self.session_date - timedelta(days=1)
            start = end - timedelta(days=days)
            self._history_cache[key] = self.provider.bars(
                self.symbol, start, end, timeframe)
        return self._history_cache[key]

    def resample(self, timeframe: str) -> pd.DataFrame:
        """Agrega las velas de HOY ya cerradas al timeframe pedido.

        Solo devuelve periodos completos: un 1h a las 10:30 no incluye la hora
        en curso, porque esa vela todavia no ha cerrado.
        """
        rule = {"15m": "15min", "1h": "1h"}.get(timeframe)
        if rule is None:
            raise ValueError(f"Timeframe no agregable: {timeframe!r}")
        minutes = {"15m": 15, "1h": 60}[timeframe]
        closed = self.causal_bars(1)
        if closed.empty:
            return closed
        agg = closed.resample(rule, label="left", closed="left").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum",
        }).dropna(subset=["close"])
        cutoff = pd.Timestamp(self.now).tz_convert("UTC") - pd.Timedelta(
            minutes=minutes)
        return agg[agg.index <= cutoff]


def _parse_time(text: str):
    return datetime.strptime(text, "%H:%M").time()


class BaseStrategy:
    """Regla evaluable.

    Las subclases declaran identidad y sobreescriben :meth:`evaluate`. El resto
    (seleccion de contrato, salida por tiempo) tiene un comportamiento por
    defecto que las familias pueden refinar.
    """

    strategy_id: str = ""
    name: str = ""
    symbol: str = ""
    rule_version: str = "v1"
    default_params: dict = {}

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params, **(params or {})}

    # --- Deteccion ------------------------------------------------------

    def evaluate(self, ctx: ScanContext) -> Signal | None:
        """Devuelve una senal si la regla dispara ahora, o None."""
        raise NotImplementedError

    # --- Salida ---------------------------------------------------------

    def scheduled_exit(self, entry_ts: datetime) -> datetime:
        """Cierre por tiempo, anclado a la entrada (nunca a la deteccion)."""
        hold = int(self.params.get("hold_minutes", 30))
        flatten = datetime.combine(
            entry_ts.astimezone(NY).date(), _parse_time(
                self.params.get("flatten_at", "15:55")), tzinfo=NY)
        return min(entry_ts.astimezone(NY) + timedelta(minutes=hold), flatten)

    def check_exit(self, ctx: ScanContext, alert) -> ExitDecision:
        """Salida anticipada. Por defecto no hay: manda el reloj."""
        return ExitDecision(should_exit=False)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.strategy_id}>"


# --- Catalogo -----------------------------------------------------------

_REGISTRY: dict[str, type[BaseStrategy]] = {}


def register(cls: type[BaseStrategy]) -> type[BaseStrategy]:
    """Decorador de auto-registro."""
    if not cls.strategy_id:
        raise ValueError(f"{cls.__name__} no declara strategy_id")
    if cls.strategy_id in _REGISTRY:
        raise ValueError(f"strategy_id duplicado: {cls.strategy_id}")
    _REGISTRY[cls.strategy_id] = cls
    return cls


def get_strategy_class(strategy_id: str) -> type[BaseStrategy]:
    try:
        return _REGISTRY[strategy_id]
    except KeyError:
        raise KeyError(
            f"Regla no registrada: {strategy_id!r}. "
            f"Registradas: {sorted(_REGISTRY)}") from None


def all_strategies() -> dict[str, type[BaseStrategy]]:
    return dict(_REGISTRY)
