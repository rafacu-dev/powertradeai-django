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

    return JsonResponse({
        "symbol": symbol,
        "candles": candles,
        "ma_curves": ma_curves,
        "htf_lines": htf_lines,
    })
