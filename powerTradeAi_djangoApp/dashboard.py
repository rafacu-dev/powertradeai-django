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
