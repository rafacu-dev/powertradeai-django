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
from django.core.management import call_command
from django.db.models import Avg, Count, Q, Sum
from django.http import JsonResponse, QueryDict
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .models import Alert, Strategy

log = logging.getLogger(__name__)

REPLAY_DEFAULT_SYMBOLS = (
    "AMZN", "GOOGL", "TSLA", "AAPL", "NVDA", "MSFT", "QQQ", "SPY",
)

# Cuantos dias con actividad muestra la tira-calendario. No son dias naturales:
# son sesiones con al menos una alerta, asi que los findes y festivos no dejan
# huecos vacios.
DIAS_EN_TIRA = 30


def _dinero(valor):
    """Descompone un importe para que la plantilla lo pinte con el locale.

    El signo va fuera del simbolo ("-$47,60", no "$-47,60") y el valor absoluto
    lo formatea ``floatformat``, que respeta la coma decimal de es-es como el
    resto del dashboard. ``vacio`` distingue "todavia no vale nada" de "vale 0".
    """
    if valor is None:
        return {"vacio": True, "signo": "", "abs": Decimal("0.00"),
                "positivo": False, "negativo": False}
    return {
        "vacio": False,
        "signo": "+" if valor > 0 else ("-" if valor < 0 else ""),
        "abs": abs(valor),
        "positivo": valor > 0,
        "negativo": valor < 0,
    }


def _resumen_por_estrategia(qs):
    """Que ha dado cada regla, agrupado por simbolo.

    Solo aparecen las reglas que han disparado al menos una vez: se agrega
    sobre las alertas, no sobre el catalogo, asi que una regla sembrada pero
    nunca ejecutada no ensucia la lista.

    ``veces`` cuenta disparos; el neto y el promedio salen solo de las cerradas.
    Dividir el neto entre los disparos mezclaria operaciones sin resultado con
    las que ya lo tienen y hundiria el promedio de cualquier regla con posicion
    abierta.
    """
    filas = (
        qs.order_by()
        .values("symbol", "strategy__strategy_id")
        .annotate(
            veces=Count("id"),
            cerradas=Count("id", filter=Q(status=Alert.Status.CLOSED)),
            pendientes=Count("id", filter=Q(status=Alert.Status.PENDING)),
            neto=Sum("net_dollars", filter=Q(status=Alert.Status.CLOSED)),
        )
    )

    grupos = {}
    for fila in filas:
        simbolo = fila["symbol"]
        neto = fila["neto"]
        promedio = (neto / fila["cerradas"]).quantize(Decimal("0.01")) \
            if fila["cerradas"] else None
        grupo = grupos.setdefault(simbolo, {
            "simbolo": simbolo, "reglas": [], "veces": 0,
            "cerradas": 0, "neto_bruto": Decimal("0.00"),
        })
        grupo["reglas"].append({
            "regla": fila["strategy__strategy_id"],
            "veces": fila["veces"],
            "cerradas": fila["cerradas"],
            "pendientes": fila["pendientes"],
            "neto": _dinero(neto),
            "neto_orden": neto if neto is not None else Decimal("0.00"),
            "promedio": _dinero(promedio),
        })
        grupo["veces"] += fila["veces"]
        grupo["cerradas"] += fila["cerradas"]
        grupo["neto_bruto"] += neto or Decimal("0.00")

    for grupo in grupos.values():
        # Dentro de cada simbolo, la regla que mas ha dado primero.
        grupo["reglas"].sort(key=lambda r: r["neto_orden"], reverse=True)
        grupo["neto"] = _dinero(grupo["neto_bruto"] if grupo["cerradas"] else None)
    # Los simbolos, alfabeticos: la lista no debe bailar entre recargas.
    return sorted(grupos.values(), key=lambda g: g["simbolo"])


