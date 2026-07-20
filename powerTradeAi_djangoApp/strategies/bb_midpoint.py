"""Familia BB midpoint: rebote/rechazo en la media Bollinger de 1 hora.

Portado de ``paper/engines/bb_midpoint.py`` y de los helpers de
``research/hypotheses/hourly_bb_mid_bounce.py``.

La regla, en orden:

  1. Tendencia: en las 6 horas previas el precio estuvo del mismo lado de la
     media BB 1H al menos el 67% del tiempo, y llego a >=0.45 de la banda.
  2. Toque: la vela de 15m toca la media (con tolerancia de 12 bps) sin
     romperla al cierre por mas de 10 bps.
  3. Confirmacion: cierre al lado correcto de la media, mas cuerpo (>=45% del
     rango) y/o volumen (>=1.3x la mediana de 20 velas), segun la variante.
  4. Espacio: el siguiente nivel S/R de 15m esta a >=35 bps.

Target: la banda opuesta. Stop: la media +/-40 bps. Hold maximo: 180 minutos.

Causalidad: la senal se fecha en el CIERRE de la vela de 15m, y el contexto
horario se lee con ``row_before`` — la vela horaria en curso no es observable.
"""
from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pandas as pd

from ..data import candidate_expirations, occ_symbol
from .base import (
    BaseStrategy,
    ExitDecision,
    ScanContext,
    Signal,
    register,
    target_stop_exit,
)
from .indicators import atr, bollinger_bands, horizontal_levels

PARAMS = SimpleNamespace(
    side_lookback_hours=6, min_side_frac=0.67, min_band_pos=0.45,
    touch_tol_bps=12.0, close_break_bps=10.0, min_body_frac=0.45,
    min_vol_ratio=1.3, volume_lookback_bars=20,
    sr15_lookback_days=30, sr15_min_bars=80, sr15_min_target_bps=35.0,
    sr15_pivot_len=5, sr15_max_pivots=40, sr15_min_touches=2,
    sr15_tol_atr=0.35, sr15_max_levels=10,
)

MAX_HOLD_MINUTES = 180
STOP_FRAC = 0.0040     # media +/- 40 bps


# --- Helpers ------------------------------------------------------------

def add_hourly_context(h1: pd.DataFrame) -> pd.DataFrame:
    out = h1.copy()
    up, mid, low = bollinger_bands(out["close"], 20, 2.0)
    out["bb_up"], out["bb_mid"], out["bb_low"] = up, mid, low
    denom_up = (up - mid).replace(0, np.nan)
    denom_dn = (mid - low).replace(0, np.nan)
    out["pos_up"] = (out["close"] - mid) / denom_up
    out["pos_dn"] = (mid - out["close"]) / denom_dn
    out["above_mid"] = out["close"] > mid
    return out.dropna(subset=["bb_mid", "bb_up", "bb_low"])


def row_before(frame: pd.DataFrame, ts) -> pd.Series | None:
    """Ultima fila ESTRICTAMENTE anterior a ``ts``.

    Estricta a proposito: la vela horaria etiquetada con la hora de entrada
    todavia no ha cerrado cuando se decide.
    """
    idx = frame.index.searchsorted(pd.Timestamp(ts), side="left") - 1
    return None if idx < 0 else frame.iloc[idx]


def recent_side(hctx: pd.DataFrame, ts, lookback: int, min_side_frac: float,
                min_band_pos: float) -> tuple[str | None, float]:
    """Lado dominante de las ultimas ``lookback`` horas, y su fuerza."""
    idx = hctx.index.searchsorted(pd.Timestamp(ts), side="left") - 1
    if idx < lookback:
        return None, float("nan")
    hist = hctx.iloc[idx - lookback + 1:idx + 1]
    above = float(hist["above_mid"].mean())
    up_strength = float(hist["pos_up"].clip(lower=0).max())
    dn_strength = float(hist["pos_dn"].clip(lower=0).max())
    if above >= min_side_frac and up_strength >= min_band_pos:
        return "CALL", up_strength
    if (1.0 - above) >= min_side_frac and dn_strength >= min_band_pos:
        return "PUT", dn_strength
    return None, max(up_strength, dn_strength)


def body_frac(row) -> float:
    rng = float(row["high"] - row["low"])
    return abs(float(row["close"] - row["open"])) / rng if rng > 0 else 0.0


def vol_ratio(m15: pd.DataFrame, ts, lookback: int) -> float:
    """Volumen de la vela contra la mediana de las ``lookback`` anteriores."""
    try:
        idx = m15.index.get_loc(pd.Timestamp(ts))
    except KeyError:
        return float("nan")
    if not isinstance(idx, (int, np.integer)) or idx < lookback:
        return float("nan")
    base = float(m15["volume"].iloc[idx - lookback:idx].median())
    return float(m15["volume"].iloc[idx] / base) if base > 0 else float("nan")


def touch_direction(row, mid: float, direction: str, touch_tol_bps: float,
                    close_break_bps: float) -> bool:
    tol = mid * touch_tol_bps / 10000.0
    brk = mid * close_break_bps / 10000.0
    if direction == "CALL":
        return float(row["low"]) <= mid + tol and float(row["close"]) >= mid - brk
    return float(row["high"]) >= mid - tol and float(row["close"]) <= mid + brk


