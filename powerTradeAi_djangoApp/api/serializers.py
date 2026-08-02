"""Serializers.

Regla de presentacion: si una alerta no ha terminado, sus campos de resultado
salen como la cadena ``"pending"``, no como ``null`` ni como ``0``. Un cero en
un P&L se lee como "no gano nada", que es una afirmacion distinta de "todavia
no se sabe".
"""
from __future__ import annotations

import re

from rest_framework import serializers

from ..models import (
    AgentAnalysis, AgentNote, AgentRun, AgentTrigger, Alert, InvestepDecision,
    ReplayRun, ScanRun, Strategy,
)

PENDING = "pending"


class StrategySerializer(serializers.ModelSerializer):
    params = serializers.SerializerMethodField()

    class Meta:
        model = Strategy
        fields = [
            "strategy_id", "name", "symbol", "rule_version",
            "enabled", "contracts", "commission", "params",
        ]

    def get_params(self, obj: Strategy):
        return _redact_secrets(obj.params)


class AlertSerializer(serializers.ModelSerializer):
    strategy_id = serializers.CharField(source="strategy.strategy_id", read_only=True)
    strategy_name = serializers.CharField(source="strategy.name", read_only=True)

    compra = serializers.SerializerMethodField()
    venta = serializers.SerializerMethodField()
    resultado = serializers.SerializerMethodField()
    meta = serializers.SerializerMethodField()

    class Meta:
        model = Alert
        fields = [
            "id", "strategy_id", "strategy_name", "rule_version",
            "evaluation_version", "academy_strategy", "strategy_branch",
            "investep_decision_id",
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

    def get_meta(self, obj: Alert):
        return _redact_secrets(obj.meta)


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
    error = serializers.SerializerMethodField()

    class Meta:
        model = ScanRun
        fields = [
            "id", "started_at", "finished_at", "strategies_evaluated",
            "alerts_created", "alerts_closed", "ok", "error",
        ]

    def get_error(self, obj: ScanRun):
        return _redact_secrets(obj.error)


class InvestepDecisionSerializer(serializers.ModelSerializer):
    agent_run_id = serializers.IntegerField(
        source="agent_run.id", read_only=True)
    thesis = serializers.SerializerMethodField()
    evidence = serializers.SerializerMethodField()
    validation = serializers.SerializerMethodField()
    blockers = serializers.SerializerMethodField()

    class Meta:
        model = InvestepDecision
        fields = [
            "id", "strategy_code", "branch", "symbol", "direction", "thesis",
            "status", "as_of", "signal_ts", "evidence", "validation",
            "blockers", "manual_hash", "prompt_version", "rule_version",
            "source", "agent_run_id", "validated_at", "created_at",
        ]

    def get_thesis(self, obj: InvestepDecision):
        return _redact_secrets(obj.thesis)

    def get_evidence(self, obj: InvestepDecision):
        return _redact_secrets(obj.evidence)

    def get_validation(self, obj: InvestepDecision):
        return _redact_secrets(obj.validation)

    def get_blockers(self, obj: InvestepDecision):
        return _redact_secrets(obj.blockers)


class ReplayRunSerializer(serializers.ModelSerializer):
    errors = serializers.SerializerMethodField()

    class Meta:
        model = ReplayRun
        fields = [
            "id", "session_date", "status", "strategy_ids", "overwrite",
            "alerts_created", "errors", "started_at", "finished_at",
        ]

    def get_errors(self, obj: ReplayRun):
        return _redact_secrets(obj.errors)


# ── Auditoria del agente ────────────────────────────────────────────

class AgentRunListSerializer(serializers.ModelSerializer):
    """Listado ligero: sin el transcript completo (se ve en el detalle)."""

    steps = serializers.SerializerMethodField()
    goal = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()

    class Meta:
        model = AgentRun
        fields = [
            "id", "trigger", "status", "model_name", "symbols", "goal",
            "summary", "alerts_created", "steps", "error",
            "started_at", "finished_at",
        ]

    def get_steps(self, obj: AgentRun) -> int:
        return len(obj.transcript or [])

    def get_goal(self, obj: AgentRun):
        return _redact_secrets(obj.goal)

    def get_summary(self, obj: AgentRun):
        return _redact_secrets(obj.summary)

    def get_error(self, obj: AgentRun):
        return _redact_secrets(obj.error)


class AgentRunSerializer(serializers.ModelSerializer):
    """Detalle: incluye el transcript completo (todo el razonamiento y cada
    skill con sus argumentos y resultado)."""

    transcript = serializers.SerializerMethodField()
    goal = serializers.SerializerMethodField()
    summary = serializers.SerializerMethodField()
    error = serializers.SerializerMethodField()

    class Meta:
        model = AgentRun
        fields = [
            "id", "trigger", "status", "model_name", "symbols", "goal",
            "summary", "transcript", "alerts_created", "error",
            "started_at", "finished_at",
        ]

    def get_transcript(self, obj: AgentRun):
        return _redact_secrets(obj.transcript)

    def get_goal(self, obj: AgentRun):
        return _redact_secrets(obj.goal)

    def get_summary(self, obj: AgentRun):
        return _redact_secrets(obj.summary)

    def get_error(self, obj: AgentRun):
        return _redact_secrets(obj.error)


class AgentAnalysisSerializer(serializers.ModelSerializer):
    agent_run_id = serializers.IntegerField(source="agent_run.id", read_only=True)
    analysis = serializers.SerializerMethodField()

    class Meta:
        model = AgentAnalysis
        fields = ["id", "symbol", "stance", "analysis", "agent_run_id", "created_at"]

    def get_analysis(self, obj: AgentAnalysis):
        return _redact_secrets(obj.analysis)


class AgentNoteSerializer(serializers.ModelSerializer):
    agent_run_id = serializers.IntegerField(
        source="agent_run.id", read_only=True, allow_null=True)
    note = serializers.SerializerMethodField()

    class Meta:
        model = AgentNote
        fields = ["id", "topic", "note", "agent_run_id", "created_at"]

    def get_note(self, obj: AgentNote):
        return _redact_secrets(obj.note)


class AgentTriggerSerializer(serializers.ModelSerializer):
    agent_run_id = serializers.IntegerField(
        source="agent_run.id", read_only=True, allow_null=True)
    reason = serializers.SerializerMethodField()

    class Meta:
        model = AgentTrigger
        fields = [
            "id", "symbol", "price", "direction", "reason", "ref_price",
            "active", "agent_run_id", "created_at", "triggered_at",
        ]

    def get_reason(self, obj: AgentTrigger):
        return _redact_secrets(obj.reason)


_SENSITIVE_KEYS = {
    "api_key", "apikey", "authorization", "password", "secret", "token",
    "access_token", "refresh_token", "api_secret",
}
_SECRET_PATTERNS = (
    re.compile(r"\b(?:ptai_[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{12,})\b"),
    re.compile(
        r"(?i)(authorization\s*[:=]\s*(?:api-key|bearer)\s+)[^&\s]+"
    ),
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^&\s]+"
    ),
)


def _redact_secrets(value):
    """Defensa adicional para no publicar credenciales dentro de tools."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if _is_sensitive_key(key)
            else _redact_secrets(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(
                lambda match: (
                    f"{match.group(1)}[REDACTED]"
                    if match.lastindex else "[REDACTED]"
                ),
                value,
            )
    return value


def _is_sensitive_key(key) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return (
        normalized in _SENSITIVE_KEYS
        or normalized.endswith("_api_key")
        or normalized.endswith("_secret")
        or normalized.endswith("_token")
        or normalized.endswith("_password")
    )
