"""Familia TSLA prev-close: gap de apertura contra el cierre RTH anterior.

Portado de ``paper/engines/prevclose.py``. Cinco reglas que solo miran los
primeros minutos de la sesion:

  R3  gap DOWN 250-350 bps -> CALL en el open de 09:30, salida absoluta 09:45.
  R1  gap DOWN 300-400 bps -> CALL en el open de 09:31.
  R2  igual que R1 pero exige volumen del primer minuto >= 2.03x la mediana 20.
  FAILED10/25  gap UP 200-400 bps que se aleja >=10 o >=25 bps -> CALL.

Todas apuntan al 75% del gap y comparten un stop simetrico a esa distancia.

Aviso sobre solapamiento: R1 y R2 disparan sobre el MISMO trade, igual que
FAILED10 y FAILED25. El motor original lo documenta explicitamente
(``BACKTEST_TARGET75_N``) y sus n nunca deben sumarse entre reglas. En esta app
cada una genera su propia alerta, asi que un agregado que sume las cinco cuenta
el mismo movimiento dos veces.
"""
from __future__ import annotations

import math
from datetime import datetime, time

import pandas as pd

from ..data import candidate_expirations, occ_symbol
from .base import (
    NY,
    BaseStrategy,
    ExitDecision,
    ScanContext,
    Signal,
    register,
    target_stop_exit,
)

TARGET_GAP_FRAC = 0.75
MAX_PREMIUM_DOLLARS = 850.0    # prima < $850 por contrato
R3_MAX_SPREAD_PCT = 2.72       # solo R3