def confirm_on_bar(row, mid: float, direction: str, confirmation: str,
                   vr: float, min_vol_ratio: float, min_body_frac: float) -> bool:
    close_ok = (float(row["close"]) > mid if direction == "CALL"
                else float(row["close"]) < mid)
    directional_body = (float(row["close"]) > float(row["open"])
                        if direction == "CALL"
                        else float(row["close"]) < float(row["open"]))
    body_ok = directional_body and body_frac(row) >= min_body_frac
    vol_ok = np.isfinite(vr) and vr >= min_vol_ratio
    return {
        "close": close_ok,
        "body": close_ok and body_ok,
        "close_volume": close_ok and vol_ok,
        "body_volume": close_ok and body_ok and vol_ok,
    }.get(confirmation, False)


def sr15_context(m15: pd.DataFrame, entry_ts, entry: float,
                 direction: str) -> dict:
    """Siguiente nivel S/R de 15m y si deja espacio suficiente al target."""
    entry_ts = pd.Timestamp(entry_ts)
    start = entry_ts - pd.Timedelta(days=PARAMS.sr15_lookback_days)
    hist = m15[(m15.index < entry_ts) & (m15.index >= start)]
    empty = {
        "sr15_atr": None, "sr15_levels_count": 0, "sr15_next_level": None,
        "sr15_next_kind": "sin_nivel", "sr15_target_dist_bps": None,
        "sr15_has_room": False,
    }
    if len(hist) < PARAMS.sr15_min_bars:
        return empty

    atr_series = atr(hist["high"], hist["low"], hist["close"], 14).dropna()
    atr_value = float(atr_series.iloc[-1]) if not atr_series.empty else float("nan")
    levels = sorted(horizontal_levels(
        hist["high"].values, hist["low"].values, atr_value,
        n=PARAMS.sr15_pivot_len, maxpiv=PARAMS.sr15_max_pivots,
        mintouch=PARAMS.sr15_min_touches, tol_atr=PARAMS.sr15_tol_atr,
        maxlevels=PARAMS.sr15_max_levels,
    ))
    rounded_atr = round(atr_value, 4) if np.isfinite(atr_value) else None
    if not levels:
        return {**empty, "sr15_atr": rounded_atr}

    if direction == "CALL":
        candidates = [x for x in levels if x > entry]
        next_level = min(candidates) if candidates else float("nan")
        kind = "resistencia_15m" if candidates else "sin_resistencia"
    else:
        candidates = [x for x in levels if x < entry]
        next_level = max(candidates) if candidates else float("nan")
        kind = "soporte_15m" if candidates else "sin_soporte"

    if np.isfinite(next_level):
        dist_bps = abs(next_level / entry - 1.0) * 10000.0
        has_room = dist_bps >= PARAMS.sr15_min_target_bps
    else:
        dist_bps, has_room = float("nan"), False

    return {
        "sr15_atr": rounded_atr,
        "sr15_levels_count": len(levels),
        "sr15_next_level": round(float(next_level), 4) if np.isfinite(next_level) else None,
        "sr15_next_kind": kind,
        "sr15_target_dist_bps": round(float(dist_bps), 2) if np.isfinite(dist_bps) else None,
        "sr15_has_room": bool(has_room),
    }


def valid_target_geometry(direction: str, entry: float, target: float,
                          stop: float) -> bool:
    """Una confirmacion puede cerrar mas alla de la banda objetivo. Entrar
    despues y llamar 'target' a un nivel que quedo detras invierte la economia
    de la regla."""
    values = [entry, target, stop]
    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in values):
        return False
    if direction == "CALL":
        return stop < entry < target
    return target < entry < stop


# --- Regla --------------------------------------------------------------

