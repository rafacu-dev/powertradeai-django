"""Dashboard standalone de PowerTradeAI.

Usa la sesion de Django admin para autenticar: si no estas logueado te manda
al login de admin y luego vuelve aqui. No depende del admin site — es una
vista independiente que comparte solo la autenticacion.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from decimal import Decimal

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Avg, Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

import json

from .models import Alert, Strategy

log = logging.getLogger(__name__)


@staff_member_required
@require_GET
def dashboard(request):
    params = request.GET
    source = params.get("source", "all")
    strategy_id = params.get("strategy", "")
    direction = params.get("direction", "")
    desde = params.get("desde", "")
    hasta = params.get("hasta", "")

    qs = Alert.objects.select_related("strategy").order_by("-session_date", "-signal_ts")

    if source and source != "all":
        qs = qs.filter(source=source)
    if strategy_id:
        qs = qs.filter(strategy__strategy_id=strategy_id)
    if direction:
        qs = qs.filter(direction=direction)
    if desde:
        qs = qs.filter(session_date__gte=desde)
    if hasta:
        qs = qs.filter(session_date__lte=hasta)

    closed = qs.filter(status=Alert.Status.CLOSED)
    stats = closed.aggregate(
        total=Count("id"),
        winners=Count("id", filter=Q(net_dollars__gt=0)),
        losers=Count("id", filter=Q(net_dollars__lte=0)),
        net=Sum("net_dollars"),
        avg_net=Avg("net_dollars"),
        avg_pct=Avg("net_pct"),
    )
    stats["net"] = stats["net"] or Decimal("0.00")
    stats["pending"] = qs.filter(status=Alert.Status.PENDING).count()
    total_closed = stats["total"] or 0
    stats["win_rate"] = (
        round(stats["winners"] / total_closed * 100, 1)
        if total_closed else None
    )

    strategies = Strategy.objects.values_list("strategy_id", flat=True).order_by("strategy_id")

    return render(request, "powertradeai/dashboard.html", {
        "alerts": qs[:200],
        "stats": stats,
        "strategies": strategies,
        "filters": {
            "source": source,
            "strategy": strategy_id,
            "direction": direction,
            "desde": desde,
            "hasta": hasta,
        },
    })


@staff_member_required
@require_POST
def replay_action(request):
    date_str = request.POST.get("date", "")
    if not date_str:
        return JsonResponse({"error": "Fecha requerida"}, status=400)

    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"error": "Formato invalido (YYYY-MM-DD)"}, status=400)

    from .engine.replay import replay_day
    from .engine.session import is_trading_day

    if not is_trading_day(day):
        return JsonResponse({"error": f"{day} no es dia habil"}, status=400)

    try:
        result = replay_day(day, overwrite=True)
    except Exception as exc:
        log.exception("replay desde dashboard fallo")
        return JsonResponse({"error": str(exc)}, status=500)

    alerts_data = []
    for a in result.alerts:
        alerts_data.append({
            "strategy": a.strategy.strategy_id,
            "direction": a.direction,
            "strike": str(a.strike),
            "entry": str(a.entry_premium),
            "exit": str(a.exit_premium) if a.exit_premium is not None else None,
            "reason": a.exit_reason,
            "net": str(a.net_dollars) if a.net_dollars is not None else None,
            "pct": str(a.net_pct) if a.net_pct is not None else None,
            "status": a.status,
        })

    return JsonResponse({
        "day": str(day),
        "alerts": alerts_data,
        "total": len(result.alerts),
        "closed": len(result.closed),
        "net": str(result.net_total),
        "errors": [{"rule": s, "detail": d} for s, d in result.errors],
    })


# ── Chart view ──────────────────────────────────────────────────────

@staff_member_required
@require_GET
def chart_view(request):
    return render(request, "powertradeai/chart.html")


@staff_member_required
@require_GET
def chart_data(request):
    """Return 15m candles + MA values for all timeframes."""
    import numpy as np
    import pandas as pd

    symbol = request.GET.get("symbol", "SPY").upper()
    days_back = min(int(request.GET.get("days", "10")), 60)

    from django.conf import settings
    from .data.alpaca_provider import AlpacaProvider

    cfg = getattr(settings, "POWERTRADEAI", {})
    provider = AlpacaProvider(
        api_key=cfg.get("ALPACA_API_KEY"),
        api_secret=cfg.get("ALPACA_API_SECRET"),
        feed=cfg.get("ALPACA_FEED", "iex"),
    )

    end = datetime.now().date()
    ma_lookback = max(days_back + 5, 25)
    htf_start = end - timedelta(days=400)

    MA_PERIODS = [9, 20, 50, 100, 200]

    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")

    def rth_filter(df):
        """Keep only Regular Trading Hours bars (9:30–16:00 ET)."""
        if df.empty:
            return df
        ny_idx = df.index.tz_convert(NY)
        mask = (ny_idx.time >= datetime(2000, 1, 1, 9, 30).time()) & \
               (ny_idx.time < datetime(2000, 1, 1, 16, 0).time())
        return df[mask]

    bars_15m = rth_filter(provider.bars(symbol, end - timedelta(days=ma_lookback), end, "15m"))
    bars_1h = rth_filter(provider.bars(symbol, htf_start, end, "1h"))
    bars_1d = provider.bars(symbol, htf_start, end, "1d")
    bars_1w = provider.bars(symbol, htf_start, end, "1w")

    def to_candles(df):
        records = []
        for ts, row in df.iterrows():
            records.append({
                "time": int(ts.timestamp()),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            })
        return records

    def compute_ma_series(df, period):
        closes = df["close"].rolling(period).mean()
        series = []
        for ts, val in closes.items():
            if pd.notna(val):
                series.append({"time": int(ts.timestamp()), "value": round(float(val), 2)})
        return series

    def current_ma(df, period):
        closes = df["close"]
        if len(closes) < period:
            return None
        return round(float(closes.iloc[-period:].mean()), 2)

    display_start = end - timedelta(days=days_back + 5)
    display_ts = int(datetime.combine(display_start, datetime.min.time()).timestamp())

    candles = [c for c in to_candles(bars_15m) if c["time"] >= display_ts]

    ma_curves = {}
    for p in MA_PERIODS:
        all_pts = compute_ma_series(bars_15m, p)
        ma_curves[str(p)] = [pt for pt in all_pts if pt["time"] >= display_ts]

    htf_lines = {}
    for tf_name, df in [("1h", bars_1h), ("1d", bars_1d), ("1w", bars_1w)]:
        lines = {}
        for p in MA_PERIODS:
            val = current_ma(df, p)
            if val is not None:
                lines[str(p)] = val
        htf_lines[tf_name] = lines

    bb_period, bb_std = 20, 2
    bb = {"upper": [], "middle": [], "lower": []}
    if len(bars_15m) >= bb_period:
        closes = bars_15m["close"]
        mid = closes.rolling(bb_period).mean()
        std = closes.rolling(bb_period).std()
        for ts in bars_15m.index:
            t = int(ts.timestamp())
            if t < display_ts or pd.isna(mid[ts]):
                continue
            m = round(float(mid[ts]), 2)
            s = round(float(std[ts]) * bb_std, 2)
            bb["upper"].append({"time": t, "value": m + s})
            bb["middle"].append({"time": t, "value": m})
            bb["lower"].append({"time": t, "value": m - s})

    return JsonResponse({
        "symbol": symbol,
        "candles": candles,
        "ma_curves": ma_curves,
        "htf_lines": htf_lines,
        "bollinger": bb,
    })


# ── Scanner de apertura: Bollinger 15m + tendencia MA20/MA40 ────────

# 10 mayores del NASDAQ (peso en el Nasdaq-100, mediados de 2026) + indices.
SCANNER_WATCHLIST = [
    "NVDA", "AAPL", "MSFT", "AMZN", "AVGO",
    "META", "GOOGL", "TSLA", "COST", "NFLX",
    # Indices via ETF: Nasdaq, S&P 500, Dow Jones.
    "QQQ", "SPY", "DIA",
]

BB_PERIOD = 20      # Bollinger sobre velas de 15m (tambien es la MA rapida).
MA_SLOW = 40        # Media lenta para leer la tendencia.
BB_K = 2            # Desviaciones estandar de las bandas.


@staff_member_required
@require_GET
def scanner_data(request):
    """Bollinger 15m (cerrado hasta ayer) vs apertura de hoy, ponderado por
    tendencia MA20/MA40 en 1 HORA.

    Las bandas de Bollinger se calculan con velas de 15m RTH ya cerradas
    (hasta el cierre de ayer); la tendencia se lee con MA20/MA40 sobre
    velas de 1h RTH tambien cerradas. Todo queda fijo antes de la apertura;
    no se usa premarket. A las 9:30 comparamos la apertura contra las
    bandas de 15m.

    Ponderacion: pesa mas la apertura que va EN CONTRA de la tendencia
    horaria.
      - Tendencia alcista (MA20 > MA40 en 1h): pesa mas quien abre por
        DEBAJO de la banda inferior (retroceso contra-tendencia).
      - Tendencia bajista (MA40 > MA20 en 1h): pesa mas quien abre por
        ENCIMA de la banda superior.
    """
    from zoneinfo import ZoneInfo

    from django.conf import settings
    from .data.alpaca_provider import AlpacaProvider
    from .data.base import MarketDataError

    NY = ZoneInfo("America/New_York")
    cfg = getattr(settings, "POWERTRADEAI", {})
    provider = AlpacaProvider(
        api_key=cfg.get("ALPACA_API_KEY"),
        api_secret=cfg.get("ALPACA_API_SECRET"),
        feed=cfg.get("ALPACA_FEED", "iex"),
    )

    open_lo = datetime(2000, 1, 1, 9, 30).time()
    open_hi = datetime(2000, 1, 1, 16, 0).time()
    today = datetime.now(NY).date()
    # ~15 dias cubre >40 velas RTH de 15m; ~40 dias cubre >40 velas RTH de 1h.
    start_15m = today - timedelta(days=15)
    start_1h = today - timedelta(days=40)

    def rth_closed(df):
        """Velas RTH (9:30-16:00) cerradas hasta ayer (excluye hoy)."""
        if df.empty:
            return df
        ny = df.index.tz_convert(NY)
        rth = df[(ny.time >= open_lo) & (ny.time < open_hi)]
        if rth.empty:
            return rth
        return rth[rth.index.tz_convert(NY).date != today]

    rows = []
    for symbol in SCANNER_WATCHLIST:
        try:
            bars = provider.bars(symbol, start_15m, today, "15m")
            bars_1h = provider.bars(symbol, start_1h, today, "1h")
        except MarketDataError as exc:
            rows.append({"symbol": symbol, "status": "ERROR", "detail": str(exc)})
            continue
        if bars.empty:
            rows.append({"symbol": symbol, "status": "SIN_DATOS"})
            continue

        ny_idx = bars.index.tz_convert(NY)
        rth = bars[(ny_idx.time >= open_lo) & (ny_idx.time < open_hi)]
        if rth.empty:
            rows.append({"symbol": symbol, "status": "SIN_DATOS"})
            continue

        rth_dates = rth.index.tz_convert(NY).date
        today_mask = rth_dates == today
        hist = rth[~today_mask]              # velas 15m RTH cerradas hasta ayer

        # Tendencia en 1h: MA20 vs MA40 sobre velas horarias RTH cerradas.
        h1 = rth_closed(bars_1h)
        if len(hist) < BB_PERIOD or len(h1) < MA_SLOW:
            rows.append({"symbol": symbol, "status": "SIN_DATOS"})
            continue

        # Bollinger sobre 15m.
        closes = hist["close"]
        bb_win = closes.iloc[-BB_PERIOD:]
        mid = float(bb_win.mean())
        std = float(bb_win.std(ddof=0))     # poblacional, como TradingView
        upper = mid + BB_K * std
        lower = mid - BB_K * std

        # Medias de tendencia sobre 1h.
        h1_closes = h1["close"]
        ma_fast = float(h1_closes.iloc[-BB_PERIOD:].mean())   # MA20 en 1h
        ma_slow = float(h1_closes.iloc[-MA_SLOW:].mean())     # MA40 en 1h

        # Tendencia por cruce de medias (con banda muerta de 0.05%).
        spread = (ma_fast - ma_slow) / ma_slow if ma_slow else 0.0
        if spread > 0.0005:
            trend = "alcista"
        elif spread < -0.0005:
            trend = "bajista"
        else:
            trend = "plano"

        today_open = None
        if today_mask.any():
            today_open = float(rth[today_mask]["open"].iloc[0])

        # Precio a mostrar: la apertura si ya abrio; si no (premarket), el
        # ultimo precio en vivo para ir observando (fallback: ultimo cierre).
        if today_open is not None:
            price = today_open
            is_open = True
        else:
            is_open = False
            try:
                price = float(provider.latest_price(symbol))
            except Exception:
                price = float(closes.iloc[-1])

        counter_trend = False
        score = 0.0
        if today_open is None:
            status = "PENDIENTE"
            z = None
        else:
            z = (today_open - mid) / std if std else 0.0
            if today_open > upper:
                status = "FUERA_ARRIBA"
            elif today_open < lower:
                status = "FUERA_ABAJO"
            else:
                status = "DENTRO"

            outside = status in ("FUERA_ARRIBA", "FUERA_ABAJO")
            if outside:
                counter_trend = (
                    (trend == "alcista" and status == "FUERA_ABAJO") or
                    (trend == "bajista" and status == "FUERA_ARRIBA")
                )
                # Contra-tendencia pesa el doble que a favor.
                weight = 2.0 if counter_trend else 1.0
                score = abs(z) * weight

        rows.append({
            "symbol": symbol,
            "status": status,
            "price": round(price, 2),
            "is_open": is_open,
            "open": round(today_open, 2) if today_open is not None else None,
            "lower": round(lower, 2),
            "middle": round(mid, 2),
            "upper": round(upper, 2),
            "prev_close": round(float(closes.iloc[-1]), 2),
            "z": round(z, 2) if z is not None else None,
            "ma20": round(ma_fast, 2),
            "ma40": round(ma_slow, 2),
            "trend": trend,
            "counter_trend": counter_trend,
            "score": round(score, 2),
        })

    # Mayor score primero (contra-tendencia sube arriba); pendientes al final.
    def sort_key(r):
        has_score = r.get("status") in ("FUERA_ARRIBA", "FUERA_ABAJO", "DENTRO")
        return (0 if has_score else 1, -(r.get("score") or 0), -abs(r.get("z") or 0))

    rows.sort(key=sort_key)

    return JsonResponse({
        "date": str(today),
        "bb_timeframe": "15m",
        "trend_timeframe": "1h",
        "bb_period": BB_PERIOD,
        "ma_slow": MA_SLOW,
        "k": BB_K,
        "rows": rows,
    })