class PrevCloseBase(BaseStrategy):
    symbol = "TSLA"
    default_params = {
        "target_gap_frac": TARGET_GAP_FRAC,
        "scheduled_exit_et": "09:45",   # salida absoluta, no relativa
        "max_premium_dollars": MAX_PREMIUM_DOLLARS,
        "max_spread_pct": None,
        "strike_depth": 8,
        "max_dte": 2,
        # Tolerancia del runtime original (prevclose_runtime): 90s si la
        # entrada es en el open de 09:30, 60s si es en el de 09:31. Aqui la
        # edad se mide desde que la señal es OBSERVABLE para un scanner de
        # velas cerradas (el fin de la vela de entrada), no desde signal_ts:
        # este scanner no ve el open en tiempo real como el productor.
        "entry_quote_tolerance_seconds": 60,
    }

    direction = "CALL"
    entry_delay_min = 1     # 0 = open de 09:30; 1 = open de 09:31

    # --- Contexto -------------------------------------------------------

    def _opening_bars(self, ctx: ScanContext) -> pd.DataFrame:
        """Velas 09:30..09:44 ya cerradas, en hora ET."""
        closed = ctx.causal_bars(1)
        if closed.empty:
            return closed
        local = closed.tz_convert(NY)
        mask = (local.index.time >= time(9, 30)) & (local.index.time <= time(9, 44))
        return closed[mask]

    def _prev_rth_close(self, ctx: ScanContext) -> float | None:
        """Cierre RTH de la sesion anterior. Sin el, la regla no existe."""
        history = ctx.history("1m", days=10)
        if history.empty:
            return None
        local = history.tz_convert(NY)
        rth = history[
            (local.index.time >= time(9, 30)) & (local.index.time < time(16, 0))
        ]
        if rth.empty:
            return None
        last_day = rth.tz_convert(NY).index[-1].date()
        session = rth[rth.tz_convert(NY).index.date == last_day]
        return float(session["close"].iloc[-1]) if not session.empty else None

    def _first_minute_volume_median20(self, ctx: ScanContext) -> float | None:
        """Mediana del volumen del primer minuto en las 20 sesiones previas."""
        history = ctx.history("1m", days=40)
        if history.empty:
            return None
        local = history.tz_convert(NY)
        first_minutes = history[local.index.time == time(9, 30)]
        if len(first_minutes) < 5:
            return None
        return float(first_minutes["volume"].tail(20).median())

    # --- Geometria ------------------------------------------------------

    def _levels(self, entry: float, open_px: float, prev_close: float):
        """Target al 75% del gap; stop simetrico respecto a la entrada."""
        gap_abs = abs(open_px - prev_close)
        frac = float(self.params["target_gap_frac"])
        target = (open_px + gap_abs * frac if self.direction == "CALL"
                  else open_px - gap_abs * frac)
        distance = max(abs(target - entry), entry * 0.0005)
        stop = entry - distance if self.direction == "CALL" else entry + distance
        return round(target, 4), round(stop, 4)

    @staticmethod
    def _valid_geometry(direction: str, entry: float, target: float,
                        stop: float) -> bool:
        """Impide operar un target que ya quedo detras del precio de entrada."""
        values = [entry, target, stop]
        if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
            return False
        if direction == "CALL":
            return stop < entry < target
        return target < entry < stop

    # --- Deteccion ------------------------------------------------------

    def _gap_matches(self, gap_bps: float) -> bool:
        raise NotImplementedError

    def _extra_conditions(self, ctx, bars, open_px, prev_close, gap_bps) -> dict | None:
        """Filtros propios de cada regla. dict de contexto, o None para rechazar."""
        return {}

    def evaluate(self, ctx: ScanContext) -> Signal | None:
        bars = self._opening_bars(ctx)
        if bars.empty:
            return None
        prev_close = self._prev_rth_close(ctx)
        if not prev_close or prev_close <= 0:
            return None

        open_px = float(bars["open"].iloc[0])
        gap_bps = (open_px / prev_close - 1.0) * 10000.0
        if not self._gap_matches(gap_bps):
            return None

        if self.entry_delay_min == 0:
            entry_ts = ctx.et(bars.index[0]).to_pydatetime()
            entry = open_px
        else:
            if len(bars) < 2:
                return None   # la primera vela aun no ha cerrado
            entry_ts = ctx.et(bars.index[1]).to_pydatetime()
            entry = float(bars["open"].iloc[1])

        extra = self._extra_conditions(ctx, bars, open_px, prev_close, gap_bps)
        if extra is None:
            return None

        # Guarda anti-entrada-tardia. El pre-entry-hit del runtime original
        # (target/stop tocado antes de la quote) queda subsumido: con esta
        # tolerancia nunca hay una vela cerrada completa entre la señal y la
        # entrada, y los filtros del backtest (first_high < target, entry <
        # target) ya cubren la vela de apertura.
        observable_at = pd.Timestamp(entry_ts) + pd.Timedelta(minutes=1)
        age = (pd.Timestamp(ctx.now) - observable_at).total_seconds()
        if age > float(self.params["entry_quote_tolerance_seconds"]):
            return None

        target, stop = self._levels(entry, open_px, prev_close)
        entry = round(entry, 4)
        if not self._valid_geometry(self.direction, entry, target, stop):
            return None

        return Signal(
            direction=self.direction,
            signal_ts=entry_ts,
            underlying=entry,
            meta={
                "open_930": open_px,
                "prev_rth_close": prev_close,
                "gap_bps": round(gap_bps, 2),
                "abs_gap_bps": round(abs(gap_bps), 2),
                "target_underlying": target,
                "stop_underlying": stop,
                "target_gap_frac": float(self.params["target_gap_frac"]),
                "entry_delay_min": self.entry_delay_min,
                "scheduled_exit_et": self.params["scheduled_exit_et"],
                "time_exit_basis": "absolute_market_time",
                **extra,
            },
        )

    # --- Contrato -------------------------------------------------------

    def select_contract(self, ctx: ScanContext, signal: Signal, at=None):
        """Strike ITM mas cercano que ADEMAS pase el filtro de prima y spread.

        El gate no es cosmetico: en el replay causal, R3 tuvo 3 candidatos y
        ninguno paso prima/spread, asi que la regla no produjo ninguna
        operacion. Si aqui se ignorase el gate, la app inventaria trades que el
        backtest nunca tuvo.
        """
        spot = signal.underlying
        is_call = signal.direction == "CALL"
        base = math.floor(spot) if is_call else math.ceil(spot)
        depth = int(self.params["strike_depth"])
        strikes = [base - i if is_call else base + i for i in range(depth)]

        max_premium = float(self.params["max_premium_dollars"])
        max_spread = self.params["max_spread_pct"]

        for expiration in candidate_expirations(
            ctx.session_date, int(self.params["max_dte"])
        ):
            for strike in strikes:
                occ = occ_symbol(self.symbol, expiration, signal.direction,
                                 float(strike))
                try:
                    quote = ctx.provider.option_quote(occ, at=at)
                except Exception:
                    continue
                if quote is None or not quote.is_live or quote.ask < quote.bid:
                    continue
                if quote.ask * 100.0 >= max_premium:
                    continue
                if max_spread is not None:
                    spread_pct = (quote.ask - quote.bid) / quote.ask * 100.0
                    if spread_pct >= float(max_spread):
                        continue
                return occ, expiration, float(strike), quote
        return None, None, None, None

    # --- Salida ---------------------------------------------------------

    def scheduled_exit(self, entry_ts: datetime) -> datetime:
        """Hora ABSOLUTA de mercado, no un hold relativo a la entrada.

        El motor original lo marca explicitamente: un hold relativo de 14m
        cerraba R3 alrededor de las 09:44 en vez de las 09:45.
        """
        hhmm = self.params["scheduled_exit_et"]
        hh, mm = (int(x) for x in hhmm.split(":"))
        return datetime.combine(
            entry_ts.astimezone(NY).date(), time(hh, mm), tzinfo=NY)

    def check_exit(self, ctx: ScanContext, alert) -> ExitDecision:
        target = alert.meta.get("target_underlying")
        stop = alert.meta.get("stop_underlying")
        if target is None or stop is None or alert.entry_ts is None:
            return ExitDecision(should_exit=False)
        return target_stop_exit(
            ctx.causal_bars(1), alert.direction,
            float(target), float(stop), alert.entry_ts)


