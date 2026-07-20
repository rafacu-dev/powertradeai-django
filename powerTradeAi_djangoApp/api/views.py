"""Endpoints de lectura. La API no crea ni modifica alertas: eso lo hace el
motor. Aqui solo se consulta."""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from ..auth import ApiKeyAuthentication
from ..models import Alert, ScanRun, Strategy
from .serializers import (
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
