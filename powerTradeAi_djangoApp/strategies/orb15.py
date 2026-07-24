"""Familia SPY ORB-15.

Regla base: el rango son las 15 velas de 1m de 09:30 a 09:44 ET. Se vigila el
quiebre a partir de una hora de arranque; el cierre de una vela por encima de
``high*(1+BUF)`` da CALL y por debajo de ``low*(1-BUF)`` da PUT. Se compra el
strike ITM mas cercano con quote viva (DTE 0-2) y se mantiene 30 minutos.

Variantes:
  * ``_0950``          arranca a vigilar a las 09:50 en lugar de 09:45.
  * ``_RANGE_INVALID`` cierra antes si el precio vuelve dentro del rango.

Constantes copiadas de ``paper/orb15_paper.py`` (BUF, HOLD_MIN, profundidad de
strikes). Si divergen, el golden test deja de reproducir el replay causal.
"""
from __future__ import annotations

import math
from datetime import datetime

import pandas as pd

from ..data import candidate_expirations, occ_symbol
from .base import NY, BaseStrategy, ExitDecision, ScanContext, Signal, register

BUF = 0.0002          # buffer del quiebre (regla validada)
HOLD_MIN = 30         # salida por tiempo (regla validada)
RANGE_BARS = 15       # 09:30..09:44, sin rellenar minutos ausentes
STRIKE_DEPTH = 8      # niveles ITM que busca el replay causal
STOP_PCT = 15.0       # % de caida del BID vs ASK de entrada que dispara el stop
STOP_LEAD_SECONDS = 1.0   # el tick de entrada no cuenta como stop (spread inicial)


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class Orb15Base(BaseStrategy):
    symbol = "SPY"
    default_params = {
        "buffer": BUF,
        "hold_minutes": HOLD_MIN,
        "watch_from": "09:45",
        "watch_until": "11:00",
        "flatten_at": "15:55",
        "range_invalidation": False,
        "strike_depth": STRIKE_DEPTH,
        "max_dte": 2,
        # El productor original archiva como ``filtered_stale_signal`` toda
        # señal observada con mas de 90s y no opera el resto del dia. Sin esta
        # guarda, un scanner reiniciado a media sesion compraria un quiebre de
        # hace 40 minutos al precio actual.
        "max_signal_age_seconds": 90,
    }

    # --- Rango ----------------------------------------------------------

    def opening_range(self, ctx: ScanContext) -> tuple[float, float] | None:
        """(low, high) de las 15 velas 09:30..09:44, o None si estan incompletas.

        No se rellena un minuto ausente ni se acepta un rango parcial: es
        exactamente la condicion de ``paper.engines.orb_runtime``.
        """
        start = pd.Timestamp(
            datetime.combine(ctx.session_date, datetime.min.time()).replace(
                hour=9, minute=30, tzinfo=NY))
        expected = pd.date_range(start, periods=RANGE_BARS, freq="1min").tz_convert("UTC")

        bars = ctx.bars
        if bars.empty or bars.index.has_duplicates:
            return None
        if not expected.isin(bars.index).all():
            return None
        window = bars.loc[expected]
        if window[["high", "low"]].isna().any(axis=None):
            return None
        return float(window["low"].min()), float(window["high"].max())

    # --- Deteccion ------------------------------------------------------

    def evaluate(self, ctx: ScanContext) -> Signal | None:
        rango = self.opening_range(ctx)
        if rango is None:
            return None
        low, high = rango

        buf = float(self.params["buffer"])
        watch_from = self.params["watch_from"]
        watch_until = self.params["watch_until"]

        # Solo velas cerradas: nada de mirar la vela en curso.
        closed = ctx.causal_bars(1)
        if closed.empty:
            return None
        local = closed.tz_convert(NY)
        window = local[
            (local.index.time >= _t(watch_from)) & (local.index.time <= _t(watch_until))
        ]

        for ts, bar in window.iterrows():
            close = float(bar["close"])
            if close > high * (1 + buf):
                direction = "CALL"
            elif close < low * (1 - buf):
                direction = "PUT"
            else:
                continue
            # El cierre de la vela que inicia en ts ocurre un minuto despues.
            signal_ts = ts + pd.Timedelta(minutes=1)
            age = (pd.Timestamp(ctx.now) - signal_ts).total_seconds()
            if age > float(self.params["max_signal_age_seconds"]):
                # La regla es "primer quiebre del dia", no "primer quiebre que
                # el scanner vio": si el primero llego tarde, el dia se pierde,
                # igual que hace el productor original.
                return None
            return Signal(
                direction=direction,
                signal_ts=signal_ts.to_pydatetime(),
                underlying=close,
                meta={
                    "range_high": high,
                    "range_low": low,
                    "range_bar_count": RANGE_BARS,
                    "signal_bar_ts": ts.isoformat(),
                    "buffer": buf,
                    "watch_from": watch_from,
                },
            )
        return None

    # --- Contrato -------------------------------------------------------

    def select_contract(self, ctx: ScanContext, signal: Signal, at=None):
        """Strike ITM mas cercano con quote viva, DTE 0-2.

        Devuelve (occ, expiration, strike, quote) o (None, None, None, None).
        Recorre en el mismo orden que el replay: primero vencimiento, luego
        profundidad de strike; asi live no declara "sin contrato" donde el
        backtest si encontro uno.
        """
        spot = signal.underlying
        is_call = signal.direction == "CALL"
        # floor/ceil, no ``int(spot)+1``: cuando el spot cotiza en un entero
        # exacto (2026-01-20, SPY 681.00) el replay causal eligio 681 y la
        # forma ``int(spot)+1`` habria elegido 682. Con floor/ceil el artefacto
        # de 128 sesiones reproduce 128/128.
        base = math.floor(spot) if is_call else math.ceil(spot)
        depth = int(self.params["strike_depth"])
        strikes = [base - i if is_call else base + i for i in range(depth)]

        for expiration in candidate_expirations(
            ctx.session_date, int(self.params["max_dte"])
        ):
            for strike in strikes:
                occ = occ_symbol(self.symbol, expiration, signal.direction, float(strike))
                try:
                    quote = ctx.provider.option_quote(occ, at=at)
                except Exception:
                    continue
                if quote is not None and quote.is_live:
                    return occ, expiration, float(strike), quote
        return None, None, None, None

    # --- Salida ---------------------------------------------------------

    def check_exit(self, ctx: ScanContext, alert) -> ExitDecision:
        """Invalidacion por regreso al rango, solo en las variantes que la usan."""
        if not self.params.get("range_invalidation"):
            return ExitDecision(should_exit=False)

        low = alert.meta.get("range_low")
        high = alert.meta.get("range_high")
        if low is None or high is None or alert.entry_ts is None:
            return ExitDecision(should_exit=False)

        # Solo velas cerradas DESPUES de la entrada: una vela que ya estaba en
        # curso al entrar no puede invalidar retroactivamente.
        closed = ctx.causal_bars(1)
        entry_cut = pd.Timestamp(alert.entry_ts).tz_convert("UTC")
        after = closed[closed.index >= entry_cut]
        for ts, bar in after.iterrows():
            if low <= float(bar["close"]) <= high:
                return ExitDecision(
                    should_exit=True,
                    reason="range_invalidation",
                    at=(ts + pd.Timedelta(minutes=1)).to_pydatetime(),
                )
        return ExitDecision(should_exit=False)


