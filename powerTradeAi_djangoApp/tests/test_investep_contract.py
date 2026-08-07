"""Contrato entre DeepSeek, el validador y la creacion de alertas."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from powerTradeAi_djangoApp.agent import decision, skills
from powerTradeAi_djangoApp.data.base import Quote
from powerTradeAi_djangoApp.models import AgentRun, Alert, InvestepDecision, Strategy
from powerTradeAi_djangoApp.strategies.base import Signal
from powerTradeAi_djangoApp.strategies.gates import event_gate, validate_quote

pytestmark = pytest.mark.django_db
NY = ZoneInfo("America/New_York")
NOW = datetime(2026, 7, 6, 10, 0, tzinfo=NY)


class _Provider:
    def latest_price(self, symbol):
        return 100.0

    def option_quote(self, occ, at=None):
        return Quote(bid=0.98, ask=1.00, ts=at or NOW)


def _run() -> AgentRun:
    return AgentRun.objects.create(status=AgentRun.Status.RUNNING)


def _valid_mechanics(monkeypatch):
    signal = Signal(
        direction="CALL",
        signal_ts=NOW - timedelta(minutes=15),
        underlying=100.0,
        meta={"rama": "INTRADAY_BREAK", "bar_state": "CLOSED_15M"},
    )
    monkeypatch.setattr(
        decision,
        "_mechanical_signal",
        lambda *args, **kwargs: (
            object(),
            {
                "status": "VALID",
                "strategy_id": "TSLA_E01_INTRADIA",
                "direction": "CALL",
                "signal_ts": signal.signal_ts.isoformat(),
                "underlying": signal.underlying,
                "features": signal.meta,
                "_signal": signal,
            },
            "test-rule-v1",
        ),
    )
    monkeypatch.setattr(
        decision, "event_gate", lambda *args, **kwargs: {"status": "CLEAR"})
    monkeypatch.setattr(
        decision, "assess_terrain",
        lambda *args, **kwargs: {"status": "SUFFICIENT"},
    )


def test_decision_valida_fija_direccion_y_procedencia(monkeypatch, settings):
    _valid_mechanics(monkeypatch)
    settings.POWERTRADEAI = {"INVESTEP_WATCHLIST": ("TSLA",)}
    run = _run()
    ctx = {"run": run, "as_of": NOW, "manual_consulted": {"E01"}}

    validated = decision.validate_setup(
        ctx,
        _Provider(),
        symbol="TSLA",
        strategy_code="E01",
        branch="INTRADAY_BREAK",
        thesis="Ruptura cerrada de 15m con terreno disponible.",
    )

    assert validated.status == InvestepDecision.Status.VALID
    assert validated.direction == Alert.Direction.CALL
    assert validated.manual_hash == decision.manual_hash()
    assert validated.prompt_version == decision.PROMPT_VERSION
    assert validated.validation["mechanical_setup"]["status"] == "VALID"


def test_validador_respeta_la_contencion_de_strategy_enabled():
    Strategy.objects.create(
        strategy_id="TSLA_E01_INTRADIA",
        name="E01 deshabilitada",
        symbol="TSLA",
        rule_version="test",
        enabled=False,
    )

    class MustNotFetch:
        def bars_1m(self, symbol, day):
            raise AssertionError("una regla deshabilitada no consulta mercado")

    _, result, _ = decision._mechanical_signal(
        MustNotFetch(), symbol="TSLA", code="E01",
        branch="INTRADAY_BREAK", now=NOW)

    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "STRATEGY_NOT_ENABLED"


def test_revalidar_despues_de_consultar_manual_no_reusa_bloqueo(
        monkeypatch, settings):
    _valid_mechanics(monkeypatch)
    settings.POWERTRADEAI = {"INVESTEP_WATCHLIST": ("TSLA",)}
    run = _run()
    ctx = {"run": run, "as_of": NOW}
    kwargs = {
        "symbol": "TSLA",
        "strategy_code": "E01",
        "branch": "INTRADAY_BREAK",
        "thesis": "Ruptura cerrada de 15m con terreno disponible.",
    }

    blocked = decision.validate_setup(ctx, _Provider(), **kwargs)
    ctx["manual_consulted"] = {"E01"}
    validated = decision.validate_setup(ctx, _Provider(), **kwargs)

    assert blocked.status == InvestepDecision.Status.BLOCKED
    assert validated.status == InvestepDecision.Status.VALID
    assert validated.pk != blocked.pk


def test_parametro_de_riesgo_malformado_falla_cerrado(monkeypatch, settings):
    _valid_mechanics(monkeypatch)
    settings.POWERTRADEAI = {"INVESTEP_WATCHLIST": ("TSLA",)}
    result = decision.validate_setup(
        {"run": _run(), "as_of": NOW, "manual_consulted": {"E01"}},
        _Provider(),
        symbol="TSLA",
        strategy_code="E01",
        branch="INTRADAY_BREAK",
        thesis="Ruptura cerrada de 15m con terreno disponible.",
        target_pct="no-numero",
    )

    assert result.status == InvestepDecision.Status.BLOCKED
    assert {item["code"] for item in result.blockers} >= {
        "PLAN10_TARGET_OUT_OF_RANGE",
    }
    assert result.evidence["target_pct"] is None


def test_dos_corridas_no_comparten_decision_id(monkeypatch, settings):
    _valid_mechanics(monkeypatch)
    settings.POWERTRADEAI = {"INVESTEP_WATCHLIST": ("TSLA",)}
    kwargs = {
        "symbol": "TSLA",
        "strategy_code": "E01",
        "branch": "INTRADAY_BREAK",
        "thesis": "Ruptura cerrada de 15m con terreno disponible.",
    }
    first = decision.validate_setup(
        {"run": _run(), "as_of": NOW, "manual_consulted": {"E01"}},
        _Provider(), **kwargs)
    second = decision.validate_setup(
        {"run": _run(), "as_of": NOW, "manual_consulted": {"E01"}},
        _Provider(), **kwargs)

    assert first.pk != second.pk
    assert first.agent_run_id != second.agent_run_id


def test_create_alert_solo_consume_su_decision_una_vez(
        monkeypatch, settings):
    settings.POWERTRADEAI = {
        "ACCOUNT_SIZE": 10_000,
        "RISK_PCT_PER_TRADE": 2,
        "MAX_CONTRACTS_PER_TRADE": 5,
        "MAX_DECISION_AGE_SECONDS": 180,
        "MAX_OPTION_SPREAD_PCT": 5,
        "MAX_OPTION_QUOTE_AGE_SECONDS": 30,
        "REQUIRE_OPTION_QUOTE_TIMESTAMP": True,
    }
    run = _run()
    validated = InvestepDecision.objects.create(
        strategy_code="E01",
        branch="INTRADAY_BREAK",
        symbol="TSLA",
        direction=Alert.Direction.CALL,
        thesis="Ruptura cerrada de 15m con terreno disponible.",
        status=InvestepDecision.Status.VALID,
        as_of=NOW,
        signal_ts=NOW - timedelta(minutes=15),
        evidence={"target_pct": 15.0, "stop_pct": 20.0},
        validation={},
        blockers=[],
        manual_hash=decision.manual_hash(),
        prompt_version=decision.PROMPT_VERSION,
        rule_version="test-rule-v1",
        source=Alert.Source.AGENT,
        idempotency_key="a" * 64,
        agent_run=run,
    )
    monkeypatch.setattr(skills, "_provider", lambda: _Provider())
    monkeypatch.setattr(skills, "_now", lambda ctx: NOW)
    ctx = {"run": run, "channel": "agent"}

    first = skills.create_alert(ctx, decision_id=validated.id)
    second = skills.create_alert(ctx, decision_id=validated.id)

    assert first["created"] is True
    assert first["contracts"] == 2
    assert second == {
        "alert_id": first["alert_id"],
        "created": False,
        "decision_id": validated.id,
        "status": Alert.Status.PENDING,
    }
    alert = Alert.objects.get()
    assert alert.investep_decision_id == validated.id
    assert alert.symbol == "TSLA"
    assert alert.academy_strategy == "E01"
    assert alert.strategy_branch == "INTRADAY_BREAK"
    assert alert.signal_ts == validated.signal_ts
    assert alert.evaluation_version == "investep_v2"


def test_create_alert_rechaza_decision_de_otra_corrida(monkeypatch):
    owner = _run()
    intruder = _run()
    validated = InvestepDecision.objects.create(
        strategy_code="E01", branch="INTRADAY_BREAK", symbol="TSLA",
        direction=Alert.Direction.CALL, thesis="tesis suficientemente concreta",
        status=InvestepDecision.Status.VALID, as_of=NOW,
        evidence={}, validation={}, blockers=[],
        manual_hash=decision.manual_hash(),
        prompt_version=decision.PROMPT_VERSION,
        source=Alert.Source.AGENT, idempotency_key="b" * 64,
        agent_run=owner,
    )
    monkeypatch.setattr(skills, "_now", lambda ctx: NOW)
    result = skills.create_alert({"run": intruder}, decision_id=validated.id)
    assert result["error"] == "decision de otra corrida"
    assert Alert.objects.count() == 0


def test_event_gate_bloquea_sin_cobertura_y_earnings(settings):
    settings.POWERTRADEAI = {}
    assert event_gate("TSLA", NOW)["blocker"] == "PENDING_EVENT_CALENDAR"

    settings.POWERTRADEAI = {
        "EVENT_CALENDAR_COVERAGE_FROM": "2026-07-01",
        "EVENT_CALENDAR_COVERAGE_UNTIL": "2026-07-31",
        "EVENT_CALENDAR": [{
            "type": "earnings", "symbol": "TSLA", "date": "2026-07-09",
            "confirmed": True,
        }],
    }
    result = event_gate("TSLA", NOW)
    assert result["status"] == "BLOCKED"
    assert result["blocker"] == "NO_OPERAR_EVENTO"

    settings.POWERTRADEAI = {
        "EVENT_CALENDAR_COVERAGE_FROM": "2026-07-01",
        "EVENT_CALENDAR_COVERAGE_UNTIL": "2026-07-31",
        "EVENT_CALENDAR": [{"type": "earnigns", "date": "2026-07-09"}],
    }
    malformed = event_gate("TSLA", NOW)
    assert malformed["status"] == "UNKNOWN"
    assert malformed["reason"] == "calendar_entry_malformed"


def test_terreno_no_reutiliza_modelo_calibrado_para_otro_target(
        monkeypatch, settings):
    from powerTradeAi_djangoApp.strategies import gates

    settings.POWERTRADEAI = {
        "SPOT_PREMIUM_MODELS": {
            "TSLA": {
                "required_move_abs_usd": 1.0,
                "sample_size": 30,
                "target_premium_pct": 15,
                "source": "estudio-test",
                "version": "v1",
            },
        },
        "MIN_SPOT_PREMIUM_SAMPLES": 20,
    }
    monkeypatch.setattr(
        gates, "_moving_average_levels",
        lambda frame, prefix: [{
            "name": f"MA40_{prefix}", "price": 105.0,
            "provenance": "REGLA_ACADEMIA",
        }],
    )
    monkeypatch.setattr(gates, "_pivot_levels", lambda *args, **kwargs: [])

    class TerrainContext:
        symbol = "TSLA"

        def history(self, timeframe, days):
            return pd.DataFrame()

        def resample(self, timeframe):
            return pd.DataFrame()

    result = gates.assess_terrain(
        TerrainContext(), "CALL", 100.0, target_premium_pct=10.0)

    assert result["blocker"] == "PENDING_EMPIRICAL_MOVE_MODEL"
    assert result["reason"] == "target_mismatch"


def test_quote_gate_rechaza_timestamp_vencido(settings):
    settings.POWERTRADEAI = {
        "MAX_OPTION_QUOTE_AGE_SECONDS": 30,
        "REQUIRE_OPTION_QUOTE_TIMESTAMP": True,
    }
    stale = Quote(bid=0.98, ask=1.00, ts=NOW - timedelta(seconds=31))
    assert validate_quote(stale, as_of=NOW)["blocker"] == "OPTION_QUOTE_STALE"
    fresh = Quote(bid=0.98, ask=1.00, ts=NOW - timedelta(seconds=5))
    assert validate_quote(fresh, as_of=NOW)["status"] == "VALID"
    future = Quote(bid=0.98, ask=1.00, ts=NOW + timedelta(seconds=3))
    assert validate_quote(
        future, as_of=NOW)["blocker"] == "OPTION_QUOTE_FROM_FUTURE"
    delayed_execution = validate_quote(
        future, as_of=NOW, allow_after_seconds=90)
    assert delayed_execution["status"] == "VALID"
    assert delayed_execution["execution_latency_seconds"] == 3.0
    invalid_number = Quote(bid=float("nan"), ask=1.00, ts=NOW)
    assert validate_quote(
        invalid_number, as_of=NOW)["blocker"] == "OPTION_QUOTE_EMPTY"


def test_snapshot_thetadata_no_fabrica_timestamp_de_quote():
    from powerTradeAi_djangoApp.data.thetadata_cloud import _normalize_quote

    without_time = _normalize_quote(pd.DataFrame([{"bid": 0.98, "ask": 1.00}]))
    assert without_time.ts is None

    with_time = _normalize_quote(pd.DataFrame([{
        "bid": 0.98,
        "ask": 1.00,
        "timestamp": "2026-07-06T14:00:00Z",
    }]))
    assert with_time.ts.isoformat() == "2026-07-06T14:00:00+00:00"


def test_misma_senal_academica_no_se_duplica_entre_corridas(
        monkeypatch, settings):
    _valid_mechanics(monkeypatch)
    settings.POWERTRADEAI = {
        "INVESTEP_WATCHLIST": ("TSLA",),
        "ACCOUNT_SIZE": 10_000,
        "RISK_PCT_PER_TRADE": 2,
        "MAX_CONTRACTS_PER_TRADE": 5,
        "MAX_DECISION_AGE_SECONDS": 180,
        "MAX_OPTION_SPREAD_PCT": 5,
        "MAX_OPTION_QUOTE_AGE_SECONDS": 30,
        "REQUIRE_OPTION_QUOTE_TIMESTAMP": True,
    }
    monkeypatch.setattr(skills, "_provider", lambda: _Provider())
    monkeypatch.setattr(skills, "_now", lambda ctx: NOW)
    monkeypatch.setattr(decision, "_now", lambda ctx: NOW)
    proposal = {
        "symbol": "TSLA",
        "strategy_code": "E01",
        "branch": "INTRADAY_BREAK",
        "thesis": "Ruptura cerrada de 15m con terreno disponible.",
    }

    first_run = _run()
    first_decision = decision.validate_setup(
        {"run": first_run, "manual_consulted": {"E01"}},
        _Provider(), **proposal)
    first = skills.create_alert(
        {"run": first_run, "channel": "agent"},
        decision_id=first_decision.id)

    second_run = _run()
    second_decision = decision.validate_setup(
        {"run": second_run, "manual_consulted": {"E01"}},
        _Provider(), **proposal)
    second = skills.create_alert(
        {"run": second_run, "channel": "agent"},
        decision_id=second_decision.id)

    assert first["created"] is True
    assert second["created"] is False
    assert second["reason"] == "duplicate_academic_signal"
    assert second["alert_id"] == first["alert_id"]
    assert Alert.objects.count() == 1


def test_calculo_de_rango_devuelve_dos_limites_auditables():
    from powerTradeAi_djangoApp.agent.option_range import (
        calculate_option_price_range,
    )

    contracts = pd.DataFrame([
        {"occ_symbol": "A", "expiration": date(2026, 7, 10),
         "strike": 100, "ask": 1.00, "low": 0.80, "high": 1.60},
        {"occ_symbol": "B", "expiration": date(2026, 7, 10),
         "strike": 102.5, "ask": 0.65, "low": 0.40, "high": 1.00},
        {"occ_symbol": "C", "expiration": date(2026, 7, 10),
         "strike": 97.5, "ask": 1.50, "low": 1.30, "high": 1.60},
    ])
    result = calculate_option_price_range(
        contracts, spot=100, direction="CALL")

    assert result["range_per_contract"] == {
        "minimum": 65.0, "maximum": 100.0, "currency": "USD",
    }
    assert sum(row["selected"] for row in result["contracts"]) == 2


def test_seed_solo_habilita_las_de_la_lista_explicita():
    """El permiso para operar viene de APTAS_PARA_PAPER, no de la forma del id.

    Antes bastaba con que el id llevara ``_E01_`` para que la regla se activara
    sola: anadir una clase al catalogo la ponia a operar sin que nadie lo
    decidiera. Ahora aparecer en el catalogo no da permiso.
    """
    from django.core.management import call_command

    from powerTradeAi_djangoApp.management.commands.seed_strategies import (
        APTAS_PARA_PAPER,
    )
    from powerTradeAi_djangoApp.models import Strategy

    # Una fila viva que ya no existe en el registro de codigo no puede quedar
    # activa por omision del loop.
    Strategy.objects.create(
        strategy_id="REGLA_HUERFANA",
        name="No registrada",
        symbol="TSLA",
        rule_version="legacy",
        enabled=True,
    )
    call_command("seed_strategies", stdout=StringIO())

    enabled = set(Strategy.objects.filter(enabled=True).values_list(
        "strategy_id", flat=True))
    assert enabled == {strategy_id for strategy_id, _ in APTAS_PARA_PAPER}
    assert not Strategy.objects.filter(
        strategy_id="REGLA_HUERFANA", enabled=True).exists()


def test_seed_no_deja_activa_ninguna_regla_hoy():
    """Estado declarado el 07-ago-2026: la aplicacion no opera nada.

    Este test es el que hay que cambiar a proposito al promover la primera
    regla. Sirve para que anadir una a APTAS_PARA_PAPER sea una decision
    visible y no un descuido.
    """
    from django.core.management import call_command

    from powerTradeAi_djangoApp.models import Strategy

    call_command("seed_strategies", stdout=StringIO())
    assert Strategy.objects.filter(enabled=True).count() == 0
    # El catalogo se conserva entero: no operar no es borrar.
    assert Strategy.objects.count() > 100


def test_seed_falla_si_la_lista_nombra_una_regla_inexistente(monkeypatch):
    """Un id mal escrito dejaria la regla apagada en silencio."""
    import pytest
    from django.core.management import call_command

    from powerTradeAi_djangoApp.management.commands import seed_strategies as cmd

    monkeypatch.setattr(cmd, "_APTAS_IDS", frozenset({"NO_EXISTE_ESTA_REGLA"}))
    with pytest.raises(SystemExit, match="NO_EXISTE_ESTA_REGLA"):
        call_command("seed_strategies", stdout=StringIO())