def _tira_de_dias(qs, filtros_base, dia_seleccionado):
    """Una columna por sesion, con el neto cerrado de ese dia.

    ``qs`` llega filtrado por todo MENOS la fecha: si se acotara por
    desde/hasta, pulsar un dia colapsaria la tira a ese unico dia y no habria
    manera de volver a los demas.

    El enlace de cada dia arrastra el resto de filtros vigentes. El del dia ya
    activo los arrastra SIN fecha, de forma que volver a pulsarlo deselecciona.
    """
    filas = (
        # El order_by() vacio es imprescindible: el queryset base ordena por
        # signal_ts, y Django mete las columnas del ORDER BY en el GROUP BY.
        # Sin limpiarlo saldria una fila por alerta en vez de una por dia.
        qs.order_by()
        .values("session_date")
        .annotate(
            neto=Sum("net_dollars", filter=Q(status=Alert.Status.CLOSED)),
            operaciones=Count("id"),
            pendientes=Count("id", filter=Q(status=Alert.Status.PENDING)),
        )
        .order_by("-session_date")[:DIAS_EN_TIRA]
    )

    dias = []
    for fila in filas:
        iso = fila["session_date"].isoformat()
        activo = iso == dia_seleccionado
        enlace = filtros_base.copy()
        if not activo:
            enlace["desde"] = iso
            enlace["hasta"] = iso
        dias.append({
            "fecha": fila["session_date"],
            # Un dia con alertas pero ninguna cerrada no vale 0: no vale nada
            # todavia. _dinero() lo marca ``vacio`` para no pintarlo plano.
            "neto": _dinero(fila["neto"]),
            "operaciones": fila["operaciones"],
            "pendientes": fila["pendientes"],
            "activo": activo,
            "url": "?" + enlace.urlencode(),
        })
    dias.reverse()  # cronologico: el dia mas reciente queda a la derecha
    return dias


@staff_member_required
@require_GET
def dashboard(request):
    params = request.GET
    source = params.get("source", Alert.Source.LIVE)
    evaluation_version = params.get("evaluation_version", "all")
    strategy_id = params.get("strategy", "")
    direction = params.get("direction", "")
    desde = params.get("desde", "")
    hasta = params.get("hasta", "")

    qs = Alert.objects.select_related("strategy").order_by("-session_date", "-signal_ts")

    if source and source != "all":
        qs = qs.filter(source=source)
    if evaluation_version != "all":
        qs = qs.filter(evaluation_version=evaluation_version)
    if strategy_id:
        qs = qs.filter(strategy__strategy_id=strategy_id)
    if direction:
        qs = qs.filter(direction=direction)

    # Un dia esta "elegido" solo cuando el rango es exactamente ese dia. Un
    # rango de varios dias deja la tira sin ninguna columna resaltada.
    dia_seleccionado = desde if desde and desde == hasta else None
    filtros_base = QueryDict(mutable=True)
    for clave, valor in (("source", source), ("strategy", strategy_id),
                         ("direction", direction)):
        if valor:
            filtros_base[clave] = valor
    if evaluation_version and evaluation_version != "all":
        filtros_base["evaluation_version"] = evaluation_version
    dias = _tira_de_dias(qs, filtros_base, dia_seleccionado)

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

    strategies = Strategy.objects.all().order_by("symbol", "strategy_id")

    return render(request, "powertradeai/dashboard.html", {
        "alerts": qs[:200],
        "stats": stats,
        "strategies": strategies,
        "dias": dias,
        "dia_seleccionado": dia_seleccionado,
        "por_estrategia": _resumen_por_estrategia(qs),
        "replay_strategies": Strategy.objects.filter(replay_enabled=True),
        "filters": {
            "source": source,
            "evaluation_version": evaluation_version,
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
    save = request.POST.get("save", "").lower() in {"1", "true", "yes", "on"}
    overwrite = request.POST.get("overwrite", "").lower() in {"1", "true", "yes", "on"}

    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"error": "Formato invalido (YYYY-MM-DD)"}, status=400)

    from .engine.replay import replay_day
    from .engine.session import is_trading_day

    if not is_trading_day(day):
        return JsonResponse({"error": f"{day} no es dia habil"}, status=400)

    try:
        result = replay_day(day, persist=save, overwrite=overwrite if save else False)
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
        "saved": save,
        "overwritten": overwrite if save else False,
        "alerts": alerts_data,
        "total": len(result.alerts),
        "closed": len(result.closed),
        "net": str(result.net_total),
        "skipped": [{"rule": s, "detail": d} for s, d in result.skipped],
        "errors": [{"rule": s, "detail": d} for s, d in result.errors],
    })


