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
from django.db import transaction
from django.utils import timezone

from ..data import get_provider
from ..models import Alert, ReplayRun, Strategy
from ..strategies import ScanContext, get_strategy_class
from .session import NY, RTH_OPEN, is_trading_day, session_close

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


@dataclass
class ReplayTimeline:
    day: date
    symbol: str
    timeframe: str = "15m"
    replay_start_time: int | None = None
    candles: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    strategies: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


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


def replay_timeline(day: date, symbol: str, provider=None,
                    strategy_ids: list[str] | None = None) -> ReplayTimeline:
    """Datos para el reproductor visual. No escribe en base de datos."""
    if not is_trading_day(day):
        raise ValueError(f"{day} no es un dia habil de mercado")

    symbol = symbol.upper()
    provider = _SessionProvider(provider or get_provider(), day)
    bars = provider.bars_1m(symbol, day)
    timeline = ReplayTimeline(day=day, symbol=symbol)
    display_bars = _display_bars_15m(provider, symbol, day)
    timeline.candles = _candles_payload(display_bars)
    timeline.replay_start_time = _first_replay_candle_time(display_bars, day)
    if bars.empty:
        return timeline

    rows = Strategy.objects.filter(enabled=True, symbol=symbol)
    if strategy_ids:
        rows = rows.filter(strategy_id__in=strategy_ids)

    for row in rows:
        timeline.strategies.append(row.strategy_id)
        try:
            strategy = get_strategy_class(row.strategy_id)(row.params)
            history_cache: dict = {}
            fired = False
            last_note_minute = None
            for moment in _minutes(day):
                ctx = ScanContext(
                    provider=provider, symbol=row.symbol, session_date=day,
                    now=moment, bars=bars, _history_cache=history_cache)
                signal = strategy.evaluate(ctx)
                if signal is None:
                    note = _observation_event(row.strategy_id, ctx, last_note_minute)
                    if note is not None:
                        last_note_minute = moment.minute
                        timeline.events.append(note)
                    continue

                timeline.events.append(_signal_event(row.strategy_id, signal))
                fired = True
                break
            if not fired:
                timeline.events.append({
                    "time": int(datetime.combine(
                        day, session_close(day), tzinfo=NY).timestamp()),
                    "type": "no_signal",
                    "strategy_id": row.strategy_id,
                    "label": "Sin senal",
                    "detail": "La regla no disparo con los datos disponibles.",
                })
        except Exception as exc:
            log.exception("timeline de %s fallo", row.strategy_id)
            timeline.errors.append((row.strategy_id, f"{type(exc).__name__}: {exc}"))

    timeline.events.sort(key=lambda item: (item["time"], item["strategy_id"]))
    return timeline


def _candles_payload(bars: pd.DataFrame) -> list[dict]:
    rows = []
    for ts, row in bars.iterrows():
        rows.append({
            "time": int(ts.timestamp()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        })
    return rows


def _display_bars_15m(provider, symbol: str, day: date) -> pd.DataFrame:
    start = _previous_trading_days_start(day, count=5)
    bars = provider.bars(symbol, start, day, "1m")
    rth = _rth_only(bars)
    if rth.empty:
        return rth
    out = rth.resample(
        "15min", label="left", closed="left",
        origin="start_day", offset="30min",
    ).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["close"])
    return _rth_only(out)


def _previous_trading_days_start(day: date, count: int) -> date:
    cursor = day
    found = 0
    while found < count:
        cursor -= timedelta(days=1)
        if is_trading_day(cursor):
            found += 1
    return cursor


def _rth_only(bars: pd.DataFrame) -> pd.DataFrame:
    if bars is None or bars.empty:
        return bars
    local = bars.index.tz_convert(NY)
    mask = []
    for ts in local:
        close = session_close(ts.date())
        mask.append(RTH_OPEN <= ts.time() < close)
    return bars[mask]


def _first_replay_candle_time(bars: pd.DataFrame, day: date) -> int | None:
    if bars.empty:
        return None
    local = bars.index.tz_convert(NY)
    same_day = bars[local.date == day]
    if same_day.empty:
        return None
    return int(same_day.index[0].timestamp())


