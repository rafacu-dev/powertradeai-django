"""Reconstruccion de una sesion pasada.

Recorre el dia minuto a minuto como lo habria hecho el worker, deja que cada
regla decida con la informacion disponible en ese instante, y resuelve la salida
con quotes historicas reales del contrato.

Las alertas se guardan con ``source="replay"``. Esa marca no es cosmetica: una
reconstruccion no sufrio latencia de red, no compitio por el fill y toma la
quote del instante teorico, no la que se habria pagado. Su P&L es un limite
superior optimista, no un resultado.

Lo que este replay NO modela:
  * el spread que se habria cruzado de verdad al enviar la orden;
  * el rechazo del broker o la falta de liquidez en ese strike;
  * el retardo entre el cierre de vela y la observacion del productor.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

import pandas as pd

from ..data import get_provider
from ..models import Alert, Strategy
from ..strategies import ScanContext, get_strategy_class
from .session import NY, is_trading_day, session_close

log = logging.getLogger(__name__)

RTH_FIRST_DECISION = "09:31"   # antes no hay ninguna vela cerrada


@dataclass
class ReplayResult:
    day: date
    alerts: list[Alert] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def closed(self) -> list[Alert]:
        return [a for a in self.alerts if a.net_dollars is not None]

    @property
    def net_total(self) -> Decimal:
        return sum((a.net_dollars for a in self.closed), Decimal("0.00"))


class _SessionProvider:
    """Envuelve al proveedor real y sirve el dia entero desde memoria.

    Sin esto, una regla como la de agresividad pediria el tape por red en cada
    uno de los ~390 minutos del barrido. Aqui se descarga una vez y se recorta.
    """

    def __init__(self, provider, day: date):
        self._provider = provider
        self._day = day
        self.name = f"replay({provider.name})"
        self._bars: dict[str, pd.DataFrame] = {}
        self._tape: dict[str, pd.DataFrame] = {}

    def bars_1m(self, symbol: str, session_date: date) -> pd.DataFrame:
        if symbol not in self._bars:
            self._bars[symbol] = self._provider.bars_1m(symbol, session_date)
        return self._bars[symbol]

    def bars(self, symbol, start, end, timeframe="1m"):
        return self._provider.bars(symbol, start, end, timeframe)

    def latest_price(self, symbol: str) -> float:
        bars = self.bars_1m(symbol, self._day)
        if bars.empty:
            return self._provider.latest_price(symbol)
        return float(bars["close"].iloc[-1])

    def trades(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        if symbol not in self._tape:
            open_at = datetime.combine(self._day, datetime.min.time(),
                                       tzinfo=NY).replace(hour=9, minute=30)
            close_at = datetime.combine(
                self._day, session_close(self._day), tzinfo=NY)
            self._tape[symbol] = self._provider.trades(symbol, open_at, close_at)
        tape = self._tape[symbol]
        if tape.empty:
            return tape
        lo = pd.Timestamp(start).tz_convert("UTC")
        hi = pd.Timestamp(end).tz_convert("UTC")
        return tape[(tape.index >= lo) & (tape.index <= hi)]

    def option_quote(self, occ: str, at: datetime | None = None):
        # En replay nunca tiene sentido el snapshot en vivo: un contrato de una
        # sesion pasada ya vencio. Sin ``at`` explicito se usa el cierre.
        if at is None:
            at = datetime.combine(self._day, session_close(self._day), tzinfo=NY)
        return self._provider.option_quote(occ, at=at)

    def option_quotes(self, occ, start, end, interval="1s"):
        return self._provider.option_quotes(occ, start, end, interval)


def _minutes(day: date):
    """Instantes de decision de la sesion, de 09:31 al cierre."""
    hh, mm = (int(x) for x in RTH_FIRST_DECISION.split(":"))
    cursor = datetime.combine(day, datetime.min.time(), tzinfo=NY).replace(
        hour=hh, minute=mm)
    end = datetime.combine(day, session_close(day), tzinfo=NY)
    while cursor <= end:
        yield cursor
        cursor += timedelta(minutes=1)


def replay_day(day: date, provider=None, strategy_ids: list[str] | None = None,
               overwrite: bool = False) -> ReplayResult:
    """Reconstruye una sesion completa y persiste las alertas como ``replay``."""
    if not is_trading_day(day):
        raise ValueError(f"{day} no es un dia habil de mercado")

    provider = _SessionProvider(provider or get_provider(), day)
    result = ReplayResult(day=day)

    rows = Strategy.objects.filter(enabled=True)
    if strategy_ids:
        rows = rows.filter(strategy_id__in=strategy_ids)

    for row in rows:
        existing = Alert.objects.filter(
            strategy=row, session_date=day, source=Alert.Source.REPLAY)
        if existing.exists():
            if not overwrite:
                result.skipped.append(
                    (row.strategy_id, "ya reconstruida (usa --overwrite)"))
                continue
            existing.delete()

        try:
            alert = _replay_strategy(row, day, provider)
        except Exception as exc:
            log.exception("replay de %s fallo", row.strategy_id)
            result.errors.append((row.strategy_id, f"{type(exc).__name__}: {exc}"))
            continue

        if alert is None:
            result.skipped.append((row.strategy_id, "sin senal"))
        else:
            result.alerts.append(alert)

    return result


def detect_signal(strategy, day: date, bars, provider,
                  history_cache: dict | None = None):
    """Primera senal de la sesion, barriendo minuto a minuto.

    Solo toca el subyacente: no pide cadena de opciones ni resuelve salida. Es
    lo que permite comparar la DETECCION contra un artefacto de backtest sin
    gastar una peticion de quotes por sesion.

    Devuelve ``(signal, moment)`` o ``(None, None)``.
    """
    if bars is None or bars.empty:
        return None, None
    cache = history_cache if history_cache is not None else {}
    for moment in _minutes(day):
        ctx = ScanContext(
            provider=provider, symbol=strategy.symbol, session_date=day,
            now=moment, bars=bars, _history_cache=cache)
        signal = strategy.evaluate(ctx)
        if signal is not None:
            return signal, moment
    return None, None


def _replay_strategy(row: Strategy, day: date, provider) -> Alert | None:
    strategy = get_strategy_class(row.strategy_id)(row.params)
    bars = provider.bars_1m(row.symbol, day)
    if bars.empty:
        return None

    # El cache de historial se comparte entre todos los instantes del barrido:
    # el contexto de 30 dias no cambia dentro de una misma sesion.
    history_cache: dict = {}

    def context(moment: datetime) -> ScanContext:
        return ScanContext(
            provider=provider, symbol=row.symbol, session_date=day,
            now=moment, bars=bars, _history_cache=history_cache)

    # --- 1. Buscar la primera senal de la sesion ------------------------
    signal, signal_moment = detect_signal(
        strategy, day, bars, provider, history_cache)
    if signal is None:
        return None

    # --- 2. Contrato con la quote del instante de la senal --------------
    ctx = context(signal_moment)
    occ, expiration, strike, quote = strategy.select_contract(
        ctx, signal, at=signal.signal_ts)
    if occ is None:
        log.info("%s %s: senal sin contrato utilizable", row.strategy_id, day)
        return None

    entry_ts = signal.signal_ts
    alert = Alert.objects.create(
        strategy=row,
        rule_version=row.rule_version,
        symbol=row.symbol,
        session_date=day,
        direction=signal.direction,
        source=Alert.Source.REPLAY,
        status=Alert.Status.PENDING,
        signal_ts=entry_ts,
        underlying_at_signal=Decimal(str(signal.underlying)),
        occ_symbol=occ,
        expiration=expiration,
        strike=Decimal(str(strike)),
        contracts=row.contracts,
        commission=row.commission,
        entry_ts=entry_ts,
        entry_bid=Decimal(str(quote.bid)),
        entry_ask=Decimal(str(quote.ask)),
        entry_premium=Decimal(str(quote.ask)),
        scheduled_exit_ts=strategy.scheduled_exit(entry_ts),
        meta={**signal.meta, "replay": True},
    )

    # --- 3. Avanzar hasta la salida -------------------------------------
    exit_at, reason = _find_exit(strategy, context, alert, day)
    if exit_at is None:
        alert.status = Alert.Status.EXPIRED
        alert.exit_reason = "sin_salida_observable"
        alert.save(update_fields=["status", "exit_reason", "updated_at"])
        return alert

    exit_quote = provider.option_quote(occ, at=exit_at)
    if exit_quote is None or exit_quote.bid <= 0:
        alert.status = Alert.Status.EXPIRED
        alert.exit_reason = f"{reason}:sin_quote"
        alert.save(update_fields=["status", "exit_reason", "updated_at"])
        return alert

    alert.close(exit_premium=exit_quote.bid, exit_ts=exit_at, reason=reason)
    return alert


def _find_exit(strategy, context, alert: Alert, day: date):
    """Primer instante en que la regla o el reloj cierran la posicion."""
    scheduled = alert.scheduled_exit_ts
    close_at = datetime.combine(day, session_close(day), tzinfo=NY)

    for moment in _minutes(day):
        if moment <= alert.entry_ts:
            continue
        decision = strategy.check_exit(context(moment), alert)
        if decision.should_exit:
            return (decision.at or moment), decision.reason
        if scheduled is not None and moment >= scheduled:
            return scheduled, "time_exit"

    return close_at, "session_close"