@staff_member_required
@require_POST
def seed_strategies_action(request):
    """Reaplica el catalogo operable desde la UI staff."""
    try:
        call_command("seed_strategies", verbosity=0)
    except Exception as exc:
        log.exception("seed_strategies desde dashboard fallo")
        return JsonResponse({"error": str(exc)}, status=500)

    live = list(Strategy.objects.filter(enabled=True)
                   .values_list("strategy_id", flat=True)
                   .order_by("strategy_id"))
    replay = list(Strategy.objects.filter(replay_enabled=True)
                  .values_list("strategy_id", flat=True)
                  .order_by("strategy_id"))
    return JsonResponse({
        "enabled": live,
        "replay_enabled": replay,
        "total": len(live),
    })


@staff_member_required
@require_http_methods(["GET", "POST"])
def strategies_control_view(request):
    """Control operacional: que reglas viven en live y cuales en replay."""
    if request.method == "POST":
        rows = list(Strategy.objects.all())
        live_ids = {
            int(value) for value in request.POST.getlist("live")
            if value.isdigit()
        }
        replay_ids = {
            int(value) for value in request.POST.getlist("replay")
            if value.isdigit()
        }
        for row in rows:
            live = row.pk in live_ids
            replay = row.pk in replay_ids
            changes = []
            if row.enabled != live:
                row.enabled = live
                changes.append("enabled")
            if row.replay_enabled != replay:
                row.replay_enabled = replay
                changes.append("replay_enabled")
            if changes:
                row.save(update_fields=[*changes, "updated_at"])
        if request.POST.get("return_to") == "dashboard":
            return redirect("powertradeai:dashboard")
        return redirect("powertradeai:strategies_control")

    strategies = Strategy.objects.all().order_by("symbol", "strategy_id")
    return render(request, "powertradeai/strategies.html", {
        "strategies": strategies,
        "live_count": Strategy.objects.filter(enabled=True).count(),
        "replay_count": Strategy.objects.filter(replay_enabled=True).count(),
    })


@staff_member_required
@require_GET
def replay_view(request):
    strategies = Strategy.objects.filter(replay_enabled=True).order_by(
        "symbol", "strategy_id")
    symbols = sorted(
        set(REPLAY_DEFAULT_SYMBOLS) | set(strategies.values_list("symbol", flat=True)))
    return render(request, "powertradeai/replay.html", {
        "symbols": symbols,
        "strategies": strategies,
        "default_symbols": REPLAY_DEFAULT_SYMBOLS,
    })


@staff_member_required
@require_GET
def scanner_view(request):
    return render(request, "powertradeai/scanner.html")


@staff_member_required
@require_GET
def replay_data(request):
    symbol = (request.GET.get("symbol") or "SPY").upper()
    date_str = request.GET.get("date", "")
    if not date_str:
        return JsonResponse({"error": "Fecha requerida"}, status=400)
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"error": "Formato invalido (YYYY-MM-DD)"}, status=400)

    strategy = request.GET.get("strategy", "").strip()
    strategy_ids = [strategy] if strategy else None

    from .engine.replay import replay_timeline
    from .engine.session import is_trading_day

    if not is_trading_day(day):
        return JsonResponse({"error": f"{day} no es dia habil"}, status=400)
    try:
        timeline = replay_timeline(day, symbol, strategy_ids=strategy_ids)
    except Exception as exc:
        log.exception("timeline replay fallo")
        return JsonResponse({"error": str(exc)}, status=500)

    return JsonResponse({
        "date": str(timeline.day),
        "symbol": timeline.symbol,
        "timeframe": timeline.timeframe,
        "replay_start_time": timeline.replay_start_time,
        "candles": timeline.candles,
        "trendlines": timeline.trendlines,
        "breakouts": timeline.breakouts,
        "events": timeline.events,
        "strategies": timeline.strategies,
        "errors": [{"strategy_id": s, "detail": d} for s, d in timeline.errors],
    })


@staff_member_required
@require_GET
def intraday_trendlines_view(request):
    strategies = Strategy.objects.all().order_by("symbol", "strategy_id")
    symbols = sorted(
        {"SPX"}
        | set(REPLAY_DEFAULT_SYMBOLS)
        | set(strategies.values_list("symbol", flat=True))
    )
    return render(request, "powertradeai/intraday_trendlines.html", {
        "symbols": symbols,
    })