def _observation_event(strategy_id: str, ctx: ScanContext,
                       last_note_minute: int | None) -> dict | None:
    # Marcadores ligeros cada 15 minutos para que el replay muestre avance aun
    # cuando ninguna regla dispara. Las senales conservan todo el detalle.
    if ctx.now.minute % 15 != 0 or ctx.now.minute == last_note_minute:
        return None
    closed = ctx.causal_bars(1)
    if closed.empty:
        return None
    last = closed.iloc[-1]
    return {
        "time": int(ctx.now.timestamp()),
        "type": "observation",
        "strategy_id": strategy_id,
        "label": "Evaluacion",
        "price": round(float(last["close"]), 4),
        "detail": "Regla evaluada sin senal.",
    }


def _signal_event(strategy_id: str, signal) -> dict:
    levels = {}
    for key in (
        "range_high", "range_low", "bb_mid", "bb_upper", "bb_lower",
        "target_underlying", "stop_underlying", "open_930", "prev_rth_close",
    ):
        value = signal.meta.get(key)
        if value is not None:
            levels[key] = value
    return {
        "time": int(pd.Timestamp(signal.signal_ts).timestamp()),
        "type": "signal",
        "strategy_id": strategy_id,
        "direction": signal.direction,
        "label": f"Senal {signal.direction}",
        "price": round(float(signal.underlying), 4),
        "detail": _signal_detail(signal.meta),
        "meta": signal.meta,
        "levels": levels,
    }


def _signal_detail(meta: dict) -> str:
    parts = []
    if meta.get("rama"):
        parts.append(f"rama {meta['rama']}")
    if meta.get("gap_bps") is not None:
        parts.append(f"gap {meta['gap_bps']} bps")
    if meta.get("volume_ratio") is not None:
        parts.append(f"vol x{meta['volume_ratio']}")
    if meta.get("range_high") is not None and meta.get("range_low") is not None:
        parts.append(f"rango {meta['range_low']} - {meta['range_high']}")
    if meta.get("target_underlying") is not None:
        parts.append(f"target {meta['target_underlying']}")
    if meta.get("stop_underlying") is not None:
        parts.append(f"stop {meta['stop_underlying']}")
    return " · ".join(parts) or "Condiciones matematicas cumplidas."


