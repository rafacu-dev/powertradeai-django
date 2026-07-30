"""Limpieza de corridas huerfanas del agente.

Lo que hace segura esta limpieza es el corte por antiguedad: una corrida real
dura minutos, asi que jamas debe tocar una que este en vuelo.
"""
from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from powerTradeAi_djangoApp.agent.maintenance import (
    DEFAULT_STALE_HOURS, close_stale_runs, stale_runs)
from powerTradeAi_djangoApp.models import AgentRun

pytestmark = pytest.mark.django_db


def _run(hours_ago, status=AgentRun.Status.RUNNING, **kw):
    run = AgentRun.objects.create(
        trigger=AgentRun.Trigger.MANUAL, status=status,
        model_name="deepseek-v4-pro", symbols=["QQQ"], goal="test", **kw)
    # started_at suele ser auto_now_add: se reescribe sin disparar auto_now
    AgentRun.objects.filter(pk=run.pk).update(
        started_at=timezone.now() - timedelta(hours=hours_ago))
    run.refresh_from_db()
    return run


def test_cierra_la_corrida_colgada():
    viejo = _run(72)
    assert close_stale_runs() == 1
    viejo.refresh_from_db()
    assert viejo.status == AgentRun.Status.ERROR
    assert "interrumpido" in viejo.error
    assert viejo.finished_at is not None


def test_no_toca_una_corrida_en_vuelo():
    """Una corrida arrancada hace minutos puede estar ejecutandose AHORA."""
    fresco = _run(0)
    assert close_stale_runs() == 0
    fresco.refresh_from_db()
    assert fresco.status == AgentRun.Status.RUNNING
    assert fresco.finished_at is None


def test_respeta_el_limite_de_horas():
    _run(DEFAULT_STALE_HOURS - 1)   # justo por debajo del corte
    assert close_stale_runs() == 0
    _run(DEFAULT_STALE_HOURS + 1)   # justo por encima
    assert close_stale_runs() == 1


def test_no_toca_corridas_ya_cerradas():
    hecho = _run(72, status=AgentRun.Status.DONE)
    error = _run(72, status=AgentRun.Status.ERROR, error="fallo original")
    assert close_stale_runs() == 0
    hecho.refresh_from_db(); error.refresh_from_db()
    assert hecho.status == AgentRun.Status.DONE
    assert error.error == "fallo original"


def test_comando_dry_run_no_escribe():
    viejo = _run(72)
    out = StringIO()
    call_command("cleanup_agent_runs", "--dry-run", stdout=out)
    assert "dry-run" in out.getvalue()
    viejo.refresh_from_db()
    assert viejo.status == AgentRun.Status.RUNNING


def test_comando_cierra_y_reporta():
    _run(72)
    out = StringIO()
    call_command("cleanup_agent_runs", stdout=out)
    assert "Cerradas 1" in out.getvalue()
    assert stale_runs().count() == 0


def test_comando_sin_nada_que_hacer():
    out = StringIO()
    call_command("cleanup_agent_runs", stdout=out)
    assert "Sin corridas colgadas" in out.getvalue()


def test_umbral_configurable_por_argumento():
    _run(3)
    out = StringIO()
    call_command("cleanup_agent_runs", "--older-than-hours", "2", stdout=out)
    assert "Cerradas 1" in out.getvalue()