@staff_member_required
@require_GET
def intraday_trendlines_data(request):
    symbol = (request.GET.get("symbol") or "SPX").upper()
    date_str = request.GET.get("date", "")
    if not date_str:
        return JsonResponse({"error": "Fecha requerida"}, status=400)
    try:
        day = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"error": "Formato invalido (YYYY-MM-DD)"}, status=400)

    from .engine.intraday_trendlines import intraday_trendline_timeline

    try:
        timeline = intraday_trendline_timeline(day, symbol)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except Exception as exc:
        log.exception("analisis de lineas intradia fallo")
        return JsonResponse({"error": str(exc)}, status=500)

    return JsonResponse({
        "date": str(timeline.day),
        "symbol": timeline.symbol,
        "timeframe": timeline.timeframe,
        "candles": timeline.candles,
        "setups": timeline.setups,
    })


# ── Agente ──────────────────────────────────────────────────────────

@staff_member_required
@require_GET
def agent_view(request):
    from django.db.models import Avg, Count, Q

    from .models import AgentAnalysis, AgentRun, Alert

    runs = AgentRun.objects.all()[:30]
    # Ultimo analisis por activo (para el panel de continuidad).
    latest = {}
    for a in AgentAnalysis.objects.all()[:200]:
        latest.setdefault(a.symbol, a)
    analyses = sorted(latest.values(), key=lambda a: a.created_at, reverse=True)

    # Expediente del agente: alertas suyas ya cerradas y puntuadas.
    agent_alerts = Alert.objects.filter(
        source=Alert.Source.AGENT, evaluation_version="investep_v2")
    closed = agent_alerts.filter(status=Alert.Status.CLOSED)
    agg = closed.aggregate(
        n=Count("id"),
        wins=Count("id", filter=Q(net_dollars__gt=0)),
        avg_pct=Avg("net_pct"),
    )
    n = agg["n"] or 0
    track = {
        "n": n,
        "pending": agent_alerts.filter(status=Alert.Status.PENDING).count(),
        "wins": agg["wins"] or 0,
        "win_rate": round((agg["wins"] or 0) / n * 100, 1) if n else None,
        "avg_pct": round(agg["avg_pct"], 2) if agg["avg_pct"] is not None else None,
    }
    recent_alerts = agent_alerts.select_related("strategy").order_by("-signal_ts")[:15]

    # Entrenamiento (agent_train): separado del expediente en vivo.
    train_qs = Alert.objects.filter(
        source="agent_train", status=Alert.Status.CLOSED,
        evaluation_version="investep_v2")
    tagg = train_qs.aggregate(n=Count("id"), wins=Count("id", filter=Q(net_dollars__gt=0)),
                              avg_pct=Avg("net_pct"))
    tn = tagg["n"] or 0
    train = {
        "n": tn, "wins": tagg["wins"] or 0,
        "win_rate": round((tagg["wins"] or 0) / tn * 100, 1) if tn else None,
        "avg_pct": round(tagg["avg_pct"], 2) if tagg["avg_pct"] is not None else None,
    }
    train_days = list(
        Alert.objects.filter(
            source="agent_train", evaluation_version="investep_v2")
        .values_list("session_date", flat=True).distinct().order_by("-session_date")[:10])

    return render(request, "powertradeai/agent.html", {
        "runs": runs,
        "analyses": analyses,
        "track": track,
        "recent_alerts": recent_alerts,
        "train": train,
        "train_days": train_days,
    })