def replay_day(day: date, provider=None, strategy_ids: list[str] | None = None,
               overwrite: bool = False, persist: bool = True) -> ReplayResult:
    """Reconstruye una sesion; ``persist=False`` no realiza escrituras."""
    if not is_trading_day(day):
        raise ValueError(f"{day} no es un dia habil de mercado")

    provider = _SessionProvider(provider or get_provider(), day)
    result = ReplayResult(day=day)
    replay_run = None
    if persist:
        replay_run = ReplayRun.objects.create(
            session_date=day,
            strategy_ids=list(strategy_ids or []),
            overwrite=overwrite,
        )

    rows = Strategy.objects.filter(enabled=True)
    if strategy_ids:
        rows = rows.filter(strategy_id__in=strategy_ids)

    planned: list[tuple[Strategy, Alert | None]] = []
    for row in list(rows):
        existing = Alert.objects.filter(
            strategy=row, session_date=day, source=Alert.Source.REPLAY)
        if persist and existing.exists() and not overwrite:
            result.skipped.append(
                (row.strategy_id, "ya reconstruida (usa --overwrite)"))
            continue

        try:
            # Todo el trabajo de red y calculo ocurre sin modificar la base.
            alert = _replay_strategy(row, day, provider)
        except Exception as exc:
            log.exception("replay de %s fallo", row.strategy_id)
            result.errors.append((row.strategy_id, f"{type(exc).__name__}: {exc}"))
            continue

        planned.append((row, alert))
        if alert is None:
            result.skipped.append((row.strategy_id, "sin senal"))

    if not persist:
        result.alerts = [alert for _, alert in planned if alert is not None]
    elif result.errors:
        # Atomicidad de la sesion: si una regla falla, ninguna reconstruccion
        # nueva se guarda y cualquier version anterior permanece intacta.
        result.skipped.extend(
            (row.strategy_id, "no persistida: fallo atomico de la sesion")
            for row, alert in planned if alert is not None
        )
    else:
        persisted: list[Alert] = []
        try:
            with transaction.atomic():
                for row, alert in planned:
                    current = Alert.objects.select_for_update().filter(
                        strategy=row,
                        session_date=day,
                        source=Alert.Source.REPLAY,
                    )
                    if current.exists() and not overwrite:
                        result.skipped.append(
                            (row.strategy_id,
                             "ya reconstruida por otra corrida"))
                        continue
                    if overwrite:
                        current.delete()
                    if alert is not None:
                        alert.save(force_insert=True)
                        persisted.append(alert)
        except Exception as exc:
            log.exception("fallo el commit atomico del replay %s", day)
            result.errors.append((
                "__commit__", f"{type(exc).__name__}: {exc}"))
        else:
            result.alerts = persisted

    if replay_run is not None:
        replay_run.alerts_created = len(result.alerts)
        replay_run.errors = [
            {"strategy_id": strategy_id, "detail": detail}
            for strategy_id, detail in result.errors
        ]
        replay_run.status = (
            ReplayRun.Status.ERROR if result.errors else ReplayRun.Status.DONE)
        replay_run.finished_at = timezone.now()
        replay_run.save(update_fields=[
            "alerts_created", "errors", "status", "finished_at"])
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

    # ThetaData sirve la primera NBBO posterior a la decision. Ese timestamp es
    # la ejecucion simulada; anclar la entrada al cierre de señal adelantaria
    # tanto el fill como el reloj de salida.
    entry_ts = quote.ts or signal.signal_ts
    # Objeto en memoria. Persistir antes de resolver dejaba filas ``pending`` si
    # una quote posterior fallaba y hacia que ``save=false`` escribiera de todos
    # modos.
    alert = Alert(
        strategy=row,
        rule_version=row.rule_version,
        symbol=row.symbol,
        session_date=day,
        direction=signal.direction,
        evaluation_version=(
            "investep_v2" if signal.meta.get("rama") else "legacy_v1"),
        source=Alert.Source.REPLAY,
        status=Alert.Status.PENDING,
        signal_ts=signal.signal_ts,
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
        academy_strategy=(
            row.strategy_id.split("_")[1]
            if any(marker in row.strategy_id for marker in ("_E01_", "_E02_"))
            else ""),
        strategy_branch=str(signal.meta.get("rama", "")),
    )

    # --- 3. Avanzar hasta la salida -------------------------------------
    exit_at, reason = _find_exit(strategy, context, alert, day)
    if exit_at is None:
        alert.status = Alert.Status.EXPIRED
        alert.exit_reason = "sin_salida_observable"
        return alert

    exit_quote = provider.option_quote(occ, at=exit_at)
    if exit_quote is None or exit_quote.bid <= 0:
        alert.status = Alert.Status.EXPIRED
        alert.exit_reason = f"{reason}:sin_quote"
        return alert
    if alert.academy_strategy:
        from django.conf import settings

        from ..strategies.gates import validate_quote

        config = getattr(settings, "POWERTRADEAI", {})
        checked = validate_quote(
            exit_quote,
            as_of=exit_at,
            max_spread_pct=float(config.get("MAX_OPTION_SPREAD_PCT", 5.0)),
            allow_after_seconds=float(config.get(
                "MAX_HISTORICAL_OPTION_QUOTE_DELAY_SECONDS", 90)),
        )
        if checked["status"] != "VALID":
            alert.status = Alert.Status.EXPIRED
            alert.exit_reason = f"{reason}:quote_invalida"
            alert.meta["exit_quote_validation"] = checked
            return alert

    alert.exit_premium = Decimal(str(exit_quote.bid))
    alert.exit_ts = exit_at
    alert.exit_reason = reason
    alert.status = Alert.Status.CLOSED
    pnl = alert.compute_pnl()
    if pnl is not None:
        alert.net_dollars, alert.net_pct = pnl
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
