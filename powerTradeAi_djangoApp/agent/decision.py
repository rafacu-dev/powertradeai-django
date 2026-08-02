"""Contrato y validacion determinista de decisiones Investep.

DeepSeek propone una estrategia y explica la tesis. El servidor vuelve a
calcular el setup y aplica los gates no negociables antes de emitir un
``decision_id`` consumible por ``create_alert``.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from ..models import Alert, InvestepDecision, Strategy
from ..strategies import ScanContext, get_strategy_class
from ..strategies.base import NY
from ..strategies.gates import assess_terrain, event_gate
from . import investep

PROMPT_VERSION = "investep-agent-v2"

STRATEGY_DIRECTIONS = {
    "E01": "CALL",
    "E02": "PUT",
    "E03": "CALL",
    "E04": "PUT",
    "E05": "CALL",
    "E06": "PUT",
    "E07": "CALL",
    "E08": "PUT",
    "E09": "PUT",
    "E10": "CALL",
}

STRATEGY_BRANCHES = {
    "E01": {"OPENING_GAP", "INTRADAY_BREAK"},
    "E02": {"OPENING_GAP", "INTRADAY_BREAK"},
    "E03": {"HOURLY_CHANGE"},
    "E04": {"HOURLY_CHANGE"},
    "E05": {"DAILY_BOUNCE"},
    "E06": {"DAILY_BOUNCE"},
    "E07": {"LATERAL_BREAK"},
    "E08": {"LATERAL_BREAK"},
    "E09": {"OPEN_OUTSIDE_NO_VOL"},
    "E10": {"OPEN_OUTSIDE_NO_VOL"},
}


def manual_hash() -> str:
    return hashlib.sha256(investep.texto().encode("utf-8")).hexdigest()


def configured_watchlist() -> tuple[str, ...]:
    raw = getattr(settings, "POWERTRADEAI", {}).get(
        "INVESTEP_WATCHLIST", ("TSLA", "SPY", "QQQ"))
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return ()
    return tuple(dict.fromkeys(
        str(item).strip().upper() for item in raw if str(item).strip()))


def _source(ctx: dict) -> str:
    return Alert.Source.AGENT_TRAIN if ctx.get("as_of") is not None else Alert.Source.AGENT


def _now(ctx: dict) -> datetime:
    value = ctx.get("as_of") or timezone.now()
    return value.astimezone(NY)


def _branch(code: str, requested: str | None, now: datetime) -> str:
    if requested:
        return str(requested).strip().upper()
    if code in {"E01", "E02"}:
        local = now.astimezone(NY)
        return "OPENING_GAP" if local.hour == 9 and local.minute == 31 else "INTRADAY_BREAK"
    branches = STRATEGY_BRANCHES.get(code, set())
    return next(iter(branches), "UNKNOWN")


def _strategy_id(symbol: str, code: str, branch: str) -> str | None:
    suffix = {
        "OPENING_GAP": "APERTURA",
        "INTRADAY_BREAK": "INTRADIA",
    }.get(branch)
    return f"{symbol}_{code}_{suffix}" if suffix and code in {"E01", "E02"} else None


def _mechanical_signal(provider, *, symbol: str, code: str, branch: str,
                       now: datetime):
    strategy_id = _strategy_id(symbol, code, branch)
    if strategy_id is None:
        # E03-E10: documentadas pero sin validador mecanico. En fase de
        # investigacion el agente SI puede operarlas —si no, nunca sabremos si
        # sirven— pero el servidor no puede confirmar el setup, asi que la
        # decision queda marcada como JUICIO_AGENTE y no se mezcla despues con
        # las deterministas. Los demas gates (evento, terreno, Plan 10,
        # watchlist) se aplican igual.
        return None, {
            "status": "JUICIO_AGENTE",
            "motivo": "NO_DETERMINISTIC_VALIDATOR",
            "strategy": code,
            "aviso": ("El servidor NO ha verificado este setup. La evidencia es "
                      "lo que el agente declara haber visto."),
        }, ""
    try:
        cls = get_strategy_class(strategy_id)
    except KeyError:
        return None, {
            "status": "BLOCKED",
            "blocker": "NO_DETERMINISTIC_VALIDATOR",
            "strategy_id": strategy_id,
        }, ""

    row = Strategy.objects.filter(strategy_id=strategy_id).first()
    if row is None or not row.enabled:
        return None, {
            "status": "BLOCKED",
            "blocker": "STRATEGY_NOT_ENABLED",
            "strategy_id": strategy_id,
        }, cls.rule_version
    params = dict(row.params)
    # El validador reporta los gates por separado; la clase solo comprueba aqui
    # la forma mecanica del setup para no convertir un calendario ausente en
    # "sin señal".
    params.update(require_event_clear=False, require_terrain_model=False)
    strategy = cls(params)
    day = now.astimezone(NY).date()
    try:
        bars = provider.bars_1m(symbol, day)
        scan_ctx = ScanContext(
            provider=provider,
            symbol=symbol,
            session_date=day,
            now=now,
            bars=bars,
        )
        signal = strategy.evaluate(scan_ctx)
    except Exception as exc:
        return None, {
            "status": "BLOCKED",
            "blocker": "MARKET_DATA_ERROR",
            "error": f"{type(exc).__name__}: {exc}",
        }, cls.rule_version
    if signal is None:
        return scan_ctx, {
            "status": "WAIT",
            "blocker": "SETUP_NOT_CONFIRMED",
            "strategy_id": strategy_id,
        }, cls.rule_version
    return scan_ctx, {
        "status": "VALID",
        "strategy_id": strategy_id,
        "direction": signal.direction,
        "signal_ts": signal.signal_ts.isoformat(),
        "underlying": signal.underlying,
        "features": signal.meta,
        "_signal": signal,
    }, cls.rule_version


def _contexto_actual(provider, symbol: str, now: datetime):
    """ScanContext y precio de la sesion en curso, sin señal mecanica.

    Lo usan las estrategias sin validador: el agente afirma ver el setup en el
    precio actual, y sobre ese ancla se comprueban los gates que SI son
    deterministas.
    """
    day = now.astimezone(NY).date()
    try:
        bars = provider.bars_1m(symbol, day)
    except Exception:
        return None, None
    if bars is None or bars.empty:
        return None, None
    ctx = ScanContext(provider=provider, symbol=symbol, session_date=day,
                      now=now, bars=bars)
    cerradas = ctx.causal_bars(1)
    if cerradas.empty:
        return ctx, None
    return ctx, float(cerradas["close"].iloc[-1])


def _idempotency_payload(*, source: str, symbol: str, code: str, branch: str,
                         direction: str, signal_ts: datetime | None,
                         now: datetime, agent_run_id: int,
                         state_fingerprint: str) -> str:
    anchor = signal_ts or now.replace(second=0, microsecond=0)
    raw = {
        "source": source,
        "symbol": symbol,
        "strategy": code,
        "branch": branch,
        "direction": direction,
        "anchor": anchor.isoformat(),
        "agent_run_id": agent_run_id,
        "manual": manual_hash(),
        "state": state_fingerprint,
    }
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_setup(
    ctx: dict,
    provider,
    *,
    symbol: str,
    strategy_code: str,
    thesis: str,
    branch: str | None = None,
    target_pct: float = 15.0,
    stop_pct: float = 20.0,
) -> InvestepDecision:
    """Valida y persiste una propuesta. Nunca eleva un dato ausente a ``valid``."""
    now = _now(ctx)
    raw_symbol = str(symbol or "").strip().upper()
    raw_code = str(strategy_code or "").strip().upper()
    raw_branch = _branch(raw_code, branch, now)
    thesis_text = str(thesis or "").strip()
    try:
        target_value = float(target_pct)
    except (TypeError, ValueError, OverflowError):
        target_value = float("nan")
    try:
        stop_value = float(stop_pct)
    except (TypeError, ValueError, OverflowError):
        stop_value = float("nan")
    sym = raw_symbol[:16] or "?"
    code = raw_code[:3] or "???"
    selected_branch = raw_branch[:32] or "UNKNOWN"
    direction = STRATEGY_DIRECTIONS.get(code, "")
    source = _source(ctx)
    blockers: list[dict] = []
    validation: dict = {}
    evidence: dict = {
        "proposed_thesis": thesis_text,
        "proposed_symbol": raw_symbol,
        "proposed_strategy": raw_code,
        "proposed_branch": raw_branch,
        "target_pct": target_value if math.isfinite(target_value) else None,
        "stop_pct": stop_value if math.isfinite(stop_value) else None,
    }

    if raw_code != code or code not in investep.ESTRATEGIAS:
        blockers.append({"code": "STRATEGY_NOT_OPERABLE", "layer": "strategy"})
    allowed_branches = STRATEGY_BRANCHES.get(code, set())
    if raw_branch != selected_branch or selected_branch not in allowed_branches:
        blockers.append({"code": "INVALID_STRATEGY_BRANCH", "layer": "strategy"})
    if raw_symbol != sym or not raw_symbol:
        blockers.append({"code": "INVALID_SYMBOL", "layer": "universe"})
    if sym not in configured_watchlist():
        blockers.append({
            "code": "SYMBOL_OUTSIDE_APPROVED_WATCHLIST",
            "layer": "universe",
        })
    if len(thesis_text) < 20:
        blockers.append({"code": "THESIS_NOT_SPECIFIC", "layer": "evidence"})

    consulted = {str(item).upper() for item in ctx.get("manual_consulted", set())}
    if code not in consulted:
        blockers.append({"code": "MANUAL_NOT_CONSULTED", "layer": "manual"})
    validation["manual"] = {
        "status": "VALID" if code in consulted else "BLOCKED",
        "manual_hash": manual_hash(),
    }

    if not math.isfinite(target_value) or not 10.0 <= target_value <= 15.0:
        blockers.append({"code": "PLAN10_TARGET_OUT_OF_RANGE", "layer": "risk"})
    if not math.isfinite(stop_value) or abs(stop_value - 20.0) > 1e-9:
        blockers.append({"code": "PLAN10_STOP_INVALID", "layer": "risk"})

    scan_ctx = None
    mechanical = {
        "status": "BLOCKED", "blocker": "STRATEGY_NOT_OPERABLE"
    }
    rule_version = ""
    if direction and selected_branch in allowed_branches:
        scan_ctx, mechanical, rule_version = _mechanical_signal(
            provider, symbol=sym, code=code, branch=selected_branch, now=now)
    signal = mechanical.pop("_signal", None)
    validation["mechanical_setup"] = mechanical
    juicio_agente = mechanical["status"] == "JUICIO_AGENTE"
    if mechanical["status"] == "BLOCKED":
        blockers.append({
            "code": mechanical.get("blocker", "MECHANICAL_SETUP_BLOCKED"),
            "layer": "setup",
        })
    if juicio_agente and len(thesis_text) < 120:
        # Sin verificacion mecanica, la unica evidencia es lo que el agente
        # describe: se le exige mas detalle que cuando el servidor comprueba.
        blockers.append({"code": "THESIS_INSUFICIENTE_SIN_VALIDADOR",
                         "layer": "evidence"})

    event = event_gate(sym, now)
    validation["event"] = event
    if event["status"] != "CLEAR":
        blockers.append({
            "code": event.get("blocker", "EVENT_NOT_CLEAR"),
            "layer": "event",
        })

    terrain = {"status": "WAIT", "blocker": "SETUP_NOT_CONFIRMED"}
    if juicio_agente:
        # No hay señal mecanica que anclar: se evalua el terreno desde el precio
        # actual, que es lo que el agente dice estar viendo.
        scan_ctx, spot = _contexto_actual(provider, sym, now)
        if scan_ctx is not None and spot:
            terrain = assess_terrain(
                scan_ctx, direction, float(spot),
                target_premium_pct=(
                    target_value if math.isfinite(target_value) else 15.0))
    elif signal is not None and scan_ctx is not None:
        terrain = assess_terrain(
            scan_ctx, direction, float(signal.underlying),
            target_premium_pct=(
                target_value if math.isfinite(target_value) else 15.0))
    validation["terrain"] = terrain
    if terrain["status"] not in {"SUFFICIENT"}:
        blockers.append({
            "code": terrain.get("blocker", "TERRAIN_NOT_VALID"),
            "layer": "terrain",
        })

    if signal is not None:
        evidence["signal"] = {
            "timestamp": signal.signal_ts.isoformat(),
            "underlying": signal.underlying,
            "features": signal.meta,
        }

    if blockers:
        waiting_codes = {"SETUP_NOT_CONFIRMED"}
        status = (
            InvestepDecision.Status.WAIT
            if all(item["code"] in waiting_codes for item in blockers)
            else InvestepDecision.Status.BLOCKED
        )
    else:
        status = InvestepDecision.Status.VALID

    signal_ts = signal.signal_ts if signal is not None else None
    state_fingerprint = hashlib.sha256(json.dumps(
        {"status": status, "blockers": blockers, "validation": validation},
        sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    idem = _idempotency_payload(
        source=source, symbol=sym, code=code, branch=selected_branch,
        direction=direction or "NONE", signal_ts=signal_ts, now=now,
        agent_run_id=ctx["run"].id, state_fingerprint=state_fingerprint)
    defaults = {
        "strategy_code": code or "???",
        "branch": selected_branch,
        "symbol": sym,
        "direction": direction or "CALL",
        "thesis": thesis_text,
        "status": status,
        "as_of": now,
        "signal_ts": signal_ts,
        "evidence": evidence,
        "validation": validation,
        "blockers": blockers,
        "manual_hash": manual_hash(),
        "prompt_version": PROMPT_VERSION,
        "rule_version": rule_version,
        "validacion": (InvestepDecision.Validacion.JUICIO_AGENTE if juicio_agente
                       else InvestepDecision.Validacion.DETERMINISTA),
        "source": source,
        "agent_run": ctx["run"],
        "validated_at": timezone.now(),
    }
    decision, _ = InvestepDecision.objects.get_or_create(
        idempotency_key=idem, defaults=defaults)
    return decision


def public_result(decision: InvestepDecision) -> dict:
    return {
        "decision_id": decision.id,
        "status": decision.status,
        "strategy": decision.strategy_code,
        "branch": decision.branch,
        "symbol": decision.symbol,
        "direction": decision.direction,
        "blockers": decision.blockers,
        "validation": decision.validation,
        "manual_hash": decision.manual_hash,
        "prompt_version": decision.prompt_version,
        "validacion": decision.validacion,
        "aviso_validacion": (
            "El servidor NO verifico el setup: esta estrategia no tiene "
            "validador determinista. La evidencia es lo que declaras haber "
            "visto, y la alerta quedara marcada como tal."
            if decision.validacion == InvestepDecision.Validacion.JUICIO_AGENTE
            else "Setup recalculado y confirmado por el servidor."),
        "can_create_alert": decision.status == InvestepDecision.Status.VALID,
    }
