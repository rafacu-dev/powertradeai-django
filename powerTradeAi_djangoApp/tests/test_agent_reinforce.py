"""Skill reinforce_position: la rama 'reforzar' de la decision en el stop.

Verifica los gates (solo entrenamiento, stop tocado, una vez, no 0DTE, tope de
riesgo) y que el refuerzo promedia la prima y duplica los contratos de forma que
el resolver calcule el P&L combinado sin tocarlo.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from powerTradeAi_djangoApp.agent import skills
from powerTradeAi_djangoApp.data.base import Quote
from powerTradeAi_djangoApp.models import Alert, AgentRun, Strategy
from powerTradeAi_djangoApp.strategies.base import NY

AS_OF = datetime(2026, 7, 15, 13, 0, tzinfo=NY)


class FakeProvider:
    name = "fake"

    def __init__(self, bid, ask):
        self.q = Quote(bid=bid, ask=ask)

    def option_quote(self, occ, at=None):
        return self.q


@pytest.fixture
def patch_provider(monkeypatch):
    def _set(bid, ask):
        monkeypatch.setattr(skills, "_provider", lambda: FakeProvider(bid, ask))
    return _set


def _open_position(*, entry_ask="1.00", contracts=1, stop_pct=15, dte=1,
                   cost=100.0, reinforced=False):
    run = AgentRun.objects.create(
        trigger=AgentRun.Trigger.TRAINING, status=AgentRun.Status.RUNNING,
        model_name="test", symbols=["TSLA"], goal="test")
    strat, _ = Strategy.objects.get_or_create(
        strategy_id="AGENT:TSLA",
        defaults={"name": "Agente TSLA", "symbol": "TSLA",
                  "rule_version": "agent_v1", "enabled": False})
    meta = {"stop_pct": stop_pct, "dte": dte, "cost": cost, "thesis": "t"}
    if reinforced:
        meta["reinforced"] = True
    a = Alert.objects.create(
        strategy=strat, rule_version="agent_v1", symbol="TSLA",
        session_date=AS_OF.date(), direction="PUT", source=Alert.Source.AGENT_TRAIN,
        status=Alert.Status.PENDING, signal_ts=AS_OF, entry_ts=AS_OF,
        scheduled_exit_ts=AS_OF, agent_run=run, occ_symbol="TSLA  260716P00380000",
        strike=Decimal("380"), contracts=contracts, commission=Decimal("1.30"),
        entry_ask=Decimal(entry_ask), entry_bid=Decimal(entry_ask),
        entry_premium=Decimal(entry_ask), meta=meta)
    return {"run": run, "as_of": AS_OF}, a


@pytest.mark.django_db
def test_refuerza_cuando_el_stop_esta_tocado(patch_provider):
    patch_provider(bid=0.84, ask=0.89)      # bid<=0.85 -> stop tocado
    ctx, a = _open_position(entry_ask="1.00", contracts=1)  # cost 100, +89 = 189 <= 200
    r = skills.reinforce_position(ctx, a.id, "cierre 15m bajo 380 + rechazo VWAP")
    assert r.get("reinforced") is True
    assert r["total_contracts"] == 2
    a.refresh_from_db()
    assert a.contracts == 2
    assert a.entry_ask == Decimal("0.9450")      # (1.00 + 0.89) / 2
    assert a.meta["reinforced"] is True
    assert a.meta["original_entry_ask"] == 1.0
    assert a.meta["reinforcements"][0]["confirmation"].startswith("cierre 15m")


@pytest.mark.django_db
def test_no_refuerza_si_el_stop_no_esta_tocado(patch_provider):
    patch_provider(bid=0.95, ask=1.00)      # bid > 0.85
    ctx, a = _open_position()
    r = skills.reinforce_position(ctx, a.id, "confirmacion")
    assert "error" in r and "stop no esta tocado" in r["error"]
    a.refresh_from_db()
    assert a.contracts == 1                  # intacta


@pytest.mark.django_db
def test_no_refuerza_dos_veces(patch_provider):
    patch_provider(bid=0.84, ask=0.89)
    ctx, a = _open_position(reinforced=True)
    r = skills.reinforce_position(ctx, a.id, "confirmacion")
    assert "error" in r and "ya se reforzo" in r["error"]


@pytest.mark.django_db
def test_no_refuerza_0dte(patch_provider):
    patch_provider(bid=0.84, ask=0.89)
    ctx, a = _open_position(dte=0)
    r = skills.reinforce_position(ctx, a.id, "confirmacion")
    assert "error" in r and "0DTE" in r["error"]


@pytest.mark.django_db
def test_no_refuerza_si_excede_el_riesgo_maximo(patch_provider):
    patch_provider(bid=1.20, ask=1.30)       # add 130; con cost 150 -> 280 > 200
    ctx, a = _open_position(entry_ask="1.50", cost=150.0)
    r = skills.reinforce_position(ctx, a.id, "confirmacion")
    assert "error" in r and "maximo" in r["error"]


@pytest.mark.django_db
def test_en_vivo_no_esta_disponible(patch_provider):
    patch_provider(bid=0.84, ask=0.89)
    ctx, a = _open_position()
    ctx["as_of"] = None                       # en vivo
    r = skills.reinforce_position(ctx, a.id, "confirmacion")
    assert "error" in r and "evaluacion" in r["error"]