@staff_member_required
@require_POST
def agent_train_launch(request):
    """Lanza un dia de entrenamiento en un hilo de fondo (no bloquea el HTTP;
    puede tardar minutos). Los resultados aparecen en el panel al refrescar."""
    import threading
    from datetime import datetime as _dt

    from django.db import close_old_connections

    from .agent.training import train_day
    from .engine.session import is_trading_day

    symbol = (request.POST.get("symbol") or "TSLA").upper()
    date_str = request.POST.get("date", "")
    try:
        step = max(int(request.POST.get("step", "5")), 1)
    except ValueError:
        step = 5
    try:
        day = _dt.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"error": "Fecha invalida (YYYY-MM-DD)"}, status=400)
    if not is_trading_day(day):
        return JsonResponse({"error": f"{day} no es dia habil"}, status=400)

    def _worker():
        try:
            train_day(symbol, day, step=step)
        except Exception:
            log.exception("entrenamiento en hilo fallo")
        finally:
            close_old_connections()

    threading.Thread(target=_worker, daemon=True).start()
    return JsonResponse({
        "launched": True, "symbol": symbol, "date": str(day), "step": step,
        "note": "Entrenamiento corriendo en segundo plano. Refresca en unos "
                "minutos para ver los resultados.",
    })


@staff_member_required
@require_POST
def agent_launch(request):
    """Lanza una corrida en un hilo de fondo y devuelve su id al instante.

    Sincrono no era viable: un ciclo del agente son varias llamadas al LLM y
    puede pasar del timeout de gunicorn. Ese SIGKILL se salta el ``finally``
    que cierra la corrida —de ahi la #1944 colgada en RUNNING— y ademas ocupaba
    uno de los dos hilos del servicio web mientras tanto."""
    from .agent.runner import lanzar_corrida

    symbols = [s.strip().upper() for s in request.POST.get("symbols", "").split(",")
               if s.strip()]
    goal = request.POST.get("goal", "").strip() or (
        "Revisa E01/E02 en la watchlist. Para cada candidato identifica la rama, "
        "consulta el manual y ejecuta validate_investep_setup. Crea una alerta "
        "solo con un decision_id VALID; informa WAIT/BLOCKED y sus blockers.")
    try:
        run = lanzar_corrida(goal, symbols=symbols, trigger="manual")
    except Exception as exc:
        log.exception("agente fallo desde el panel")
        return JsonResponse({"error": str(exc)}, status=500)
    return JsonResponse({
        "run_id": run.id, "status": run.status, "symbols": run.symbols,
        "note": f"Corrida #{run.id} lanzada en segundo plano. "
                f"Refresca en un par de minutos para ver el resultado.",
    })


# ── Convexidad ──────────────────────────────────────────────────────

@staff_member_required
@require_GET
def convexidad_view(request):
    """Que contrato se duplica con el menor movimiento, por simbolo.

    Sirve SIEMPRE lo cacheado y dispara el refresco en un hilo: un escaneo son
    ~30 cotizaciones por simbolo y el web solo tiene dos hilos de gunicorn."""
    from .agent import convexidad_scan

    datos = convexidad_scan.cacheado()
    if datos is None or request.GET.get("refrescar"):
        lanzado = convexidad_scan.refrescar_en_fondo()
    else:
        lanzado = False
    return render(request, "powertradeai/convexidad.html", {
        "datos": datos,
        "refrescando": lanzado or bool(datos is None),
        "universo": convexidad_scan.UNIVERSO,
    })


@staff_member_required
@require_GET
def convexidad_data(request):
    from .agent import convexidad_scan

    datos = convexidad_scan.cacheado()
    if datos is None:
        convexidad_scan.refrescar_en_fondo()
        return JsonResponse({"listo": False,
                             "nota": "calculando, vuelve en unos segundos"})
    return JsonResponse({"listo": True, **datos})


# ── Chart view ──────────────────────────────────────────────────────

@staff_member_required
@require_GET
def chart_view(request):
    return render(request, "powertradeai/chart.html")


@staff_member_required
@require_POST
def chart_chat(request):
    """Un turno de chat con el agente sobre el ticker del grafico."""
    import json as _json

    from .agent.runner import chat_agent

    try:
        payload = _json.loads(request.body or "{}")
    except _json.JSONDecodeError:
        payload = {}
    symbol = (payload.get("symbol") or "SPY").upper()
    message = (payload.get("message") or "").strip()
    history = payload.get("history") or []
    if not message:
        return JsonResponse({"error": "mensaje vacio"}, status=400)
    try:
        run, reply = chat_agent(symbol, message, history=history)
    except Exception as exc:
        log.exception("chat del agente fallo")
        return JsonResponse({"error": str(exc)}, status=500)
    return JsonResponse({
        "reply": reply, "run_id": run.id, "status": run.status,
        "alerts_created": run.alerts_created,
    })


