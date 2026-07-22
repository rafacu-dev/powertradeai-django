"""Serializers.

Regla de presentacion: si una alerta no ha terminado, sus campos de resultado
salen como la cadena ``"pending"``, no como ``null`` ni como ``0``. Un cero en
un P&L se lee como "no gano nada", que es una afirmacion distinta de "todavia
no se sabe".
"""
from __future__ import annotations

from rest_framework import serializers

from ..models import (
    AgentAnalysis, AgentNote, AgentRun, AgentTrigger, Alert, ScanRun, Strategy,
)

PENDING = "pending"


class StrategySerializer(serializers.ModelSerializer):
    class Meta:
        model = Strategy
        fields = [
            "strategy_id", "name", "symbol", "rule_version",
            "enabled", "contracts", "commission", "params",
        ]


class AlertSerializer(serializers.ModelSerializer):
    strategy_id = serializers.CharField(source="strategy.strategy_id", read_only=True)
    strategy_name = serializers.CharField(source="strategy.name", read_only=True)

    compra = serializers.SerializerMethodField()
    venta = serializers.SerializerMethodField()
    resultado = serializers.SerializerMethodField()

    class Meta:
        model = Alert
        fields = [
            "id", "strategy_id", "strategy_name", "rule_version",
            "symbol", "session_date", "direction", "status", "source",
            "signal_ts", "detected_at", "underlying_at_signal",
            "occ_symbol", "expiration", "strike", "contracts",
            "compra", "venta", "resultado", "meta",
        ]

    def get_compra(self, obj: Alert):
        """La entrada siempre esta: sin ella no habria alerta registrada."""
        return {
            "ts": obj.entry_ts,
            "strike": obj.strike,
            "prima": obj.entry_premium,
            "bid": obj.entry_bid,
            "ask": obj.entry_ask,
            "coste_total": obj.gross_entry_cost,
        }

    def get_venta(self, obj: Alert):
        # Las alertas del agente no tienen prima de opcion, pero si cierre
        # (exit_ts) y motivo: no las trates como pendientes por falta de prima.
        if obj.exit_ts is None:
            return {
                "ts": PENDING, "prima": PENDING, "motivo": PENDING,
                "cierre_previsto": obj.scheduled_exit_ts,
            }
        return {
            "ts": obj.exit_ts,
            "prima": obj.exit_premium if obj.exit_premium is not None else PENDING,
            "motivo": obj.exit_reason,
            "cierre_previsto": obj.scheduled_exit_ts,
        }

    def get_resultado(self, obj: Alert):
        # Mientras no cierre, pendiente. Al cerrar, el % siempre esta
        # (net_pct); el monto en dolares solo para las reglas de opciones.
        if obj.status != Alert.Status.CLOSED:
            return {"monto": PENDING, "porciento": PENDING, "estado": obj.status}
        return {
            "monto": obj.net_dollars if obj.net_dollars is not None else PENDING,
            "porciento": obj.net_pct if obj.net_pct is not None else PENDING,
            "estado": obj.status,
        }


class StrategyPerformanceSerializer(serializers.Serializer):
    """Agregado por regla. Solo cuenta alertas cerradas: mezclar pendientes
    en una media produce un numero que no significa nada."""

    source = serializers.CharField()
    strategy_id = serializers.CharField()
    name = serializers.CharField()
    symbol = serializers.CharField()
    alertas_totales = serializers.IntegerField()
    alertas_pendientes = serializers.IntegerField()
    alertas_cerradas = serializers.IntegerField()
    ganadoras = serializers.IntegerField()
    perdedoras = serializers.IntegerField()
    neto_total = serializers.DecimalField(max_digits=14, decimal_places=2)
    neto_medio = serializers.DecimalField(
        max_digits=14, decimal_places=2, allow_null=True)
    pct_medio = serializers.DecimalField(
        max_digits=10, decimal_places=2, allow_null=True)
    win_rate = serializers.DecimalField(
        max_digits=6, decimal_places=2, allow_null=True)


class ScanRunSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScanRun
        fields = [
            "id", "started_at", "finished_at", "strategies_evaluated",
            "alerts_created", "alerts_closed", "ok", "error",
        ]


# ── Auditoria del agente ────────────────────────────────────────────

class AgentRunListSerializer(serializers.ModelSerializer):
    """Listado ligero: sin el transcript completo (se ve en el detalle)."""

    steps = serializers.SerializerMethodField()

    class Meta:
        model = AgentRun
        fields = [
            "id", "trigger", "status", "model_name", "symbols", "goal",
            "summary", "alerts_created", "steps", "error",
            "started_at", "finished_at",
        ]

    def get_steps(self, obj: AgentRun) -> int:
        return len(obj.transcript or [])


class AgentRunSerializer(serializers.ModelSerializer):
    """Detalle: incluye el transcript completo (todo el razonamiento y cada
    skill con sus argumentos y resultado)."""

    class Meta:
        model = AgentRun
        fields = [
            "id", "trigger", "status", "model_name", "symbols", "goal",
            "summary", "transcript", "alerts_created", "error",
            "started_at", "finished_at",
        ]


class AgentAnalysisSerializer(serializers.ModelSerializer):
    agent_run_id = serializers.IntegerField(source="agent_run.id", read_only=True)

    class Meta:
        model = AgentAnalysis
        fields = ["id", "symbol", "stance", "analysis", "agent_run_id", "created_at"]


class AgentNoteSerializer(serializers.ModelSerializer):
    agent_run_id = serializers.IntegerField(
        source="agent_run.id", read_only=True, allow_null=True)

    class Meta:
        model = AgentNote
        fields = ["id", "topic", "note", "agent_run_id", "created_at"]


class AgentTriggerSerializer(serializers.ModelSerializer):
    agent_run_id = serializers.IntegerField(
        source="agent_run.id", read_only=True, allow_null=True)

    class Meta:
        model = AgentTrigger
        fields = [
            "id", "symbol", "price", "direction", "reason", "ref_price",
            "active", "agent_run_id", "created_at", "triggered_at",
        ]