def _t(text: str):
    return datetime.strptime(text, "%H:%M").time()


# --- Las cuatro variantes causalmente verificadas -----------------------

@register
class SpyOrb15Base(Orb15Base):
    strategy_id = "SPY_ORB15_BASE"
    name = "SPY ORB-15 apertura limpia"
    rule_version = "orb15_base_causal_v3"


@register
class SpyOrb15RangeInvalid(Orb15Base):
    strategy_id = "SPY_ORB15_RANGE_INVALID"
    name = "SPY ORB-15 invalidacion por regreso al rango"
    rule_version = "orb15_range_invalid_causal_v3"
    default_params = {**Orb15Base.default_params, "range_invalidation": True}


@register
class SpyOrb150950(Orb15Base):
    strategy_id = "SPY_ORB15_0950"
    name = "SPY ORB-15 desde 9:50"
    rule_version = "orb15_0950_causal_v3"
    default_params = {**Orb15Base.default_params, "watch_from": "09:50"}


@register
class SpyOrb150950RangeInvalid(Orb15Base):
    strategy_id = "SPY_ORB15_0950_RANGE_INVALID"
    name = "SPY ORB-15 9:50 + invalidacion rango"
    rule_version = "orb15_0950_range_invalid_causal_v3"
    default_params = {
        **Orb15Base.default_params,
        "watch_from": "09:50",
        "range_invalidation": True,
    }