@staff_member_required
@require_GET
def chart_price(request):
    """Ultimo precio del ticker (1 sola llamada, para el poll rapido)."""
    from django.conf import settings
    from .data.alpaca_provider import AlpacaProvider

    symbol = request.GET.get("symbol", "SPY").upper()
    cfg = getattr(settings, "POWERTRADEAI", {})
    provider = AlpacaProvider(
        api_key=cfg.get("ALPACA_API_KEY"),
        api_secret=cfg.get("ALPACA_API_SECRET"),
        feed=cfg.get("ALPACA_FEED", "iex"),
    )
    try:
        price = float(provider.latest_price(symbol))
    except Exception as exc:
        return JsonResponse({"error": str(exc)}, status=502)
    return JsonResponse({"symbol": symbol, "price": round(price, 2)})


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

    MA_PERIODS = [20, 40, 100, 200]

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

    # VWAP de sesion: se reinicia cada dia. Precio tipico ponderado por volumen,
    # acumulado desde la apertura de cada jornada.
    vwap = []
    if "volume" in bars_15m.columns and not bars_15m.empty:
        tp = (bars_15m["high"] + bars_15m["low"] + bars_15m["close"]) / 3
        vol = bars_15m["volume"]
        day = bars_15m.index.tz_convert(NY).date
        cum_pv = (tp * vol).groupby(day).cumsum()
        cum_v = vol.groupby(day).cumsum()
        vw = cum_pv / cum_v.replace(0, np.nan)
        for ts in bars_15m.index:
            t = int(ts.timestamp())
            if t < display_ts or pd.isna(vw[ts]):
                continue
            vwap.append({"time": t, "value": round(float(vw[ts]), 2)})

    return JsonResponse({
        "symbol": symbol,
        "candles": candles,
        "ma_curves": ma_curves,
        "htf_lines": htf_lines,
        "bollinger": bb,
        "vwap": vwap,
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

        # Estado de la APERTURA (la senal real). PENDIENTE en premarket.
        if today_open is None:
            status = "PENDIENTE"
        elif today_open > upper:
            status = "FUERA_ARRIBA"
        elif today_open < lower:
            status = "FUERA_ABAJO"
        else:
            status = "DENTRO"

        # Estado del PRECIO mostrado (apertura si abrio; si no, precio en vivo
        # de premarket). Es lo que dispara el puntico y la prioridad.
        z = (price - mid) / std if std else 0.0
        if price > upper:
            price_status = "ARRIBA"     # sobre la banda superior -> rojo
        elif price < lower:
            price_status = "ABAJO"      # bajo la banda inferior  -> verde
        else:
            price_status = "DENTRO"

        # Contra-tendencia: solo cuenta con tendencia horaria CLARA.
        outside = price_status in ("ARRIBA", "ABAJO")
        counter_trend = outside and (
            (trend == "alcista" and price_status == "ABAJO") or
            (trend == "bajista" and price_status == "ARRIBA")
        )
        # Score: distancia a la media (|z|), x2 si es contra-tendencia clara.
        weight = 2.0 if counter_trend else 1.0
        score = abs(z) * weight if outside else 0.0

        rows.append({
            "symbol": symbol,
            "status": status,
            "price": round(price, 2),
            "is_open": is_open,
            "price_status": price_status,
            "open": round(today_open, 2) if today_open is not None else None,
            "lower": round(lower, 2),
            "middle": round(mid, 2),
            "upper": round(upper, 2),
            "prev_close": round(float(closes.iloc[-1]), 2),
            "z": round(z, 2),
            "ma20": round(ma_fast, 2),
            "ma40": round(ma_slow, 2),
            "trend": trend,
            "counter_trend": counter_trend,
            "score": round(score, 2),
        })

    # Primero los que tienen el precio FUERA de banda; mientras mas lejos de
    # la media (mayor score), mas arriba. Errores/sin datos al final.
    def sort_key(r):
        outside = r.get("price_status") in ("ARRIBA", "ABAJO")
        valid = "price_status" in r
        return (0 if valid else 1, 0 if outside else 1,
                -(r.get("score") or 0), -abs(r.get("z") or 0))

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
