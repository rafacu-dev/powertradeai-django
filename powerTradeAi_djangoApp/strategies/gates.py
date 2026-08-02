"""Gates transversales que una señal academica debe superar antes de operar.

Los calculos de este modulo son deterministas. Cuando el corpus no publica un
umbral, el resultado queda identificado como ``IMPLEMENTACION`` o se bloquea con
un estado ``PENDING_*``; nunca se presenta como regla de Investep Academy.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
from django.conf import settings

from .base import NY, ScanContext, solo_rth


def _config() -> dict:
    return getattr(settings, "POWERTRADEAI", {})


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(NY).date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def event_gate(symbol: str, as_of: datetime) -> dict:
    """Valida cobertura y bloqueos del calendario configurado.

    ``EVENT_CALENDAR`` es una lista de diccionarios. Para earnings basta con
    ``{"type": "earnings", "symbol": "TSLA", "date": "2026-08-05"}``.
    Los eventos macro pueden usar ``symbol="*"`` y la misma fecha. La cobertura
    se declara con ``EVENT_CALENDAR_COVERAGE_UNTIL``; sin ella el estado es
    desconocido y la decision se bloquea.
    """
    cfg = _config()
    today = as_of.astimezone(NY).date()
    coverage_from = _as_date(cfg.get("EVENT_CALENDAR_COVERAGE_FROM"))
    coverage_until = _as_date(cfg.get("EVENT_CALENDAR_COVERAGE_UNTIL"))
    if (coverage_from is None or coverage_until is None
            or not coverage_from <= today <= coverage_until):
        return {
            "status": "UNKNOWN",
            "blocker": "PENDING_EVENT_CALENDAR",
            "coverage_from": (
                coverage_from.isoformat() if coverage_from else None),
            "coverage_until": (
                coverage_until.isoformat() if coverage_until else None),
            "provenance": "IMPLEMENTACION",
        }

    calendar = cfg.get("EVENT_CALENDAR", [])
    if not isinstance(calendar, list):
        return {
            "status": "UNKNOWN",
            "blocker": "PENDING_EVENT_CALENDAR",
            "reason": "calendar_malformed",
            "coverage_from": coverage_from.isoformat(),
            "coverage_until": coverage_until.isoformat(),
            "provenance": "IMPLEMENTACION",
        }

    hits = []
    sym = symbol.upper()
    for raw in calendar:
        if not isinstance(raw, dict):
            return {
                "status": "UNKNOWN",
                "blocker": "PENDING_EVENT_CALENDAR",
                "reason": "calendar_entry_malformed",
                "coverage_from": coverage_from.isoformat(),
                "coverage_until": coverage_until.isoformat(),
                "provenance": "IMPLEMENTACION",
            }
        event_symbol = str(raw.get("symbol", "*")).upper()
        event_date = _as_date(raw.get("date") or raw.get("starts_at"))
        kind = str(raw.get("type", "macro")).lower()
        if event_date is None or kind not in {"earnings", "macro"}:
            return {
                "status": "UNKNOWN",
                "blocker": "PENDING_EVENT_CALENDAR",
                "reason": "calendar_entry_malformed",
                "coverage_from": coverage_from.isoformat(),
                "coverage_until": coverage_until.isoformat(),
                "provenance": "IMPLEMENTACION",
            }
        if event_symbol not in {"*", sym}:
            continue
        days_until = (event_date - today).days
        blocked = 0 <= days_until <= 3 if kind == "earnings" else days_until == 0
        if blocked:
            hits.append({
                "type": kind,
                "symbol": event_symbol,
                "date": event_date.isoformat(),
                "confirmed": bool(raw.get("confirmed", False)),
                "source": raw.get("source"),
            })

    if hits:
        return {
            "status": "BLOCKED",
            "blocker": "NO_OPERAR_EVENTO",
            "events": hits,
            "coverage_from": coverage_from.isoformat(),
            "coverage_until": coverage_until.isoformat(),
            "provenance": "REGLA_ACADEMIA",
        }
    return {
        "status": "CLEAR",
        "events": [],
        "coverage_from": coverage_from.isoformat(),
        "coverage_until": coverage_until.isoformat(),
        "provenance": "IMPLEMENTACION",
    }


def _moving_average_levels(frame: pd.DataFrame, prefix: str) -> list[dict]:
    if frame is None or frame.empty or "close" not in frame:
        return []
    closes = frame["close"].astype(float)
    out = []
    for period in (20, 40, 100, 200):
        if len(closes) >= period:
            out.append({
                "name": f"MA{period}_{prefix}",
                "price": float(closes.iloc[-period:].mean()),
                "provenance": "REGLA_ACADEMIA",
            })
    return out


def _pivot_levels(frame: pd.DataFrame, prefix: str, window: int = 3) -> list[dict]:
    """Pivotes mecanicos, etiquetados como implementacion."""
    if frame is None or len(frame) < window * 2 + 1:
        return []
    highs = frame["high"].to_numpy(float)
    lows = frame["low"].to_numpy(float)
    levels = []
    for i in range(window, len(frame) - window):
        if highs[i] == np.max(highs[i - window:i + window + 1]):
            levels.append({
                "name": f"TECHO_{prefix}", "price": float(highs[i]),
                "provenance": "IMPLEMENTACION",
            })
        if lows[i] == np.min(lows[i - window:i + window + 1]):
            levels.append({
                "name": f"PISO_{prefix}", "price": float(lows[i]),
                "provenance": "IMPLEMENTACION",
            })
    return levels[-30:]


def _spot_model(symbol: str, target_pct: float) -> dict | None:
    models = _config().get("SPOT_PREMIUM_MODELS", {})
    if not isinstance(models, dict):
        return None
    model = models.get(symbol.upper()) or models.get("*")
    if not isinstance(model, dict):
        return None
    required_abs = model.get("required_move_abs_usd")
    required_pct = model.get("required_move_pct")
    if required_abs is None and required_pct is None:
        return None
    try:
        parsed_abs = float(required_abs) if required_abs is not None else None
        parsed_pct = float(required_pct) if required_pct is not None else None
        sample_size = int(model.get("sample_size", 0))
        model_target = float(model.get("target_premium_pct", target_pct))
    except (TypeError, ValueError, OverflowError):
        return None
    required_value = parsed_abs if parsed_abs is not None else parsed_pct
    if (required_value is None or not math.isfinite(required_value)
            or required_value <= 0 or sample_size < 0
            or not math.isfinite(model_target)):
        return None
    return {
        "required_move_abs_usd": parsed_abs,
        "required_move_pct": parsed_pct,
        "sample_size": sample_size,
        "target_premium_pct": model_target,
        "source": model.get("source", "configured_model"),
        "version": model.get("version", "unversioned"),
    }


def assess_terrain(
    ctx: ScanContext,
    direction: str,
    spot: float,
    *,
    target_premium_pct: float = 15.0,
) -> dict:
    """Compara primera barrera con el movimiento spot-prima requerido."""
    try:
        hourly = solo_rth(ctx.history("1h", days=90))
        daily = ctx.history("1d", days=400)
        today_15m = ctx.resample("15m")
        today_1h = ctx.resample("1h")
    except Exception as exc:
        return {
            "status": "UNKNOWN",
            "blocker": "PENDING_BARRIER_DATA",
            "error": f"{type(exc).__name__}: {exc}",
        }

    if today_1h is not None and not today_1h.empty:
        hourly_frames = [
            frame for frame in (hourly, today_1h)
            if frame is not None and not frame.empty
        ]
        hourly = pd.concat(hourly_frames).sort_index()
        hourly = hourly[~hourly.index.duplicated(keep="last")]

    levels = [
        *_moving_average_levels(hourly, "1H"),
        *_moving_average_levels(daily, "1D"),
        *_pivot_levels(today_15m, "15M", window=2),
        *_pivot_levels(hourly.tail(120), "1H"),
        *_pivot_levels(daily.tail(260), "1D"),
    ]
    if direction == "CALL":
        ahead = [level for level in levels if level["price"] > spot]
        barrier = min(ahead, key=lambda level: level["price"], default=None)
    else:
        ahead = [level for level in levels if level["price"] < spot]
        barrier = max(ahead, key=lambda level: level["price"], default=None)

    if barrier is None:
        return {
            "status": "UNKNOWN",
            "blocker": "PENDING_BARRIER_MAP",
            "levels_considered": len(levels),
        }

    distance = abs(float(barrier["price"]) - float(spot))
    distance_pct = distance / float(spot) * 100 if spot else None
    model = _spot_model(ctx.symbol, target_premium_pct)
    if model is None:
        return {
            "status": "UNKNOWN",
            "blocker": "PENDING_EMPIRICAL_MOVE_MODEL",
            "barrier": barrier,
            "distance_abs_usd": round(distance, 4),
            "distance_pct": round(distance_pct, 4) if distance_pct is not None else None,
        }

    if abs(model["target_premium_pct"] - float(target_premium_pct)) > 1e-9:
        return {
            "status": "UNKNOWN",
            "blocker": "PENDING_EMPIRICAL_MOVE_MODEL",
            "reason": "target_mismatch",
            "requested_target_premium_pct": float(target_premium_pct),
            "model_target_premium_pct": model["target_premium_pct"],
            "barrier": barrier,
            "distance_abs_usd": round(distance, 4),
        }

    min_samples = int(_config().get("MIN_SPOT_PREMIUM_SAMPLES", 20))
    if model["sample_size"] < min_samples:
        return {
            "status": "UNKNOWN",
            "blocker": "PENDING_EMPIRICAL_MOVE_MODEL",
            "reason": "muestra_insuficiente",
            "sample_size": model["sample_size"],
            "minimum_samples": min_samples,
            "barrier": barrier,
            "distance_abs_usd": round(distance, 4),
        }

    required = model["required_move_abs_usd"]
    if required is None:
        required = float(spot) * float(model["required_move_pct"]) / 100
    sufficient = distance >= required
    return {
        "status": "SUFFICIENT" if sufficient else "BLOCKED",
        "blocker": None if sufficient else "NO_OPERAR_SIN_TERRENO",
        "barrier": barrier,
        "distance_abs_usd": round(distance, 4),
        "distance_pct": round(distance_pct, 4) if distance_pct is not None else None,
        "required_move_abs_usd": round(required, 4),
        "model": model,
        "provenance": "IMPLEMENTACION",
    }


def validate_quote(
    quote,
    *,
    as_of: datetime,
    max_spread_pct: float = 5.0,
    allow_after_seconds: float | None = None,
) -> dict:
    """Valida bid/ask, mercado cruzado, spread y frescura de una quote."""
    if quote is None:
        return {"status": "BLOCKED", "blocker": "OPTION_QUOTE_MISSING"}
    bid = float(getattr(quote, "bid", 0) or 0)
    ask = float(getattr(quote, "ask", 0) or 0)
    if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask <= 0:
        return {"status": "BLOCKED", "blocker": "OPTION_QUOTE_EMPTY"}
    if ask < bid:
        return {"status": "BLOCKED", "blocker": "OPTION_QUOTE_CROSSED"}
    spread = (ask - bid) / ask * 100
    if spread > float(max_spread_pct):
        return {
            "status": "BLOCKED", "blocker": "OPTION_SPREAD_TOO_WIDE",
            "spread_pct": round(spread, 4),
        }

    quote_ts = getattr(quote, "ts", None)
    max_age = int(_config().get("MAX_OPTION_QUOTE_AGE_SECONDS", 30))
    age = None
    if quote_ts is not None:
        try:
            observed = pd.Timestamp(as_of)
            quoted = pd.Timestamp(quote_ts)
            if observed.tzinfo is None:
                observed = observed.tz_localize(NY)
            if quoted.tzinfo is None:
                quoted = quoted.tz_localize(NY)
            raw_age = (
                observed.tz_convert("UTC") - quoted.tz_convert("UTC")
            ).total_seconds()
            future_skew = float(
                allow_after_seconds
                if allow_after_seconds is not None
                else _config().get("MAX_OPTION_QUOTE_FUTURE_SKEW_SECONDS", 2)
            )
            future_skew = max(future_skew, 0)
            if raw_age < -future_skew:
                return {
                    "status": "BLOCKED",
                    "blocker": "OPTION_QUOTE_FROM_FUTURE",
                    "future_seconds": round(abs(raw_age), 3),
                    "maximum_skew_seconds": future_skew,
                }
            age = max(raw_age, 0)
            latency = max(-raw_age, 0)
        except (TypeError, ValueError, OverflowError):
            return {
                "status": "BLOCKED",
                "blocker": "OPTION_QUOTE_TIMESTAMP_INVALID",
            }
        if age > max_age:
            return {
                "status": "BLOCKED", "blocker": "OPTION_QUOTE_STALE",
                "age_seconds": round(age, 3), "maximum_age_seconds": max_age,
            }
    elif _config().get("REQUIRE_OPTION_QUOTE_TIMESTAMP", True):
        return {"status": "BLOCKED", "blocker": "OPTION_QUOTE_TIMESTAMP_MISSING"}

    return {
        "status": "VALID", "bid": bid, "ask": ask,
        "spread_pct": round(spread, 4),
        "age_seconds": round(age, 3) if age is not None else None,
        "execution_latency_seconds": (
            round(latency, 3) if quote_ts is not None else None),
    }
