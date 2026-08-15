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

import numpy as np
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
    trendlines: list[dict] = field(default_factory=list)
    breakouts: list[dict] = field(default_factory=list)
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
    intraday_bars = _intraday_bars_5m(provider, symbol, day)
    timeline.candles = _candles_payload(display_bars)
    timeline.replay_start_time = _first_replay_candle_time(display_bars, day)
    timeline.trendlines = _trendlines_payload(
        provider, symbol, day, display_bars, intraday_bars)
    timeline.breakouts = _confirmed_breakouts(display_bars, day, timeline.trendlines)
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
    return _resample_intraday_bars(bars, "15min")


def _intraday_bars_5m(provider, symbol: str, day: date) -> pd.DataFrame:
    start = _previous_trading_days_start(day, count=5)
    bars = provider.bars(symbol, start, day, "1m")
    return _resample_intraday_bars(bars, "5min")


def _resample_intraday_bars(bars: pd.DataFrame, rule: str) -> pd.DataFrame:
    rth = _rth_only(bars)
    if rth is None or rth.empty:
        return rth
    out = rth.resample(
        rule, label="left", closed="left",
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


def _trendlines_payload(provider, symbol: str, day: date,
                        display_bars: pd.DataFrame,
                        intraday_bars: pd.DataFrame | None = None) -> list[dict]:
    """Lineas multi-temporalidad calculadas con datos cerrados antes del replay."""
    if display_bars is None or display_bars.empty:
        return []
    draw_start = int(display_bars.index[0].timestamp())
    draw_end = int(display_bars.index[-1].timestamp())
    previous = day - timedelta(days=1)
    specs = [
        ("1mo", _monthly_bars(provider, symbol, previous), 12),
        ("1w", _weekly_bars(provider, symbol, previous), 52),
        ("1d", provider.bars(symbol, previous - timedelta(days=260), previous, "1d"), 120),
        ("1h", _rth_only(provider.bars(
            symbol, previous - timedelta(days=90), previous, "1h")), 160),
        ("15m", display_bars[
            display_bars.index.tz_convert(NY).date < day
        ], 140),
    ]
    out: list[dict] = []
    for timeframe, bars, lookback in specs:
        frame = bars.tail(lookback) if bars is not None and not bars.empty else bars
        out.extend(_trendlines_for_frame(frame, timeframe, draw_start, draw_end))
    if intraday_bars is None or intraday_bars.empty:
        intraday_bars = display_bars
    out.extend(_intraday_trendlines_5m(intraday_bars, day))
    return out


def _monthly_bars(provider, symbol: str, end: date) -> pd.DataFrame:
    daily = provider.bars(symbol, end - timedelta(days=550), end, "1d")
    return _resample_ohlc(daily, "ME")


def _weekly_bars(provider, symbol: str, end: date) -> pd.DataFrame:
    daily = provider.bars(symbol, end - timedelta(days=420), end, "1d")
    return _resample_ohlc(daily, "W-FRI")


def _resample_ohlc(bars: pd.DataFrame, rule: str) -> pd.DataFrame:
    if bars is None or bars.empty:
        return bars
    return bars.resample(rule).agg({
        "open": "first", "high": "max", "low": "min",
        "close": "last", "volume": "sum",
    }).dropna(subset=["close"])


def _trendlines_for_frame(bars: pd.DataFrame, timeframe: str,
                          draw_start: int, draw_end: int) -> list[dict]:
    if bars is None or len(bars) < 8:
        return []
    highs = bars["high"].astype(float).to_numpy()
    lows = bars["low"].astype(float).to_numpy()
    closes = bars["close"].astype(float).to_numpy()
    piv_high, piv_low = _pivot_points(highs, lows)
    lines = []
    source_last = int(bars.index[-1].timestamp())

    res = _best_diagonal_line(piv_high, len(bars), timeframe, "resistencia")
    if res is not None:
        lines.append(_line_payload(res, draw_start, draw_end, source_last))
    sup = _best_diagonal_line(piv_low, len(bars), timeframe, "soporte")
    if sup is not None:
        lines.append(_line_payload(sup, draw_start, draw_end, source_last))

    for level in _horizontal_levels(piv_high + piv_low, closes):
        lines.append({
            "timeframe": timeframe,
            "kind": "corte",
            "direction": "lateral",
            "touches": level["touches"],
            "score": level["touches"],
            "points": [
                {"time": draw_start, "value": level["price"]},
                {"time": draw_end, "value": level["price"]},
            ],
            "label": f"{timeframe} corte {level['price']}",
        })
    return lines


def _pivot_points(highs: np.ndarray, lows: np.ndarray, window: int = 2):
    piv_high, piv_low = [], []
    n = len(highs)
    for i in range(window, n - window):
        h_slice = highs[i - window:i + window + 1]
        l_slice = lows[i - window:i + window + 1]
        if highs[i] == h_slice.max() and np.count_nonzero(h_slice == highs[i]) == 1:
            piv_high.append((i, float(highs[i])))
        if lows[i] == l_slice.min() and np.count_nonzero(l_slice == lows[i]) == 1:
            piv_low.append((i, float(lows[i])))
    return piv_high, piv_low


def _best_diagonal_line(points: list[tuple[int, float]], n: int,
                        timeframe: str, kind: str) -> dict | None:
    if len(points) < 2:
        return None
    pivots = points[-8:]
    best = None
    for i in range(len(pivots) - 1):
        for j in range(i + 1, len(pivots)):
            x1, y1 = pivots[i]
            x2, y2 = pivots[j]
            if x2 == x1:
                continue
            slope = (y2 - y1) / (x2 - x1)
            if kind == "resistencia" and slope >= 0:
                continue
            if kind == "soporte" and slope <= 0:
                continue
            intercept = y1 - slope * x1
            prices = np.array([p[1] for p in pivots], dtype=float)
            xs = np.array([p[0] for p in pivots], dtype=float)
            projected = slope * xs + intercept
            tol = max(float(np.median(prices)) * 0.004, 0.01)
            touches = int(np.count_nonzero(np.abs(prices - projected) <= tol))
            if touches < 3:
                continue
            score = touches * 10 + abs(x2 - x1)
            candidate = {
                "timeframe": timeframe,
                "kind": kind,
                "direction": "bajista" if kind == "resistencia" else "alcista",
                "touches": touches,
                "score": score,
                "slope": float(slope),
                "intercept": float(intercept),
                "start_index": max(0, x1 - 1),
                "end_index": n - 1,
            }
            if best is None or candidate["score"] > best["score"]:
                best = candidate
    return best


def _line_payload(line: dict, start_time: int, end_time: int,
                  source_last_time: int) -> dict:
    seconds = _timeframe_seconds(line["timeframe"])
    x1 = line["end_index"] + (start_time - source_last_time) / seconds
    x2 = line["end_index"] + (end_time - source_last_time) / seconds
    y1 = line["slope"] * x1 + line["intercept"]
    y2 = line["slope"] * x2 + line["intercept"]
    return {
        "timeframe": line["timeframe"],
        "kind": line["kind"],
        "direction": line["direction"],
        "touches": line["touches"],
        "score": line["score"],
        "points": [
            {"time": start_time, "value": round(float(y1), 4)},
            {"time": end_time, "value": round(float(y2), 4)},
        ],
        "label": f"{line['timeframe']} {line['kind']} {line['direction']}",
    }


def _timeframe_seconds(timeframe: str) -> int:
    return {
        "5m": 5 * 60,
        "15m": 15 * 60,
        "1h": 60 * 60,
        "1d": 24 * 60 * 60,
        "1w": 7 * 24 * 60 * 60,
        "1mo": 30 * 24 * 60 * 60,
    }[timeframe]


def _horizontal_levels(points: list[tuple[int, float]], closes: np.ndarray) -> list[dict]:
    if len(points) < 3:
        return []
    prices = sorted(float(p[1]) for p in points)
    ref = float(np.median(closes)) if len(closes) else prices[-1]
    tol = max(ref * 0.004, 0.01)
    clusters: list[list[float]] = []
    for price in prices:
        if clusters and abs(price - np.mean(clusters[-1])) <= tol:
            clusters[-1].append(price)
        else:
            clusters.append([price])
    levels = [
        {"price": round(float(np.mean(cluster)), 4), "touches": len(cluster)}
        for cluster in clusters if len(cluster) >= 3
    ]
    return sorted(levels, key=lambda item: item["touches"], reverse=True)[:3]


def _intraday_trendlines_15m(bars: pd.DataFrame, replay_day: date) -> list[dict]:
    return _intraday_trendlines_5m(bars, replay_day)


def _intraday_trendlines_5m(bars: pd.DataFrame, replay_day: date) -> list[dict]:
    """Lineas cortas intradia calculadas con swings de 5 minutos."""
    if bars is None or bars.empty:
        return []
    previous = bars[bars.index.tz_convert(NY).date < replay_day]
    if previous.empty:
        return []
    out: list[dict] = []
    for _, session in previous.groupby(previous.index.tz_convert(NY).date):
        out.extend(_session_micro_trendlines(session))
    return out


def _session_micro_trendlines(session: pd.DataFrame) -> list[dict]:
    if len(session) < 5:
        return []
    highs = session["high"].astype(float).to_numpy()
    lows = session["low"].astype(float).to_numpy()
    candidates = []
    candidates.extend(_swing_trendlines(session, highs, "resistencia"))
    candidates.extend(_swing_trendlines(session, lows, "soporte"))
    return _select_swing_lines(candidates)


def _swing_trendlines(session: pd.DataFrame, values: np.ndarray,
                      kind: str) -> list[dict]:
    pivots = _swing_points(values, kind)
    if len(pivots) < 2:
        return []
    ref = float(np.median(values))
    min_move = max(ref * 0.0015, 0.05)
    tol = max(ref * 0.0012, 0.03)
    out = []
    for left in range(len(pivots) - 1):
        for right in range(left + 1, len(pivots)):
            i, y1 = pivots[left]
            j, y2 = pivots[right]
            span = j - i
            if span < 2:
                continue
            move = abs(y2 - y1)
            if move < min_move:
                continue
            slope = (y2 - y1) / span
            if kind == "resistencia" and slope >= 0:
                continue
            if kind == "soporte" and slope <= 0:
                continue
            intercept = y1 - slope * i
            xs = np.arange(i, j + 1, dtype=float)
            projected = slope * xs + intercept
            segment = values[i:j + 1]
            if kind == "resistencia":
                violation = np.max(segment - projected)
            else:
                violation = np.max(projected - segment)
            if float(violation) > tol * 3:
                continue
            touch_idx = np.flatnonzero(np.abs(segment - projected) <= tol)
            touch_zones = _touch_zones(touch_idx)
            touches = len(touch_zones)
            if touches < 3:
                continue
            first_touch, last_touch = touch_zones[0][0], touch_zones[-1][-1]
            if last_touch - first_touch < 4:
                continue
            end_index = min(len(session) - 1, j + max(2, span))
            end_value = slope * end_index + intercept
            out.append({
                "timeframe": "5m",
                "kind": kind,
                "direction": "bajista" if kind == "resistencia" else "alcista",
                "touches": touches,
                "score": round(float(move), 4),
                "points": [
                    {
                        "time": int(session.index[i].timestamp()),
                        "value": round(float(y1), 4),
                    },
                    {
                        "time": int(session.index[end_index].timestamp()),
                        "value": round(float(end_value), 4),
                    },
                ],
                "label": f"5m intradia {kind}",
                "scope": "intradia",
                "session_date": str(session.index[-1].tz_convert(NY).date()),
                "start_index": i,
                "end_index": end_index,
            })
    return out


def _touch_zones(touch_idx: np.ndarray) -> list[list[int]]:
    zones: list[list[int]] = []
    for idx in [int(item) for item in touch_idx]:
        if zones and idx == zones[-1][-1] + 1:
            zones[-1].append(idx)
        else:
            zones.append([idx])
    return zones


def _swing_points(values: np.ndarray, kind: str) -> list[tuple[int, float]]:
    pivots: list[tuple[int, float]] = []
    n = len(values)
    if n <= 14:
        return [(i, float(values[i])) for i in range(n)]
    for i in range(n):
        left = max(0, i - 1)
        right = min(n, i + 2)
        window = values[left:right]
        value = float(values[i])
        if kind == "resistencia":
            if value >= float(window.max()):
                pivots.append((i, value))
        elif value <= float(window.min()):
            pivots.append((i, value))
    return pivots


def _select_swing_lines(lines: list[dict]) -> list[dict]:
    selected: list[dict] = []
    ranked = sorted(lines, key=_swing_rank, reverse=True)
    for line in ranked:
        if sum(1 for item in selected if item["kind"] == line["kind"]) >= 2:
            continue
        if any(_line_overlaps(line, item) for item in selected):
            continue
        selected.append(line)
    return sorted(selected, key=lambda item: (item["points"][0]["time"], item["kind"]))


def _swing_rank(line: dict) -> tuple:
    p1, p2 = line["points"]
    duration = int(p2["time"]) - int(p1["time"])
    move = abs(float(p2["value"]) - float(p1["value"]))
    return (move, duration, int(line.get("touches", 0)))


def _line_overlaps(a: dict, b: dict) -> bool:
    if a["kind"] != b["kind"]:
        return False
    a1, a2 = a["points"]
    b1, b2 = b["points"]
    overlap = min(int(a2["time"]), int(b2["time"])) - max(int(a1["time"]), int(b1["time"]))
    shortest = min(int(a2["time"]) - int(a1["time"]), int(b2["time"]) - int(b1["time"]))
    if shortest <= 0:
        return False
    price_gap = abs(float(a1["value"]) - float(b1["value"])) + abs(float(a2["value"]) - float(b2["value"]))
    return overlap / shortest > 0.6 and price_gap < 1.0


def _confirmed_breakouts(bars: pd.DataFrame, day: date,
                         trendlines: list[dict]) -> list[dict]:
    """Circulos en los dias previos: ruptura de linea + vela de continuidad."""
    if bars is None or bars.empty or len(bars) < 3:
        return []
    local = bars.index.tz_convert(NY)
    previous = bars[local.date < day]
    if len(previous) < 3:
        return []

    out = []
    for line in trendlines:
        if line.get("kind") not in {"resistencia", "soporte", "corte"}:
            continue
        points = line.get("points") or []
        if len(points) < 2:
            continue
        for session_day, session in previous.groupby(previous.index.tz_convert(NY).date):
            if line.get("session_date") and line["session_date"] != str(session_day):
                continue
            if len(session) < 3:
                continue
            found = _first_confirmed_breakout_in_session(session, line, points)
            if found is not None:
                out.append(found)
                break
    return out


def _first_confirmed_breakout_in_session(session: pd.DataFrame, line: dict,
                                         points: list[dict]) -> dict | None:
    for i in range(1, len(session) - 1):
        prev = session.iloc[i - 1]
        current = session.iloc[i]
        confirm = session.iloc[i + 1]
        current_ts = int(session.index[i].timestamp())
        confirm_ts = int(session.index[i + 1].timestamp())
        prev_ts = int(session.index[i - 1].timestamp())
        prev_level = _line_value_at(points, prev_ts)
        current_level = _line_value_at(points, current_ts)
        confirm_level = _line_value_at(points, confirm_ts)
        if None in (prev_level, current_level, confirm_level):
            continue

        broke_up = (
            line["kind"] in {"resistencia", "corte"}
            and float(prev["close"]) <= prev_level
            and float(current["close"]) > current_level
        )
        confirms_up = (
            float(confirm["close"]) > confirm_level
            and float(confirm["close"]) > float(confirm["open"])
        )
        if broke_up and confirms_up:
            return _breakout_payload(
                line, current_ts, "CALL", float(current["close"]),
                current_level)

        broke_down = (
            line["kind"] in {"soporte", "corte"}
            and float(prev["close"]) >= prev_level
            and float(current["close"]) < current_level
        )
        confirms_down = (
            float(confirm["close"]) < confirm_level
            and float(confirm["close"]) < float(confirm["open"])
        )
        if broke_down and confirms_down:
            return _breakout_payload(
                line, current_ts, "PUT", float(current["close"]),
                current_level)
    return None


def _line_value_at(points: list[dict], ts: int) -> float | None:
    try:
        p1, p2 = points[0], points[-1]
        t1, t2 = int(p1["time"]), int(p2["time"])
        y1, y2 = float(p1["value"]), float(p2["value"])
    except (KeyError, TypeError, ValueError):
        return None
    if t2 == t1:
        return y2
    return y1 + (y2 - y1) * ((ts - t1) / (t2 - t1))


def _breakout_payload(line: dict, ts: int, direction: str,
                      close: float, level: float) -> dict:
    return {
        "time": ts,
        "direction": direction,
        "price": round(close, 4),
        "line_price": round(level, 4),
        "timeframe": line.get("timeframe"),
        "kind": line.get("kind"),
        "label": (
            f"Confirmacion {direction} {line.get('timeframe')} "
            f"{line.get('kind')}"
        ),
    }


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
