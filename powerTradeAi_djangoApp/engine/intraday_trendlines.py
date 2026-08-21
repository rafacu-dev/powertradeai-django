"""Analisis visual causal de rupturas estructurales intradia en velas de 1m."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time

import numpy as np
import pandas as pd

from ..data import get_provider, occ_symbol
from .session import NY, RTH_OPEN, is_trading_day, session_close


PIVOT_WINDOW = 2
MAX_CONFIRM_BARS = 3
FAILED_SWING_TOLERANCE_BPS = 5.0
MIN_SLOPE_BPS_PER_MIN = 0.05
MIN_TREND_R2 = 0.45
MAX_SETUPS = 4
SIGNAL_END = "11:30"


@dataclass
class IntradayTrendlineTimeline:
    day: date
    symbol: str
    timeframe: str = "1m"
    candles: list[dict] = field(default_factory=list)
    setups: list[dict] = field(default_factory=list)


def intraday_trendline_timeline(
    day: date, symbol: str, provider=None,
) -> IntradayTrendlineTimeline:
    """Devuelve la sesion de 1m y sus rupturas estructurales confirmadas."""
    if not is_trading_day(day):
        raise ValueError(f"{day} no es un dia habil de mercado")

    symbol = symbol.upper()
    provider = provider or get_provider()
    source = _spx_parity_bars(provider, day) if symbol == "SPX" else provider.bars_1m(symbol, day)
    bars = _session_bars(source, day)
    timeline = IntradayTrendlineTimeline(day=day, symbol=symbol)
    timeline.candles = _candles_payload(bars)
    timeline.setups = structural_reversal_setups(bars)
    return timeline


def _spx_parity_bars(provider, day: date) -> pd.DataFrame:
    """Forward de SPX a 1m: strike + call_mid - put_mid de SPXW 0DTE."""
    spy = _session_bars(provider.bars_1m("SPY", day), day)
    if spy.empty:
        return spy
    estimate = float(spy["open"].iloc[0]) * 10.025
    center = round(estimate / 5.0) * 5.0
    start = datetime.combine(day, time(9, 30), tzinfo=NY)
    end = datetime.combine(day, session_close(day), tzinfo=NY)
    for strike in (center, center - 5, center + 5, center - 10, center + 10):
        call_occ = occ_symbol("SPXW", day, "CALL", strike)
        put_occ = occ_symbol("SPXW", day, "PUT", strike)
        try:
            call = provider.option_quotes(call_occ, start, end, interval="1m")
            put = provider.option_quotes(put_occ, start, end, interval="1m")
        except Exception:
            continue
        call_mid = _valid_option_mid(call)
        put_mid = _valid_option_mid(put)
        joined = pd.concat({"call": call_mid, "put": put_mid}, axis=1).dropna()
        if len(joined) < 100:
            continue
        quote = strike + joined["call"] - joined["put"]
        quote = quote[(quote - estimate).abs() <= 150]
        quote = quote[~quote.index.duplicated(keep="last")].sort_index()
        if len(quote) < 100:
            continue
        frame = pd.DataFrame({"open": quote, "close": quote.shift(-1)}).dropna()
        frame["high"] = frame[["open", "close"]].max(axis=1)
        frame["low"] = frame[["open", "close"]].min(axis=1)
        frame["volume"] = 0.0
        return frame
    return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])


def _valid_option_mid(frame: pd.DataFrame) -> pd.Series:
    if frame is None or frame.empty or not {"bid", "ask"}.issubset(frame.columns):
        return pd.Series(dtype=float)
    bid = pd.to_numeric(frame["bid"], errors="coerce")
    ask = pd.to_numeric(frame["ask"], errors="coerce")
    return ((bid + ask) / 2.0).where((bid > 0) & (ask > bid))


def _session_bars(bars: pd.DataFrame, day: date) -> pd.DataFrame:
    if bars is None or bars.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame = bars.copy()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    frame.index = index.tz_convert("UTC")
    local = frame.index.tz_convert(NY)
    close_at = session_close(day)
    mask = (
        (local.date == day)
        & (local.time >= RTH_OPEN)
        & (local.time < close_at)
    )
    frame = frame.loc[mask]
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna(subset=["open", "high", "low", "close"])


def _candles_payload(bars: pd.DataFrame) -> list[dict]:
    return [
        {
            "time": int(ts.timestamp()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for ts, row in bars.iterrows()
    ]


def _confirmed_pivots(
    highs: np.ndarray, lows: np.ndarray, window: int = PIVOT_WINDOW,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    pivot_highs: list[tuple[int, float]] = []
    pivot_lows: list[tuple[int, float]] = []
    for index in range(window, len(highs) - window):
        high_window = highs[index - window:index + window + 1]
        low_window = lows[index - window:index + window + 1]
        if highs[index] >= np.max(high_window):
            pivot_highs.append((index, float(highs[index])))
        if lows[index] <= np.min(low_window):
            pivot_lows.append((index, float(lows[index])))
    return pivot_highs, pivot_lows


def _fit_line(pivots: list[tuple[int, float]]) -> dict | None:
    if len(pivots) < 4:
        return None
    selected = pivots[-6:]
    x = np.array([point[0] for point in selected], dtype=float)
    y = np.array([point[1] for point in selected], dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    denominator = float(np.sum((y - y.mean()) ** 2))
    r2 = 0.0 if denominator == 0 else 1.0 - float(np.sum((y - fitted) ** 2)) / denominator
    return {
        "pivots": selected,
        "slope": float(slope),
        "intercept": float(intercept),
        "r2": float(r2),
    }


def structural_reversal_setups(bars: pd.DataFrame) -> list[dict]:
    """Detecta rupturas sin mirar velas posteriores a la confirmacion."""
    if bars is None or len(bars) < 20:
        return []
    local = bars.index.tz_convert(NY)
    scan = bars[local.time <= datetime.strptime(SIGNAL_END, "%H:%M").time()]
    if len(scan) < 20:
        return []

    highs = scan["high"].to_numpy(dtype=float)
    lows = scan["low"].to_numpy(dtype=float)
    closes = scan["close"].to_numpy(dtype=float)
    pivot_highs, pivot_lows = _confirmed_pivots(highs, lows)
    setups: list[dict] = []
    last_confirmation: dict[str, int] = {}

    for confirm_index in range(15, len(scan) - 1):
        for direction in ("LONG", "SHORT"):
            if confirm_index - last_confirmation.get(direction, -10_000) < 30:
                continue
            reversal_source = pivot_lows if direction == "LONG" else pivot_highs
            reversal_pivots = [
                point for point in reversal_source
                if point[0] + PIVOT_WINDOW <= confirm_index
                and confirm_index - point[0] <= 30
            ]
            if len(reversal_pivots) < 2:
                continue
            first_reversal, last_reversal = reversal_pivots[-2:]
            tolerance = FAILED_SWING_TOLERANCE_BPS / 10_000.0
            if direction == "LONG" and last_reversal[1] < first_reversal[1] * (1 - tolerance):
                continue
            if direction == "SHORT" and last_reversal[1] > first_reversal[1] * (1 + tolerance):
                continue

            setup = _confirmed_setup(
                scan, closes, pivot_highs, pivot_lows, direction,
                confirm_index, first_reversal, last_reversal,
            )
            if setup is None:
                continue
            setup.update(_evaluate_setup(bars, setup))
            setups.append(setup)
            last_confirmation[direction] = confirm_index
            if len(setups) >= MAX_SETUPS:
                return sorted(setups, key=lambda item: item["confirm_time"])
    return sorted(setups, key=lambda item: item["confirm_time"])


def _confirmed_setup(
    scan: pd.DataFrame,
    closes: np.ndarray,
    pivot_highs: list[tuple[int, float]],
    pivot_lows: list[tuple[int, float]],
    direction: str,
    confirm_index: int,
    first_reversal: tuple[int, float],
    last_reversal: tuple[int, float],
) -> dict | None:
    for break_index in range(max(1, confirm_index - MAX_CONFIRM_BARS), confirm_index):
        trend_source = pivot_highs if direction == "LONG" else pivot_lows
        trend_pivots = [
            point for point in trend_source
            if point[0] + PIVOT_WINDOW <= break_index - 1
            and break_index - point[0] <= 60
        ]
        fitted = _fit_line(trend_pivots)
        if fitted is None or fitted["r2"] < MIN_TREND_R2:
            continue
        slope = fitted["slope"]
        intercept = fitted["intercept"]
        slope_bps = slope / closes[break_index] * 10_000.0
        previous = float(closes[break_index - 1])
        broken = float(closes[break_index])
        confirmed = float(closes[confirm_index])
        previous_line = slope * (break_index - 1) + intercept
        break_line = slope * break_index + intercept
        last_swing = trend_pivots[-1][1]

        if direction == "LONG":
            valid = (
                slope_bps <= -MIN_SLOPE_BPS_PER_MIN
                and previous <= previous_line
                and broken > break_line
                and confirmed > broken
                and confirmed > last_swing
            )
            target = max(point[1] for point in fitted["pivots"])
            stop = min(first_reversal[1], last_reversal[1])
        else:
            valid = (
                slope_bps >= MIN_SLOPE_BPS_PER_MIN
                and previous >= previous_line
                and broken < break_line
                and confirmed < broken
                and confirmed < last_swing
            )
            target = min(point[1] for point in fitted["pivots"])
            stop = max(first_reversal[1], last_reversal[1])
        if not valid:
            continue

        entry_index = confirm_index + 1
        entry = float(scan["open"].iloc[entry_index])
        if direction == "LONG" and not (stop < entry < target):
            continue
        if direction == "SHORT" and not (target < entry < stop):
            continue
        trend_start_index = fitted["pivots"][0][0]
        return {
            "direction": direction,
            "break_time": int(scan.index[break_index].timestamp()),
            "confirm_time": int(scan.index[confirm_index].timestamp()),
            "entry_time": int(scan.index[entry_index].timestamp()),
            "entry_price": round(entry, 4),
            "target_price": round(float(target), 4),
            "stop_price": round(float(stop), 4),
            "trend_r2": round(float(fitted["r2"]), 4),
            "trend_slope_bps_min": round(float(slope_bps), 4),
            "trendline": [
                {
                    "time": int(scan.index[trend_start_index].timestamp()),
                    "value": round(float(slope * trend_start_index + intercept), 4),
                },
                {
                    "time": int(scan.index[confirm_index].timestamp()),
                    "value": round(float(slope * confirm_index + intercept), 4),
                },
            ],
            "rejection_line": [
                {
                    "time": int(scan.index[first_reversal[0]].timestamp()),
                    "value": round(float(first_reversal[1]), 4),
                },
                {
                    "time": int(scan.index[last_reversal[0]].timestamp()),
                    "value": round(float(last_reversal[1]), 4),
                },
            ],
        }
    return None


def _evaluate_setup(bars: pd.DataFrame, setup: dict) -> dict:
    direction = setup["direction"]
    entry = float(setup["entry_price"])
    target = float(setup["target_price"])
    stop = float(setup["stop_price"])
    entry_time = pd.Timestamp(setup["entry_time"], unit="s", tz="UTC")
    future = bars[bars.index >= entry_time]
    outcome = "EOD"
    exit_time = int(future.index[-1].timestamp())
    exit_price = float(future["close"].iloc[-1])
    for ts, row in future.iterrows():
        if direction == "LONG":
            stop_hit = float(row["low"]) <= stop
            target_hit = float(row["high"]) >= target
        else:
            stop_hit = float(row["high"]) >= stop
            target_hit = float(row["low"]) <= target
        if stop_hit:
            outcome, exit_time, exit_price = "STOP", int(ts.timestamp()), stop
            break
        if target_hit:
            outcome, exit_time, exit_price = "TARGET", int(ts.timestamp()), target
            break
    risk = entry - stop if direction == "LONG" else stop - entry
    signed = 1.0 if direction == "LONG" else -1.0
    pnl_r = signed * (exit_price - entry) / risk
    return {
        "outcome": outcome,
        "exit_time": exit_time,
        "exit_price": round(float(exit_price), 4),
        "pnl_r": round(float(pnl_r), 4),
    }