class BBMidpointBase(BaseStrategy):
    direction = "CALL"
    confirmation = "body"
    rule_version = "bb1h_closed_signal_first_quote_v4"
    default_params = {
        "hold_minutes": MAX_HOLD_MINUTES,
        "flatten_at": "15:55",
        "cooldown_minutes": 60,
        "strike_depth": 8,
        "max_dte": 7,
    }

    def _frames(self, ctx: ScanContext) -> tuple[pd.DataFrame, pd.DataFrame]:
        """(1h con contexto, 15m) uniendo historial y sesion en curso.

        El historial de 30 dias alimenta las bandas y los pivotes S/R; la
        sesion de hoy solo aporta velas ya cerradas.
        """
        hist_1h = ctx.history("1h", days=PARAMS.sr15_lookback_days + 10)
        hist_15m = ctx.history("15m", days=PARAMS.sr15_lookback_days + 5)
        today_1h = ctx.resample("1h")
        today_15m = ctx.resample("15m")

        h1 = pd.concat([hist_1h, today_1h]).sort_index()
        m15 = pd.concat([hist_15m, today_15m]).sort_index()
        h1 = h1[~h1.index.duplicated(keep="last")]
        m15 = m15[~m15.index.duplicated(keep="last")]
        return h1, m15

    def evaluate(self, ctx: ScanContext) -> Signal | None:
        h1, m15 = self._frames(ctx)
        if len(h1) < 25 or len(m15) < 21:
            return None

        hctx = add_hourly_context(h1)
        bar_ts = pd.Timestamp(m15.index[-1])
        row = m15.iloc[-1]
        # Los indices marcan el INICIO de la vela: la confirmacion completa solo
        # existe quince minutos despues.
        entry_ts = bar_ts + pd.Timedelta(minutes=15)

        hrow = row_before(hctx, entry_ts)
        if hrow is None:
            return None

        trend, strength = recent_side(
            hctx, entry_ts, PARAMS.side_lookback_hours,
            PARAMS.min_side_frac, PARAMS.min_band_pos)
        if trend != self.direction:
            return None

        mid, up, low = float(hrow.bb_mid), float(hrow.bb_up), float(hrow.bb_low)
        if not touch_direction(row, mid, self.direction,
                               PARAMS.touch_tol_bps, PARAMS.close_break_bps):
            return None

        vr = vol_ratio(m15, bar_ts, PARAMS.volume_lookback_bars)
        if not confirm_on_bar(row, mid, self.direction, self.confirmation, vr,
                              PARAMS.min_vol_ratio, PARAMS.min_body_frac):
            return None

        entry = round(float(row.close), 4)
        # S/R se construye con velas ANTERIORES a la confirmacion: aunque esa
        # vela ya cerro, incluirla cambiaria la regla backtesteada.
        sr = sr15_context(m15, bar_ts, entry, self.direction)
        if not sr.get("sr15_has_room"):
            return None
        if float(sr.get("sr15_target_dist_bps") or 0) < PARAMS.sr15_min_target_bps:
            return None

        target = round(up if self.direction == "CALL" else low, 4)
        stop = round(mid * (1 - STOP_FRAC) if self.direction == "CALL"
                     else mid * (1 + STOP_FRAC), 4)
        if not valid_target_geometry(self.direction, entry, target, stop):
            return None

        return Signal(
            direction=self.direction,
            signal_ts=ctx.et(entry_ts).to_pydatetime(),
            underlying=entry,
            meta={
                "confirmation": self.confirmation,
                "confirmation_bar_start": bar_ts.isoformat(),
                "confirmation_bar_end": entry_ts.isoformat(),
                "entry_timing": "15m_bar_close",
                "bb_mid": mid, "bb_upper": up, "bb_lower": low,
                "target_underlying": target,
                "stop_underlying": stop,
                "side_strength": round(float(strength), 4),
                "volume_ratio": round(float(vr), 4) if np.isfinite(vr) else None,
                "body_frac": round(body_frac(row), 4),
                "cooldown_minutes": int(self.params["cooldown_minutes"]),
                **sr,
            },
        )

    def select_contract(self, ctx: ScanContext, signal: Signal, at=None):
        """Strike ITM mas cercano con quote viva. Vencimiento hasta 7 dias:
        el hold puede llegar a 180 minutos y un 0DTE se vacia de valor."""
        spot = signal.underlying
        is_call = signal.direction == "CALL"
        base = math.floor(spot) if is_call else math.ceil(spot)
        depth = int(self.params["strike_depth"])
        strikes = [base - i if is_call else base + i for i in range(depth)]

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
                if quote is not None and quote.is_live:
                    return occ, expiration, float(strike), quote
        return None, None, None, None

    def check_exit(self, ctx: ScanContext, alert) -> ExitDecision:
        target = alert.meta.get("target_underlying")
        stop = alert.meta.get("stop_underlying")
        if target is None or stop is None or alert.entry_ts is None:
            return ExitDecision(should_exit=False)
        return target_stop_exit(
            ctx.causal_bars(1), alert.direction,
            float(target), float(stop), alert.entry_ts)


# --- Las cuatro reglas no-TimesFM ---------------------------------------

@register
class TslaBB1hCallSr15(BBMidpointBase):
    strategy_id = "TSLA_BB1H_CALL_SR15"
    name = "TSLA BB 1H CALL body + S/R 15m"
    symbol = "TSLA"
    direction = "CALL"
    confirmation = "body"


@register
class AaplBB1hCallSr15(BBMidpointBase):
    strategy_id = "AAPL_BB1H_CALL_SR15"
    name = "AAPL BB 1H CALL body + S/R 15m"
    symbol = "AAPL"
    direction = "CALL"
    confirmation = "body"


@register
class SpyPutBB1hBodyVolume(BBMidpointBase):
    strategy_id = "SPY_PUT_BB1H_BODY_VOLUME"
    name = "SPY PUT BB 1H body_volume"
    symbol = "SPY"
    direction = "PUT"
    confirmation = "body_volume"


@register
class SpyPutBB1hCloseVolume(BBMidpointBase):
    strategy_id = "SPY_PUT_BB1H_CLOSE_VOLUME"
    name = "SPY PUT BB 1H close_volume"
    symbol = "SPY"
    direction = "PUT"
    confirmation = "close_volume"