# --- Las cinco reglas ---------------------------------------------------

@register
class TslaPrevCloseD0Spread(PrevCloseBase):
    """R3: entra en el open de 09:30. Unica con filtro de spread."""

    strategy_id = "TSLA_PREVCLOSE_D0_G250350_P850_SPREAD"
    name = "TSLA prev-close 9:30 gap250-350 prima<850 spread<2.72"
    rule_version = "prevclose_target75_causal_quote_v4"
    entry_delay_min = 0
    default_params = {
        **PrevCloseBase.default_params,
        "max_spread_pct": R3_MAX_SPREAD_PCT,
        "entry_quote_tolerance_seconds": 90,   # entrada 09:30: el runtime da 90s
    }

    def _gap_matches(self, gap_bps: float) -> bool:
        return gap_bps < 0 and 250 <= abs(gap_bps) < 350


class _PrevCloseGapDown(PrevCloseBase):
    """R1/R2: gap DOWN 300-400 bps, entrada en el open de 09:31."""

    def _gap_matches(self, gap_bps: float) -> bool:
        return gap_bps < 0 and 300 <= abs(gap_bps) < 400

    def _extra_conditions(self, ctx, bars, open_px, prev_close, gap_bps):
        # Si el target ya se alcanzo dentro del primer minuto, entrar despues
        # seria comprar un movimiento que ya ocurrio.
        target_75 = open_px + abs(open_px - prev_close) * 0.75
        if float(bars["high"].iloc[0]) >= target_75:
            return None
        median20 = self._first_minute_volume_median20(ctx)
        ratio = (float(bars["volume"].iloc[0]) / median20
                 if median20 and median20 > 0 else None)
        return {"first1_vol_ratio20": ratio}


@register
class TslaPrevCloseD1(_PrevCloseGapDown):
    strategy_id = "TSLA_PREVCLOSE_D1_G300400_P850"
    name = "TSLA prev-close 9:31 gap300-400 prima<850"
    rule_version = "prevclose_target75_causal_quote_v4"


@register
class TslaPrevCloseVol1M(_PrevCloseGapDown):
    """R2: R1 + confirmacion de volumen. Comparte trade con R1."""

    strategy_id = "TSLA_PREVCLOSE_VOL1M"
    name = "TSLA prev-close volumen1m"
    rule_version = "prevclose_target75_causal_quote_v4"
    default_params = {**PrevCloseBase.default_params, "min_vol_ratio20": 2.03}

    def _extra_conditions(self, ctx, bars, open_px, prev_close, gap_bps):
        extra = super()._extra_conditions(ctx, bars, open_px, prev_close, gap_bps)
        if extra is None:
            return None
        ratio = extra.get("first1_vol_ratio20")
        if ratio is None or ratio < float(self.params["min_vol_ratio20"]):
            return None
        return extra


class _FailedFadeCall(PrevCloseBase):
    """Gap UP 200-400 bps que en el primer minuto se aleja aun mas."""

    away_threshold_bps = 10.0

    def _gap_matches(self, gap_bps: float) -> bool:
        return gap_bps > 0 and 200 <= abs(gap_bps) < 400

    def _extra_conditions(self, ctx, bars, open_px, prev_close, gap_bps):
        first_high = float(bars["high"].iloc[0])
        away_bps = max(0.0, first_high - open_px) / open_px * 10000.0
        if away_bps < float(self.params["away_threshold_bps"]):
            return None
        # El backtest clasifica estas filas como missed_target_before_entry:
        # comprar despues de tocar el target inflaria el live.
        target_75 = open_px + abs(open_px - prev_close) * TARGET_GAP_FRAC
        entry = float(bars["open"].iloc[1]) if len(bars) > 1 else open_px
        if not (first_high < target_75 and entry < target_75):
            return None
        return {"away_confirm_bps": round(away_bps, 2)}


@register
class TslaFailedFadeAway10(_FailedFadeCall):
    strategy_id = "TSLA_FAILED_FADE_CALL_AWAY10"
    name = "TSLA failed-fade CALL away>=10"
    rule_version = "failed_fade_d1_away10_target75_causal_v4"
    default_params = {**PrevCloseBase.default_params, "away_threshold_bps": 10.0}


@register
class TslaFailedFadeAway25(_FailedFadeCall):
    """Comparte trades con AWAY10: un away>=25 tambien cumple away>=10."""

    strategy_id = "TSLA_FAILED_FADE_CALL_AWAY25"
    name = "TSLA failed-fade CALL away>=25"
    rule_version = "failed_fade_d1_away25_target75_causal_v4"
    default_params = {**PrevCloseBase.default_params, "away_threshold_bps": 25.0}