@register
class SpyOrb150950RangeInvalidStop15(SpyOrb150950RangeInvalid):
    """Igual que la base 9:50 + invalidacion, con un stop causal sobre la PRIMA.

    Es GESTION DE RIESGO sobre la regla existente, no una regla nueva: misma
    senal de entrada, mismo contrato, misma invalidacion por regreso al rango y
    misma salida por tiempo. La unica diferencia es un stop adicional: cierra si
    el BID de la opcion cae ``option_stop_pct``% por debajo del ASK de entrada.

    De los tres eventos posibles (stop, invalidacion, tiempo) gana el que ocurra
    ANTES en el tiempo. El stop es causal: solo mira quotes hasta ``ctx.now`` y,
    si no hay serie de quotes, NO cierra (deja mandar al reloj) en lugar de
    inventarse el motivo del cierre.
    """

    strategy_id = "SPY_ORB15_0950_RANGE_INVALID_STOP15"
    name = "SPY ORB-15 9:50 + invalidacion + stop 15% prima"
    rule_version = "orb15_0950_range_invalid_stop15_causal_v1"
    default_params = {
        **SpyOrb150950RangeInvalid.default_params,
        "option_stop_pct": STOP_PCT,
        "option_stop_lead_seconds": STOP_LEAD_SECONDS,
    }

    def check_exit(self, ctx: ScanContext, alert) -> ExitDecision:
        # La base decide invalidacion/rango; aqui se suma el stop de prima y
        # gana el evento causalmente MAS TEMPRANO. En empate exacto gana el stop
        # (supuesto conservador, igual que el replay causal del proyecto).
        fired = [
            d for d in (super().check_exit(ctx, alert),
                        self._option_stop_exit(ctx, alert))
            if d.should_exit
        ]
        if not fired:
            return ExitDecision(should_exit=False)
        return min(fired, key=lambda d: (
            _utc(d.at) if d.at is not None else _utc(ctx.now),
            0 if d.reason == "option_stop" else 1,
        ))

    def _option_stop_exit(self, ctx: ScanContext, alert) -> ExitDecision:
        """Primer BID <= ask_entrada*(1 - stop%) observado tras la entrada.

        Causal: solo mira quotes hasta ``ctx.now``. Excluye el arranque
        (``option_stop_lead_seconds``) para que el spread del propio tick de
        entrada no dispare un stop instantaneo.
        """
        if alert.entry_ts is None or alert.entry_ask is None or not alert.occ_symbol:
            return ExitDecision(should_exit=False)

        stop_pct = float(self.params["option_stop_pct"])
        threshold = float(alert.entry_ask) * (1.0 - stop_pct / 100.0)

        entry_ts = _utc(alert.entry_ts)
        start = entry_ts + pd.Timedelta(
            seconds=float(self.params["option_stop_lead_seconds"]))
        now = _utc(ctx.now)
        if now < start:
            return ExitDecision(should_exit=False)

        try:
            quotes = ctx.provider.option_quotes(
                alert.occ_symbol, entry_ts.to_pydatetime(), now.to_pydatetime())
        except Exception:
            # Sin serie de quotes el stop queda INDETERMINADO: no se cierra.
            return ExitDecision(should_exit=False)
        if quotes is None or quotes.empty:
            return ExitDecision(should_exit=False)

        window = quotes[quotes.index >= start]
        if window.empty:
            return ExitDecision(should_exit=False)
        bids = pd.to_numeric(window["bid"], errors="coerce")
        # Un bid<=0 no es "cayo 15%": es ausencia de mercado. Se excluye para no
        # fabricar un stop con una quote muerta.
        breach = window[(bids > 0) & (bids <= threshold)]
        if breach.empty:
            return ExitDecision(should_exit=False)
        return ExitDecision(
            should_exit=True, reason="option_stop",
            at=_utc(breach.index[0]).to_pydatetime())
