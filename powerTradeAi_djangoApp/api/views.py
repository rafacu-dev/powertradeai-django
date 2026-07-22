"""Endpoints de lectura y replay on-demand."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from rest_framework import serializers as drf_serializers, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ..auth import ApiKeyAuthentication
from ..models import (
    AgentAnalysis, AgentNote, AgentRun, AgentTrigger, Alert, ScanRun, Strategy,
)
from .serializers import (
    AgentAnalysisSerializer,
    AgentNoteSerializer,
    AgentRunListSerializer,
    AgentRunSerializer,
    AgentTriggerSerializer,
    AlertSerializer,
    ScanRunSerializer,
    StrategyPerformanceSerializer,
    StrategySerializer,
)


class ApiKeyViewSet(viewsets.ReadOnlyModelViewSet):
    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsAuthenticated]


class AlertViewSet(ApiKeyViewSet):
    """Alertas, con filtros por estrategia, simbolo, estado y fechas.

    GET /alerts/?status=pending
    GET /alerts/?strategy=SPY_ORB15_0950&desde=2026-07-01&hasta=2026-07-19
    """

    serializer_class = AlertSerializer

    def get_queryset(self):
        qs = Alert.objects.select_related("strategy")
        params = self.request.query_params

        # Por defecto solo lo que ocurrio de verdad. Las reconstrucciones se
        # piden explicitamente con ?source=replay o ?source=all.
        source = params.get("source", Alert.Source.LIVE)
        if source != "all":
            qs = qs.filter(source=source)

        if (status := params.get("status")):
            qs = qs.filter(status=status)
        if (strategy := params.get("strategy")):
            qs = qs.filter(strategy__strategy_id=strategy)
        if (symbol := params.get("symbol")):
            qs = qs.filter(symbol=symbol.upper())
        if (direction := params.get("direction")):
            qs = qs.filter(direction=direction.upper())
        if (desde := params.get("desde")):
            qs = qs.filter(session_date__gte=desde)
        if (hasta := params.get("hasta")):
            qs = qs.filter(session_date__lte=hasta)
        return qs

    @action(detail=False, methods=["get"])
    def pending(self, request):
        """Atajo para lo que sigue vivo ahora mismo."""
        qs = self.get_queryset().filter(status=Alert.Status.PENDING)
        return Response(self.get_serializer(qs, many=True).data)


class StrategyViewSet(ApiKeyViewSet):
    serializer_class = StrategySerializer
    queryset = Strategy.objects.all()
    lookup_field = "strategy_id"

    @action(detail=False, methods=["get"])
    def performance(self, request):
        """Resumen por regla. Solo agrega alertas CERRADAS."""
        params = request.query_params
        # NUNCA se agregan live y replay juntos: la media resultante no
        # significaria nada. Se elige una fuente, y por defecto es la real.
        source = params.get("source", Alert.Source.LIVE)
        if source == "all":
            raise ValidationError(
                "source=all no se admite aqui: mezclar alertas en vivo con "
                "reconstrucciones produce un P&L sin significado. Pide "
                "source=live o source=replay.")
        source_filter = Q(alerts__source=source)

        closed_filter = source_filter & Q(alerts__status=Alert.Status.CLOSED)
        if (desde := params.get("desde")):
            closed_filter &= Q(alerts__session_date__gte=desde)
        if (hasta := params.get("hasta")):
            closed_filter &= Q(alerts__session_date__lte=hasta)

        rows = Strategy.objects.annotate(
            alertas_totales=Count("alerts", filter=source_filter, distinct=True),
            alertas_pendientes=Count(
                "alerts",
                filter=source_filter & Q(alerts__status=Alert.Status.PENDING),
                distinct=True),
            alertas_cerradas=Count("alerts", filter=closed_filter, distinct=True),
            ganadoras=Count(
                "alerts", filter=closed_filter & Q(alerts__net_dollars__gt=0),
                distinct=True),
            perdedoras=Count(
                "alerts", filter=closed_filter & Q(alerts__net_dollars__lte=0),
                distinct=True),
            neto_total=Sum("alerts__net_dollars", filter=closed_filter),
            neto_medio=Avg("alerts__net_dollars", filter=closed_filter),
            pct_medio=Avg("alerts__net_pct", filter=closed_filter),
        )

        payload = []
        for row in rows:
            cerradas = row.alertas_cerradas or 0
            payload.append({
                "source": source,
                "strategy_id": row.strategy_id,
                "name": row.name,
                "symbol": row.symbol,
                "alertas_totales": row.alertas_totales or 0,
                "alertas_pendientes": row.alertas_pendientes or 0,
                "alertas_cerradas": cerradas,
                "ganadoras": row.ganadoras or 0,
                "perdedoras": row.perdedoras or 0,
                "neto_total": row.neto_total or Decimal("0.00"),
                "neto_medio": row.neto_medio,
                "pct_medio": row.pct_medio,
                # Sin muestra no hay win rate: null, no 0%.
                "win_rate": (
                    Decimal(row.ganadoras) / Decimal(cerradas) * 100
                ).quantize(Decimal("0.01")) if cerradas else None,
            })
        return Response(StrategyPerformanceSerializer(payload, many=True).data)


class ScanRunViewSet(ApiKeyViewSet):
    """Salud del worker: distingue 'no hubo senal' de 'no estaba corriendo'."""

    serializer_class = ScanRunSerializer
    queryset = ScanRun.objects.all()[:200]


# ── Auditoria del agente (solo lectura, via API key) ────────────────

class AgentRunViewSet(ApiKeyViewSet):
    """Corridas del agente. El listado es ligero; el DETALLE trae el
    transcript completo (razonamiento + cada skill con args y resultado).

    GET /api/powertradeai/agent-runs/?symbol=TSLA&status=done&desde=2026-07-01
    GET /api/powertradeai/agent-runs/<id>/
    """

    def get_serializer_class(self):
        return (AgentRunSerializer if self.action == "retrieve"
                else AgentRunListSerializer)

    def get_queryset(self):
        qs = AgentRun.objects.all()
        p = self.request.query_params
        if (status := p.get("status")):
            qs = qs.filter(status=status)
        if (trigger := p.get("trigger")):
            qs = qs.filter(trigger=trigger)
        if (symbol := p.get("symbol")):
            # Portable en SQLite y Postgres: la pertenencia al JSON se evalua
            # en Python y luego se filtra por id (mantiene el queryset).
            sym = symbol.upper()
            ids = [r.id for r in qs.only("id", "symbols")
                   if sym in (r.symbols or [])]
            qs = qs.filter(id__in=ids)
        if (desde := p.get("desde")):
            qs = qs.filter(started_at__date__gte=desde)
        if (hasta := p.get("hasta")):
            qs = qs.filter(started_at__date__lte=hasta)
        return qs


class AgentAnalysisViewSet(ApiKeyViewSet):
    """Analisis del agente por activo (append-only).

    GET /api/powertradeai/agent-analyses/?symbol=TSLA
    """

    serializer_class = AgentAnalysisSerializer

    def get_queryset(self):
        qs = AgentAnalysis.objects.all()
        if (symbol := self.request.query_params.get("symbol")):
            qs = qs.filter(symbol=symbol.upper())
        return qs


class AgentNoteViewSet(ApiKeyViewSet):
    """Cuaderno de notas del agente.

    GET /api/powertradeai/agent-notes/?topic=TSLA
    """

    serializer_class = AgentNoteSerializer

    def get_queryset(self):
        qs = AgentNote.objects.all()
        if (topic := self.request.query_params.get("topic")):
            qs = qs.filter(topic=topic)
        return qs


class AgentTriggerViewSet(ApiKeyViewSet):
    """Niveles de vigilancia que fijo el agente.

    GET /api/powertradeai/agent-triggers/?symbol=TSLA&active=true
    """

    serializer_class = AgentTriggerSerializer

    def get_queryset(self):
        qs = AgentTrigger.objects.all()
        p = self.request.query_params
        if (symbol := p.get("symbol")):
            qs = qs.filter(symbol=symbol.upper())
        if (active := p.get("active")):
            qs = qs.filter(active=active.lower() in ("1", "true", "yes"))
        return qs


class ReplayView(APIView):
    """Replay on-demand via HTTP.

    POST /api/powertradeai/replay/
    {
        "desde": "2026-07-14",
        "hasta": "2026-07-18",
        "strategy": ["SPY_ORB15_BASE"],   // opcional
        "save": false                      // opcional, default false
    }

    Corre el motor de replay contra datos historicos y devuelve los resultados.
    Con save=false (default) NO persiste nada en la BD — solo calcula y responde.
    Con save=true guarda como source=replay (equivale al comando replay_range).
    """

    authentication_classes = [ApiKeyAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request):
        desde = request.data.get("desde")
        hasta = request.data.get("hasta", desde)
        if not desde:
            raise ValidationError({"desde": "requerido (YYYY-MM-DD)"})

        try:
            start = datetime.strptime(desde, "%Y-%m-%d").date()
            end = datetime.strptime(hasta, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            raise ValidationError("desde/hasta deben tener formato YYYY-MM-DD")

        if start > end:
            raise ValidationError("desde no puede ser posterior a hasta")
        if (end - start).days > 30:
            raise ValidationError("rango maximo: 30 dias")

        strategy_ids = request.data.get("strategy")
        if isinstance(strategy_ids, str):
            strategy_ids = [strategy_ids]
        save = request.data.get("save", True)

        from ..engine.replay import replay_day
        from ..engine.session import is_trading_day

        days = [
            start + timedelta(days=i)
            for i in range((end - start).days + 1)
            if is_trading_day(start + timedelta(days=i))
        ]
        if not days:
            return Response({"days": [], "summary": {
                "sesiones": 0, "alertas": 0, "neto": "0.00",
            }})

        all_days = []
        total_alerts = 0
        total_closed = 0
        net = Decimal("0.00")

        for day in days:
            try:
                result = replay_day(
                    day,
                    strategy_ids=strategy_ids,
                    overwrite=save,
                )
            except Exception as exc:
                all_days.append({
                    "date": str(day), "error": str(exc), "alerts": [],
                })
                continue

            day_alerts = []
            for alert in result.alerts:
                entry = {
                    "strategy_id": alert.strategy.strategy_id,
                    "symbol": alert.symbol,
                    "direction": alert.direction,
                    "occ_symbol": alert.occ_symbol,
                    "strike": str(alert.strike),
                    "entry_premium": str(alert.entry_premium),
                    "exit_premium": str(alert.exit_premium) if alert.exit_premium is not None else None,
                    "exit_reason": alert.exit_reason,
                    "net_dollars": str(alert.net_dollars) if alert.net_dollars is not None else None,
                    "net_pct": str(alert.net_pct) if alert.net_pct is not None else None,
                    "status": alert.status,
                }
                day_alerts.append(entry)

            day_net = result.net_total
            net += day_net
            total_alerts += len(result.alerts)
            total_closed += len(result.closed)

            all_days.append({
                "date": str(day),
                "alerts": day_alerts,
                "net": str(day_net),
            })

            if not save:
                Alert.objects.filter(
                    session_date=day,
                    source=Alert.Source.REPLAY,
                    pk__in=[a.pk for a in result.alerts],
                ).delete()

        winners = 0
        for d in all_days:
            for a in d.get("alerts", []):
                nd = a.get("net_dollars")
                if nd is not None and Decimal(nd) > 0:
                    winners += 1

        return Response({
            "days": all_days,
            "summary": {
                "sesiones": len(days),
                "alertas": total_alerts,
                "cerradas": total_closed,
                "ganadoras": winners,
                "perdedoras": total_closed - winners,
                "neto": str(net),
                "save": save,
                "disclaimer": (
                    "Reconstruccion sin latencia ni competencia por fill. "
                    "El neto es un limite superior optimista."
                ),
            },
        })
