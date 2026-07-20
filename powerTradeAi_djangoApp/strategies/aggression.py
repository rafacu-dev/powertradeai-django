"""TSLA W5: movimiento agresivo en ventanas de segundos.

Portado de ``paper/engines/aggression.py`` y del detector de
``research/hypotheses/dal_agresividad_segundos.py``.

A diferencia del resto de familias, esta NO trabaja sobre velas de un minuto:
construye barras de UN SEGUNDO a partir del tape de operaciones y busca
movimientos bruscos con volumen y numero de operaciones muy por encima de su
propia linea base.

La cadena de filtros de ``TSLA_W5_STABLE``, congelada por el artefacto de 120
dias (``w5_h1_not_contra_premium800_trade5_score15``):

  1. Trigger del detector en ventana de 5 segundos.
  2. ``trade_ratio >= 5.0`` y ``score >= 15.0``.
  3. Estructura horaria que no vaya en contra (EMA9/EMA20 sobre velas cerradas).
  4. Prima del contrato < $800.
  5. ``confirm10``: en los 10 segundos siguientes a la entrada, el bid debe
     alcanzar ``entry_ask * 1.05``. Si no, se aborta la posicion.

Sobre el punto 5: un scanner que consulta cada 30 segundos no puede observar en
vivo el maximo del bid en una ventana de 10. Aqui se reconstruye a posteriori
con la serie historica de quotes del contrato. Si el proveedor no la sirve, el
gate se declara indeterminado y NO se cierra la alerta por confirm10 — antes
dejarla viva que inventar un aborto que no se ha observado.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

from ..data import candidate_expirations, occ_symbol
from .base import BaseStrategy, ExitDecision, ScanContext, Signal, register

# Parametros congelados del detector (m22_v4_tr3_cd180).
WINDOWS = (5, 10, 15, 30)
MIN_MOVE_BPS = 22.0
MIN_VOL_RATIO = 4.0
MIN_TRADE_RATIO = 3.0
BASELINE_SEC = 300
COOLDOWN_SEC = 180
# 300s de baseline + 180s de cooldown + margen para reconstruir la cadena sin
# recorrer toda la sesion en cada pasada.
REPLAY_LOOKBACK_SEC = 900

# Filtros propios de W5_STABLE.
W5_MIN_TRADE_RATIO = 5.0
W5_MIN_SCORE = 15.0
W5_MAX_PREMIUM_DOLLARS = 800.0
CONFIRM_SECONDS = 10
CONFIRM_BID_VS_ASK_PCT = 5.0

EMA_WINDOWS = (9, 20)


@dataclass(frozen=True)
class Trigger:
    ts: pd.Timestamp
    direction: str
    window_sec: int
    entry: float
    move_bps: float
    vol_ratio: float
    trade_ratio: float
    speed_bps_s: float
    score: float


# --- Barras de un segundo ----------------------------------------------

def second_bars(trades: pd.DataFrame) -> pd.DataFrame:
    """Agrega el tape a barras de 1 segundo.

    ``trades`` cuenta operaciones, no volumen: el detector usa las dos series
    por separado y confundirlas cambia ``trade_ratio``.
    """
    if trades is None or trades.empty:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "trades", "vwap"],
            index=pd.DatetimeIndex([], tz="UTC"))

    price = trades["price"].resample("1s").ohlc()
    out = price.copy()
    out["volume"] = trades["size"].resample("1s").sum()
    out["trades"] = trades["price"].resample("1s").count()
    out["vwap"] = (trades["price"].mul(trades["size"]).resample("1s").sum()
                   / out["volume"].replace(0, np.nan))
    # Un segundo sin operaciones no es un hueco de precio: arrastra el ultimo.
    out["close"] = out["close"].ffill()
    for column in ("open", "high", "low"):
        out[column] = out[column].fillna(out["close"])
    out[["volume", "trades"]] = out[["volume", "trades"]].fillna(0)
    out["vwap"] = out["vwap"].fillna(out["close"])
    return out.dropna(subset=["close"])


def detect_aggression(sec: pd.DataFrame, windows=WINDOWS,
                      min_move_bps=MIN_MOVE_BPS, min_vol_ratio=MIN_VOL_RATIO,
                      min_trade_ratio=MIN_TRADE_RATIO,
                      baseline_sec=BASELINE_SEC,
                      cooldown_sec=COOLDOWN_SEC) -> list[Trigger]:
    """Movimientos bruscos con volumen y operaciones sobre su linea base.

    Por cada segundo se prueban todas las ventanas y se queda la de mayor
    ``score``. Tras un trigger, ``cooldown_sec`` de silencio.
    """
    triggers: list[Trigger] = []
    last_ts: pd.Timestamp | None = None
    close, volume, trades = sec["close"], sec["volume"], sec["trades"]

    for ts in sec.index:
        if last_ts is not None and ts < last_ts + pd.Timedelta(seconds=cooldown_sec):
            continue
        best: Trigger | None = None
        for win in windows:
            past_ts = ts - pd.Timedelta(seconds=win)
            if past_ts not in sec.index:
                continue
            p0, p1 = float(close.loc[past_ts]), float(close.loc[ts])
            if p0 <= 0:
                continue
            move_bps = (p1 / p0 - 1.0) * 10000.0
            if abs(move_bps) < min_move_bps:
                continue

            baseline_start = ts - pd.Timedelta(seconds=baseline_sec)
            hist = sec[(sec.index < past_ts) & (sec.index >= baseline_start)]
            if len(hist) < max(60, win * 4):
                continue
            cur_vol = float(volume.loc[past_ts:ts].sum())
            cur_trades = float(trades.loc[past_ts:ts].sum())
            base_vol = float(hist["volume"].rolling(win).sum().median())
            base_trades = float(hist["trades"].rolling(win).sum().median())
            vol_ratio = cur_vol / base_vol if base_vol > 0 else np.inf
            trade_ratio = cur_trades / base_trades if base_trades > 0 else np.inf
            if vol_ratio < min_vol_ratio or trade_ratio < min_trade_ratio:
                continue

            candidate = Trigger(
                ts=ts,
                direction="CALL" if move_bps > 0 else "PUT",
                window_sec=win,
                entry=p1,
                move_bps=round(move_bps, 2),
                vol_ratio=round(float(vol_ratio), 2),
                trade_ratio=round(float(trade_ratio), 2),
                speed_bps_s=round(abs(move_bps) / win, 3),
                score=round(float(
                    abs(move_bps) * np.log1p(vol_ratio)
                    * np.log1p(trade_ratio) / win), 3),
            )
            if best is None or candidate.score > best.score:
                best = candidate
        if best is not None:
            triggers.append(best)
            last_ts = ts
    return triggers


# --- Estructura horaria -------------------------------------------------

def with_emas(bars: pd.DataFrame) -> pd.DataFrame:
    out = bars.copy()
    for window in EMA_WINDOWS:
        out[f"ema{window}"] = out["close"].ewm(span=window, adjust=False).mean()
        out[f"ema{window}_slope"] = out[f"ema{window}"].diff(3)
    return out.dropna()


def structure_support(row, direction: str) -> str:
    """'favor', 'contra' o 'mixto' segun la estructura de medias."""
    bull = (row["close"] > row["ema20"] and row["ema9"] > row["ema20"]
            and row["ema20_slope"] > 0)
    bear = (row["close"] < row["ema20"] and row["ema9"] < row["ema20"]
            and row["ema20_slope"] < 0)
    if direction == "CALL":
        if bull:
            return "favor"
        if bear:
            return "contra"
    else:
        if bear:
            return "favor"
        if bull:
            return "contra"
    return "mixto"


# --- Regla --------------------------------------------------------------

@register
class TslaW5Stable(BaseStrategy):
    strategy_id = "TSLA_W5_STABLE"
    name = "TSLA W5 filtro estable"
    symbol = "TSLA"
    rule_version = "w5_stable_exact_v4_causal_tplus1"
    default_params = {
        "window_sec": 5,
        "min_trade_ratio": W5_MIN_TRADE_RATIO,
        "min_score": W5_MIN_SCORE,
        "max_premium_dollars": W5_MAX_PREMIUM_DOLLARS,
        "confirm_seconds": CONFIRM_SECONDS,
        "confirm_bid_vs_entry_ask_pct": CONFIRM_BID_VS_ASK_PCT,
        "hold_minutes": 30,
        "flatten_at": "15:55",
        "max_signal_age_seconds": 15,
        "strike_depth": 8,
        "max_dte": 7,
    }

    # --- Deteccion ------------------------------------------------------

    def _second_bars(self, ctx: ScanContext) -> pd.DataFrame:
        """Barras de 1s de la ventana de replay que precede a ``now``."""
        end = ctx.now
        start = end - timedelta(seconds=REPLAY_LOOKBACK_SEC)
        tape = ctx.provider.trades(self.symbol, start, end)
        return second_bars(tape)

    def _h1_support(self, ctx: ScanContext, signal_ts, direction: str) -> str:
        """Estructura horaria con velas cuyo intervalo YA cerro en la señal."""
        history = ctx.history("1h", days=140)
        today = ctx.resample("1h")
        h1 = pd.concat([history, today]).sort_index()
        h1 = h1[~h1.index.duplicated(keep="last")]
        if h1.empty:
            return "sin_datos"
        cut = pd.Timestamp(signal_ts).tz_convert("UTC")
        closed = h1[h1.index + pd.Timedelta(hours=1) <= cut]
        enriched = with_emas(closed)
        if enriched.empty:
            return "sin_datos"
        return structure_support(enriched.iloc[-1], direction)

    def evaluate(self, ctx: ScanContext) -> Signal | None:
        sec = self._second_bars(ctx)
        if sec.empty:
            return None

        triggers = detect_aggression(sec)
        if not triggers:
            return None

        # El detector devuelve todos los triggers de la ventana; solo interesa
        # el ultimo, y solo si aun es reciente.
        trigger = triggers[-1]
        if int(trigger.window_sec) != int(self.params["window_sec"]):
            return None
        if float(trigger.trade_ratio) < float(self.params["min_trade_ratio"]):
            return None
        if float(trigger.score) < float(self.params["min_score"]):
            return None

        signal_ts = pd.Timestamp(trigger.ts)
        signal_ts = (signal_ts.tz_localize("UTC") if signal_ts.tzinfo is None
                     else signal_ts.tz_convert("UTC"))
        age = (pd.Timestamp(ctx.now).tz_convert("UTC") - signal_ts).total_seconds()
        if age > float(self.params["max_signal_age_seconds"]):
            # Una señal vieja no se compra: simularlo inflaria el resultado.
            return None

        support = self._h1_support(ctx, signal_ts, trigger.direction)
        if support == "sin_datos" or support not in {"favor", "mixto"}:
            return None

        return Signal(
            direction=trigger.direction,
            signal_ts=signal_ts.to_pydatetime(),
            underlying=float(trigger.entry),
            meta={
                "frozen_rule": "w5_h1_not_contra_premium800_trade5_score15",
                "detector": "m22_v4_tr3_cd180",
                "window_sec": int(trigger.window_sec),
                "move_bps": float(trigger.move_bps),
                "vol_ratio": float(trigger.vol_ratio),
                "trade_ratio": float(trigger.trade_ratio),
                "speed_bps_s": float(trigger.speed_bps_s),
                "score": float(trigger.score),
                "h1_support": support,
                "underlying_bar_interval": "1s_from_trades",
                "confirm_seconds": int(self.params["confirm_seconds"]),
                "confirm_bid_vs_entry_ask_pct": float(
                    self.params["confirm_bid_vs_entry_ask_pct"]),
            },
        )

    # --- Contrato -------------------------------------------------------

    def select_contract(self, ctx: ScanContext, signal: Signal, at=None):
        """Strike ITM mas cercano con prima < $800.

        El gate de prima no es opcional: en el journal del productor hay
        candidatos rechazados por prima ($965) que, sin este filtro, se
        convertirian aqui en operaciones que el backtest nunca tuvo.
        """
        spot = signal.underlying
        is_call = signal.direction == "CALL"
        base = math.floor(spot) if is_call else math.ceil(spot)
        depth = int(self.params["strike_depth"])
        strikes = [base - i if is_call else base + i for i in range(depth)]
        max_premium = float(self.params["max_premium_dollars"])

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
                if quote is None or not quote.is_live:
                    continue
                if quote.ask * 100.0 >= max_premium:
                    continue
                return occ, expiration, float(strike), quote
        return None, None, None, None

    # --- Salida ---------------------------------------------------------

    def check_exit(self, ctx: ScanContext, alert) -> ExitDecision:
        """Gate ``confirm10``: si el bid no despega, se aborta la posicion."""
        if alert.entry_ts is None or alert.entry_ask is None:
            return ExitDecision(should_exit=False)
        if alert.meta.get("confirm10_status") == "confirmed":
            return ExitDecision(should_exit=False)

        window = int(self.params["confirm_seconds"])
        entry_ts = pd.Timestamp(alert.entry_ts).tz_convert("UTC")
        due = entry_ts + pd.Timedelta(seconds=window)
        if pd.Timestamp(ctx.now).tz_convert("UTC") < due:
            return ExitDecision(should_exit=False)   # aun dentro de la ventana

        threshold = float(alert.entry_ask) * (
            1.0 + float(self.params["confirm_bid_vs_entry_ask_pct"]) / 100.0)

        try:
            quotes = ctx.provider.option_quotes(
                alert.occ_symbol, entry_ts.to_pydatetime(), due.to_pydatetime())
        except Exception:
            # Sin serie de quotes el gate queda INDETERMINADO. No se cierra por
            # confirm10: abortar sin haber observado el bid seria inventarse el
            # motivo del cierre.
            return ExitDecision(should_exit=False)

        if quotes is None or quotes.empty:
            return ExitDecision(should_exit=False)

        if float(quotes["bid"].max()) >= threshold:
            # Confirmada: se anota para no volver a pedir la serie cada pasada.
            alert.meta["confirm10_status"] = "confirmed"
            alert.save(update_fields=["meta", "updated_at"])
            return ExitDecision(should_exit=False)

        return ExitDecision(should_exit=True, reason="confirm10_abort", at=due)
